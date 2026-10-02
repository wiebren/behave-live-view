# -*- coding: UTF-8 -*-
"""
Unit tests for :mod:`behave_live_view.remote`: The events of other processes
(worker processes of a parallel test run) for the interactive view.

HINT: Sender and receiver work across processes; here they are used in one
process (the connection is the same).
"""

import io
import multiprocessing
import os
import threading
import time

import pytest

from behave.formatter.base import StreamOpener
from behave_live_view import LiveFormatter, model
from behave_live_view.host import LiveHost
from behave_live_view.remote import (
    EventReceiver, EventSender, EVENTS_PARAM_NAME
)

from test_live import (
    FakeApp, FakeRunner, make_config, make_outline_a, run_feature_with
)


class EventCollector:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


@pytest.fixture
def receiver():
    collector = EventCollector()
    this_receiver = EventReceiver(collector)
    this_receiver.collector = collector
    yield this_receiver
    this_receiver.close()


def make_worker_config(receiver, command_args=None):
    """Configuration of a worker process: The parent has sent its userdata."""
    config = make_config(command_args)
    config.userdata[EVENTS_PARAM_NAME] = receiver.param_value
    return config


class TestEventChannel:
    def test_sender_delivers_events_to_the_receiver(self, receiver):
        events = [{"type": "feature_started", "feature": make_outline_a()},
                  {"type": "feature_finished", "filename": "a.feature",
                   "status": "passed"}]
        sender = EventSender.connect_for(make_worker_config(receiver))
        for event in events:
            sender.send(event)
        sender.close()
        assert receiver.wait_until_done(timeout=5)
        assert receiver.collector.events == events
        assert receiver.event_count == 2

    def test_receiver_waits_until_the_senders_are_done(self, receiver):
        sender = EventSender.connect_for(make_worker_config(receiver))
        sender.send({"type": "testrun_started", "files": ["a.feature"]})
        deadline = time.time() + 5
        while not receiver.collector.events and time.time() < deadline:
            time.sleep(0.01)
        assert receiver.wait_until_done(timeout=0.2) is False
        sender.close()
        assert receiver.wait_until_done(timeout=5) is True

    def test_receiver_accepts_many_senders(self, receiver):
        config = make_worker_config(receiver)
        senders = [EventSender.connect_for(config) for _ in range(3)]
        for index, sender in enumerate(senders):
            sender.send({"type": "output", "filename": None,
                         "text": u"WORKER-%d" % index})
            sender.close()
        assert receiver.wait_until_done(timeout=5)
        texts = sorted(event["text"] for event in receiver.collector.events)
        assert texts == [u"WORKER-0", u"WORKER-1", u"WORKER-2"]

    def test_receiver_rejects_other_connections(self, receiver):
        from multiprocessing import AuthenticationError
        from multiprocessing.connection import Client
        address = receiver.param_value.partition(u":")[2]
        with pytest.raises(AuthenticationError):
            Client(address, authkey=b"XFAIL-WRONG-KEY")
        assert receiver.wait_until_done(timeout=5)
        assert receiver.collector.events == []

    def test_receiver_accepts_senders_that_connect_at_once(self, receiver):
        # -- REGRESSION: The workers of a parallel test run connect at the
        # same time. On macOS, a full listen queue refused them.
        config = make_worker_config(receiver)
        barrier = threading.Barrier(16)
        senders = []

        def connect():
            barrier.wait()
            senders.append(EventSender.connect_for(config))

        threads = [threading.Thread(target=connect) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(senders) == 16 and None not in senders
        for sender in senders:
            sender.close()

    def test_no_sender_without_receiver(self):
        assert EventSender.connect_for(make_config()) is None

    @pytest.mark.parametrize("param_value", [
        u"00:" + os.path.join("no-such-dir", "listener"),
        u"not-hex:somewhere",
        u"garbage",
    ])
    def test_no_sender_if_receiver_cannot_be_reached(self, param_value):
        config = make_config()
        config.userdata[EVENTS_PARAM_NAME] = param_value
        assert EventSender.connect_for(config) is None

    def test_sender_ignores_a_receiver_that_is_gone(self):
        connection, other_end = multiprocessing.Pipe()
        other_end.close()   # -- LIKE: The parent process has ended.
        sender = EventSender(connection)
        sender.send({"type": "output", "filename": None, "text": u"x"})
        assert sender.connection is None
        sender.send({"type": "testrun_finished"})
        sender.close()      # -- SHOULD NOT RAISE.

    def test_events_of_wrong_type_are_ignored(self, receiver):
        sender = EventSender.connect_for(make_worker_config(receiver))
        sender.send([u"not", u"an", u"event"])
        sender.send({"type": "testrun_finished"})
        sender.close()
        assert receiver.wait_until_done(timeout=5)
        assert receiver.collector.events == [{"type": "testrun_finished"}]

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="REQUIRES: os.fork()")
    @pytest.mark.filterwarnings("ignore:.*multi-threaded.*fork:DeprecationWarning")
    def test_forked_child_does_not_keep_the_connection(self, receiver):
        # -- CASE: A step forks a child process that lives on. The host
        # must not wait for its end.
        sender = EventSender.connect_for(make_worker_config(receiver))
        pid = os.fork()
        if pid == 0:    # -- CHILD PROCESS:
            os._exit(0 if sender.connection is None else 1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        sender.send({"type": "testrun_finished"})   # -- STILL CONNECTED.
        sender.close()
        assert receiver.wait_until_done(timeout=5)
        assert receiver.collector.events == [{"type": "testrun_finished"}]


# -----------------------------------------------------------------------------
# FORMATTER: In a worker process of a parallel test run
# -----------------------------------------------------------------------------
class TestLiveFormatterInWorker:
    def test_sends_events_instead_of_plain_status_lines(self, receiver):
        config = make_worker_config(receiver, ["--no-color"])
        config.reporters = []
        stream = io.StringIO()
        formatter = LiveFormatter(StreamOpener(stream=stream), config)
        assert formatter.host is None and formatter.sender is not None
        run_feature_with(formatter, config)
        formatter.close()

        assert receiver.wait_until_done(timeout=5)
        assert stream.getvalue() == u""
        events = receiver.collector.events
        assert events[0]["type"] == "feature_started"
        assert events[-1]["type"] == "feature_finished"
        tree = model.StatusTree()
        for event in events:
            tree.apply(event)
        assert tree.features["features/alice.feature"].state == model.FAILED

    def test_does_not_end_the_testrun(self, receiver):
        # -- HINT: A worker process runs one feature after the other, each
        # with a new formatter; other features are still running.
        config = make_worker_config(receiver)
        formatter = LiveFormatter(StreamOpener(stream=io.StringIO()), config)
        formatter.close()
        assert receiver.wait_until_done(timeout=5)
        assert {"type": "testrun_finished"} not in receiver.collector.events

    def test_writes_plain_status_lines_if_host_is_gone(self, receiver):
        config = make_worker_config(receiver, ["--no-color"])
        config.reporters = []
        receiver.close()
        stream = io.StringIO()
        formatter = LiveFormatter(StreamOpener(stream=stream), config)
        assert formatter.sender is None
        run_feature_with(formatter, config)
        formatter.close()
        assert u"✘ Feature: Alice" in stream.getvalue()

    def test_host_in_this_process_is_used_first(self, receiver, monkeypatch):
        host = LiveHost(make_config(), FakeApp)
        monkeypatch.setattr(LiveHost, "current", host)
        config = make_worker_config(receiver)
        formatter = LiveFormatter(StreamOpener(stream=io.StringIO()), config)
        assert formatter.host is host and formatter.sender is None


# -----------------------------------------------------------------------------
# HOST: Receives the events of the worker processes
# -----------------------------------------------------------------------------
class WorkerRunner(FakeRunner):
    """Fake of a parallel test runner: Its "worker" sends the events."""

    def __init__(self, host, events, **kwargs):
        super(WorkerRunner, self).__init__(host, **kwargs)
        self.worker_events = events
        self.param_value = None

    def run(self):
        config = self.host.config
        self.param_value = config.userdata.get(EVENTS_PARAM_NAME)

        def run_worker():
            sender = EventSender.connect_for(config)
            for event in self.worker_events:
                sender.send(event)
            sender.close()

        # -- HINT: Its events may arrive after its result (see: host).
        worker = threading.Thread(target=run_worker)
        worker.start()
        worker.join()
        return self.failed


class TestLiveHostWithWorkers:
    @pytest.fixture(autouse=True)
    def without_terminal_guard(self, monkeypatch):
        from behave_live_view.host import TerminalGuard
        monkeypatch.setattr(TerminalGuard, "is_supported",
                            staticmethod(lambda: False))

    def test_events_of_worker_processes_are_shown(self):
        events = [{"type": "feature_started", "feature": make_outline_a()},
                  {"type": "scenario_finished", "filename": "a.feature",
                   "key": "a.feature:2", "status": "passed", "duration": 0}]
        host = LiveHost(make_config(["--jobs=2"]), FakeApp)
        runner = WorkerRunner(host, events)
        assert host.run(runner) is True     # -- NOT RUN: a.feature:5
        assert runner.param_value is not None
        assert host.app.events[:2] == events
        # -- END OF TEST RUN: The workers do not know it, the host tells it.
        assert host.app.events[-1] == {"type": "testrun_finished"}
        scenarios = host.status_tree.features["a.feature"].scenarios()
        assert [node.state for node in scenarios] == \
               [model.PASSED, model.UNTESTED]

    def test_userdata_parameter_is_removed_after_the_testrun(self):
        config = make_config(["--jobs=2"])
        host = LiveHost(config, FakeApp)
        host.run(WorkerRunner(host, []))
        assert EVENTS_PARAM_NAME not in config.userdata
        assert host._receiver is None   # pylint: disable=protected-access

    def test_no_receiver_without_jobs(self):
        config = make_config()
        host = LiveHost(config, FakeApp)
        seen = []
        fake_runner = FakeRunner(host)
        fake_runner.run = lambda: seen.append(dict(config.userdata)) or False
        host.run(fake_runner)
        assert seen == [{}]

    @pytest.mark.parametrize("command_args, parallel", [
        (["--jobs=2"], True),
        ([], False),
    ])
    def test_view_knows_if_the_testrun_is_parallel(self, command_args,
                                                   parallel):
        class ParallelAwareApp(FakeApp):
            def __init__(self, parallel=False, **kwargs):
                super(ParallelAwareApp, self).__init__(**kwargs)
                self.parallel = parallel

        host = LiveHost(make_config(command_args), ParallelAwareApp)
        assert host.make_app().parallel is parallel

    def test_sequential_testrun_is_not_ended_by_the_host(self):
        # -- HINT: Its own "live" formatter ends it (see: LiveFormatter).
        host = LiveHost(make_config(["--jobs=2"]), FakeApp)
        host.run(FakeRunner(host))
        assert {"type": "testrun_finished"} not in host.app.events
