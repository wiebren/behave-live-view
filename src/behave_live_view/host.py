# -*- coding: UTF-8 -*-
"""
Host of the interactive view of the "live" formatter.

The interactive view (see: :mod:`behave_live_view.app`, needs the
package "textual") must run in the main thread of the process.
Therefore, the host takes over the test run:

* The test runner runs in a background thread.
* The "live" formatter posts its events to the host (thread-safe).
* Anything that is written to stdout/stderr while the view is shown
  (summary, hook output, ...) is kept and written when the view is closed.
* The view stays open when the test run has ended (until the user quits).

.. note:: The tests are executed in a background thread then.

The host is used by :class:`behave_live_view.runner.LiveRunner`.
"""

import contextlib
import ctypes
import inspect
import os
import queue
import sys
import tempfile
import threading

from behave.formatter._registry import select_formatter_class
from behave_live_view import clipboard, editor
from behave_live_view.model import StatusTree
from behave_live_view.plain import write_failures
from behave_live_view.sources import SourceWatcher
from behave_live_view.steps import (
    StepRegistrationTracker, forget_reloadable_step_definitions
)


def has_terminal():
    """Check if stdin, stdout and stderr are bound to a terminal."""
    try:
        return all(stream is not None and stream.isatty()
                   for stream in (sys.__stdin__, sys.__stdout__,
                                  sys.__stderr__))
    except (AttributeError, ValueError):
        return False


def load_app_class():
    """Load the application class of the interactive view (needs "textual").

    :return: Application class or None, if "textual" is not installed.
    """
    try:
        from behave_live_view.app import LiveApp
    except ImportError:
        return None
    return LiveApp


class TerminalGuard:
    """Keeps stdout/stderr writers away from the terminal (context manager).

    While the interactive view owns the terminal, the file descriptors of
    stdout/stderr are redirected into a temporary file. This covers streams
    that were bound early (like: summary reporter), subprocesses and worker
    processes. The view itself writes to a duplicate of the terminal.
    """

    def __init__(self):
        self.output_file = None
        self.terminal_file = None
        self._saved_fds = {}
        self._saved_streams = None

    @staticmethod
    def is_supported():
        return os.name == "posix"

    def __enter__(self):
        if not self.is_supported():
            return self

        sys.stdout.flush()
        sys.stderr.flush()
        self.output_file = tempfile.TemporaryFile(mode="w+b")
        self.terminal_file = os.fdopen(os.dup(2), "w", encoding="utf-8",
                                       errors="replace")
        for fd in (1, 2):
            self._saved_fds[fd] = os.dup(fd)
            os.dup2(self.output_file.fileno(), fd)
        # -- HINT: "textual" renders to sys.__stderr__ and
        # determines the terminal size with sys.__stdout__.
        self._saved_streams = (sys.__stdout__, sys.__stderr__)
        sys.__stdout__ = sys.__stderr__ = self.terminal_file
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._saved_fds:
            return
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        sys.__stdout__, sys.__stderr__ = self._saved_streams
        for fd, saved_fd in self._saved_fds.items():
            os.dup2(saved_fd, fd)
            os.close(saved_fd)
        self._saved_fds = {}
        self.terminal_file.close()

    def keep_text(self, text):
        """Keep text in the order in which the output was written.

        :return: True, if the text is kept (False: Guard is not active).
        """
        if self.output_file is None or not self._saved_fds:
            return False
        # -- HINT: Unbuffered, shares the file offset with stdout/stderr.
        os.write(self.output_file.fileno(), text.encode("utf-8", "replace"))
        return True

    def read_output(self):
        """Return the text that was written to stdout/stderr meanwhile."""
        if self.output_file is None:
            return u""
        self.output_file.seek(0)
        text = self.output_file.read().decode("utf-8", "replace")
        self.output_file.close()
        self.output_file = None
        return text


class KeptOutputStream:
    """Replaces sys.stdout/sys.stderr while the interactive view is shown.

    Keeps what is written -- in the order of all output -- until the view is
    closed. It does not depend on the state of the view; nothing is lost
    if the test run starts before or ends after the view.
    """
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, guard, fallback):
        self.guard = guard
        self.fallback = fallback    # -- List of text parts (guard is off).
        self._lock = threading.Lock()

    def write(self, text):
        if not isinstance(text, str):
            text = text.decode("utf-8", "replace")
        with self._lock:
            if not self.guard.keep_text(text):
                self.fallback.append(text)
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        pass

    @staticmethod
    def isatty():
        return False

    @staticmethod
    def writable():
        return True

    def fileno(self):
        raise OSError("KeptOutputStream has no file descriptor")


class ProblemNotice:
    """A problem of the host that the view should show to the user."""

    def __init__(self, text):
        self.text = text


class LiveHost:
    """Runs a test run together with the interactive view."""
    current = None  #: The active host (used by: LiveFormatter).
    TESTRUN_DONE = object()
    PATIENCE = 3.0      # -- Seconds until the user is told about waiting.
    DELIVERY_INTERVAL = 0.05
    MAX_EVENTS_PER_DELIVERY = 5000

    def __init__(self, config, app_class):
        self.config = config
        self.app_class = app_class
        self.app = None
        self.runner = None
        self.make_runner = None
        self.failed = None
        self.error = None
        self._testrun_thread = None
        self._guard = None
        self._kept_texts = []
        self._view_is_ready = False
        self._run_count = 0
        self._gave_up = False
        #: Relative file names refer to this directory
        #: (test code may change the current directory).
        self.base_dir = os.getcwd()
        self.source_watcher = SourceWatcher()
        self._testrun_active = threading.Event()
        self._events = queue.Queue()
        #: Own status tree: Decides about the exit status and is used for
        #: the summary of failures (the view may be closed at any time and
        #: does not see all events then).
        self.status_tree = StatusTree()
        self._post_lock = threading.Lock()
        self._announced = False

    @classmethod
    def make_for(cls, config):
        """Create a host if the interactive view can and should be used.

        :return: Host object or None (test run is run normally).
        """
        console_formats = cls.select_console_formats(config)
        if len(console_formats) != 1 or config.dry_run or not has_terminal():
            # -- HINT: Another console formatter would garble the view.
            return None
        if not cls.is_live_format(console_formats[0]):
            # -- HINT: "live" writes to an output file, the console formatter
            # is another one (the view would stay empty and hide it).
            return None
        app_class = load_app_class()
        if app_class is None:
            sys.stderr.write('live: Install "textual" for the interactive '
                             'view (using plain status lines).\n')
            return None
        return cls(config, app_class)

    @staticmethod
    def is_live_format(format_name):
        from behave_live_view.formatter import LiveFormatter
        try:
            formatter_class = select_formatter_class(format_name)
        except (LookupError, ImportError, TypeError, ValueError):
            return False
        return (isinstance(formatter_class, type)
                and issubclass(formatter_class, LiveFormatter))

    @staticmethod
    def select_console_format_indexes(config):
        """Select the formats that write to the console (no outfile),
        as indexes into the list of formats.
        """
        formats = config.format or [config.default_format]
        outputs = config.outputs or []
        indexes = []
        for index, _name in enumerate(formats):
            opener = outputs[index] if index < len(outputs) else None
            if opener is None or not getattr(opener, "name", None):
                indexes.append(index)
        return indexes

    @classmethod
    def select_console_formats(cls, config):
        """Select the formats that write to the console (no outfile)."""
        formats = config.format or [config.default_format]
        return [formats[index]
                for index in cls.select_console_format_indexes(config)]

    # -- EVENTS: From the test runner thread.
    def post_event(self, event):
        """Post an event to the interactive view (thread-safe, non-blocking)."""
        if not self._announced:
            self._announced = True
            self._announce_features()
        if event.get("type") == "feature_started":
            # -- REMEMBER: State of the feature file that is shown now.
            self.source_watcher.remember_feature(event["feature"]["key"])
        self._post(event)

    def _post(self, event):
        """Apply an event to the own status tree and pass it to the view."""
        with self._post_lock:
            try:
                self.status_tree.apply(event)
            except Exception:   # pylint: disable=broad-except
                pass    # -- MALFORMED EVENT: Must not break the test run.
            self._events.put(event)

    def _announce_features(self):
        """Show the features that the runner has parsed already (pending)."""
        from behave_live_view.formatter import make_feature_outline
        features = getattr(self.runner, "features", None) or []
        if features:
            outlines = [make_feature_outline(feature) for feature in features]
            self._post({"type": "testrun_started", "features": outlines})

    def _on_view_ready(self):
        """Called once when the view runs: Take over the output streams
        (from the view), then start the test run.

        HINT: The test run must not start earlier. It replaces sys.stdout
        to capture output (and restores what it has found there).
        """
        self._view_is_ready = True
        stream = KeptOutputStream(self._guard, self._kept_texts)
        sys.stdout = sys.stderr = stream
        self._start_testrun()

    def deliver_events(self):
        """Pass the posted events to the view (called in its thread)."""
        if not self._view_is_ready:
            self._on_view_ready()
        self._keep_captured_output()
        for _ in range(self.MAX_EVENTS_PER_DELIVERY):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            if isinstance(event, ProblemNotice):
                self._show_problem(event.text)
            elif event is not self.TESTRUN_DONE:
                self.app.handle_event(event)
            elif self.error is not None:
                # -- TEST RUN CRASHED: Close the view, show the error.
                self.app.exit()
            else:
                self.app.testrun_done(self.failed)

    def _show_problem(self, text):
        notify = getattr(self.app, "notify", None)
        if notify is not None:
            notify(text, severity="error", timeout=20, markup=False)

    def make_app(self):
        """Create the view; it polls this host for events (with a timer)."""
        host = self

        class HostedLiveApp(self.app_class):
            def on_mount(self):
                # -- HINT: Called in addition to the base class handler.
                self.set_interval(host.DELIVERY_INTERVAL, host.deliver_events)

        callbacks = dict(on_interrupt=self.interrupt,
                         on_rerun=self.rerun,
                         on_check_changes=self.check_changes,
                         on_open=self.open_in_editor,
                         on_copy=clipboard.copy_to_clipboard)
        # -- HINT: Pass only what this view class supports (by name).
        supported = inspect.signature(self.app_class.__init__).parameters
        callbacks = dict((name, func) for name, func in callbacks.items()
                         if name in supported)
        return HostedLiveApp(**callbacks)

    # -- TEST RUN:
    def interrupt(self):
        """Stop the test run (requested by the user), like: KeyboardInterrupt.

        The test runner runs in a thread and gets no KeyboardInterrupt from
        a signal. A runner with "interrupt()" stops on its own.
        Otherwise, a KeyboardInterrupt is raised in the test run thread:
        the step that is executed now fails with it, like in a normal run.
        """
        runner = self.runner
        interrupt = getattr(runner, "interrupt", None)
        if interrupt is not None:
            interrupt()
        elif runner is not None and self._testrun_active.is_set():
            runner.abort(reason="KeyboardInterrupt")
            self._raise_in_testrun_thread(KeyboardInterrupt)

    def _raise_in_testrun_thread(self, exception_class):
        """Raise an exception in the test run thread (asynchronously).

        HINT: Arrives when the thread executes Python code again
        (a blocking call, like a socket read, ends first).
        """
        thread = self._testrun_thread
        if thread is None or not thread.is_alive():
            return
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread.ident), ctypes.py_object(exception_class))

    def _run_testrun(self):
        self._testrun_active.set()
        self._run_count += 1
        try:
            try:
                self.failed = bool(self.runner.run())
                self._report_feature_results()
            finally:
                self._testrun_active.clear()
        except KeyboardInterrupt:
            # -- INTERRUPTED: Outside of any step (see: interrupt()).
            self.failed = True
        except Exception as e:  # pylint: disable=broad-except
            if self._run_count == 1:
                self.error = e
            else:
                # -- RERUN CANNOT RUN: Like a syntax error in a file that
                # was just edited. Keep the view, the user can fix it.
                self.failed = True
                text = u"%s: %s" % (e.__class__.__name__, e)
                sys.stderr.write(u"RERUN FAILED: %s\n" % text)
                self._events.put(ProblemNotice(u"Rerun failed: %s" % text))
        except BaseException as e:  # pylint: disable=broad-except
            self.error = e
        finally:
            self._events.put(self.TESTRUN_DONE)

    def _report_feature_results(self):
        """Tell the view the final result of each feature of this run.

        HINT: A feature (or scenario) that is excluded, like by tags, is not
        shown to a formatter in some cases (option: --no-skipped). It would
        look like something that did not run because the run was stopped.
        """
        for feature in getattr(self.runner, "features", None) or []:
            try:
                statuses = dict(
                    (str(scenario.location), scenario.status.name)
                    for scenario in feature.walk_scenarios())
                self._post({"type": "feature_result",
                            "filename": feature.filename,
                            "status": feature.status.name,
                            "statuses": statuses})
            except Exception:   # pylint: disable=broad-except
                continue    # -- BEST EFFORT: Other kind of feature object.

    def _start_testrun(self):
        self._testrun_thread = threading.Thread(
            target=self._run_testrun, name="behave-testrun", daemon=True)
        self._testrun_thread.start()

    def open_in_editor(self, filename, line=None):
        """Open a source location in the editor (requested by the user).

        HINT: Called in the thread of the view. An editor that needs the
        terminal gets it, the view is suspended while the editor runs.
        :return: None on success, otherwise the message for the user.
        """
        userdata = getattr(self.config, "userdata", None) or {}
        filename = os.path.join(self.base_dir, filename)
        try:
            command = editor.select_editor_command(
                configured=userdata.get("live.editor"))
            if not editor.needs_terminal(command):
                editor.open_in_editor(filename, line, command=command)
                return None
            # -- HINT: stdout/stderr are redirected, use the real terminal.
            terminal = getattr(self._guard, "terminal_file", None)
            with self._suspended_view():
                editor.open_in_editor(filename, line, command=command,
                                      terminal=terminal)
        except editor.EditorError as e:
            return str(e)
        except Exception as e:  # pylint: disable=broad-except
            return "Cannot open editor: %s: %s" % (e.__class__.__name__, e)
        return None

    @contextlib.contextmanager
    def _suspended_view(self):
        """Give the terminal to another program (context manager).

        HINT: app.suspend() is not used. It binds sys.stdout/sys.stderr to
        the terminal and restores what it has found before. But the test
        run continues meanwhile and replaces these streams for each step
        that it captures: its output would go to the terminal and a stale
        capture buffer could stay in place. The editor does not need these
        streams, it gets the terminal as its stdout/stderr.
        """
        app = self.app
        driver = getattr(app, "_driver", None)
        if driver is None or not getattr(driver, "can_suspend", False):
            raise editor.EditorError(
                "An editor that needs the terminal cannot be used here; "
                "set BEHAVE_EDITOR to an editor with its own window.")
        app._suspend_signal()       # pylint: disable=protected-access
        driver.suspend_application_mode()
        try:
            with driver.no_automatic_restart():
                yield
        finally:
            driver.resume_application_mode()
            app._resume_signal()    # pylint: disable=protected-access
            app.refresh(layout=True)

    def check_changes(self, locations):
        """Check which source files were changed since they were loaded.

        :return: Dict with lists of file names: "reload" (changed modules
            that can be reloaded, the user is asked), "warn" (changed, but
            cannot be reloaded) and "features" (changed feature files).
        """
        locations = [location for location in locations if location]
        return self.source_watcher.check_changes(locations)

    def rerun(self, locations, reload=False):
        """Run some features/scenarios again (requested by the user).

        :param locations: What to run, as list of "file" or "file:line".
        :param reload: If true, changed Python modules are loaded again.
        :return: True, if the test run was started.
        """
        # pylint: disable=redefined-builtin
        locations = [location for location in locations if location]
        thread = self._testrun_thread
        if (not locations or self.make_runner is None
                or (thread is not None and thread.is_alive())):
            return False

        # -- NEW TEST RUN: With a new runner and a clean runtime
        # (step definitions and hooks are loaded again).
        # -- HINT: Test code may have changed the current directory.
        os.chdir(self.base_dir)
        watcher = self.source_watcher
        if reload:
            watcher.reload_project_modules()
        # -- HINT: After the reload. Keeps the step definitions of modules
        # that stay loaded (step libraries): they are not executed again.
        forget_reloadable_step_definitions()

        # -- CHANGED FEATURE FILE: Its lines may have moved, run all of it.
        changed_features = watcher.select_changed_features(locations)
        if changed_features:
            locations = [watcher.select_feature_file(location)
                         if watcher.select_feature_file(location)
                         in changed_features else location
                         for location in locations]
            locations = list(dict.fromkeys(locations))
        config = self.config
        config.paths = list(locations)
        config.reporters = []
        config.setup_reporters()
        self.failed = None
        self.runner = self.make_runner()
        self._post({"type": "rerun_started",
                    "locations": list(locations),
                    "changed_features": changed_features})
        self._start_testrun()
        return True

    def run(self, runner, make_runner=None):
        """Run the test run with the interactive view.

        :param runner:  Test runner to use.
        :param make_runner: Function that makes a new runner (for: rerun).
        :return: True, if the test run failed (like: runner.run()).
        """
        self.runner = runner
        self.make_runner = make_runner
        self.app = self.make_app()
        guard = self._guard = TerminalGuard()
        # -- REMEMBER: Which file registers a step definition (for: rerun).
        step_tracker = StepRegistrationTracker()
        step_tracker.start()
        type(self).current = self
        original_streams = (sys.stdout, sys.stderr)
        view_interrupted = False
        view_problem = None
        try:
            with guard:
                try:
                    # -- HINT: Test run is started when the view is ready.
                    self.app.run()
                except KeyboardInterrupt:
                    view_interrupted = True
                except Exception as e:  # pylint: disable=broad-except
                    if self._testrun_thread is not None:
                        raise
                    view_problem = e    # -- VIEW FAILED TO START.
                finally:
                    # -- ENSURE: Test run ends, even if the view was quit.
                    self._wait_for_testrun(guard)
                    self._keep_captured_output()
                    sys.stdout, sys.stderr = original_streams
        finally:
            type(self).current = None
            step_tracker.stop()
            self._write_kept_output(guard)

        if self._testrun_thread is None and self.error is None:
            if view_interrupted:
                return True     # -- USER: Has stopped it before it started.
            # -- VIEW FAILED TO START: Run the test run without it.
            if view_problem is not None:
                sys.stderr.write("live: Interactive view failed (%s: %s), "
                                 "using plain status lines.\n" % (
                                     view_problem.__class__.__name__,
                                     view_problem))
            return self.runner.run()

        if self.error is not None:
            raise self.error    # pylint: disable=raising-bad-type
        return bool(self.failed) or self._has_failures()

    def _wait_for_testrun(self, guard):
        """The view is closed: Wait until the test run has ended.

        HINT: A step may block for long (the interrupt arrives when its
        blocking call ends). Tell the user and let him give up (ctrl+c).
        """
        thread = self._testrun_thread
        if thread is None or not thread.is_alive():
            return
        terminal = getattr(guard, "terminal_file", None) or sys.__stderr__
        try:
            self.interrupt()
            thread.join(self.PATIENCE)
            if not thread.is_alive():
                return
            terminal.write(u"behave: Waiting for the step that runs now "
                           u"to stop (press ctrl+c to give up) ...\n")
            terminal.flush()
            while thread.is_alive():
                thread.join(0.25)
        except KeyboardInterrupt:
            # -- GIVE UP: The (daemon) thread ends with this process.
            self.failed = True
            self._gave_up = True

    def _has_failures(self):
        """Check the final state: A rerun may have fixed failures, but
        nothing may be left that failed or that never ran (test run was
        stopped by the user or by --stop).
        """
        status_tree = self.status_tree
        counts = status_tree.counts()
        not_run = (counts.get("untested", 0) + counts.get("pending", 0)
                   + counts.get("running", 0))
        return bool(status_tree.failed_nodes() or not_run)

    def _keep_captured_output(self):
        """Move what the view has captured from print() to the kept output.

        HINT: Keeps it in order with the output that bypasses the view,
        like the summary of a reporter that was bound to stdout early.
        """
        captured_output = getattr(self.app, "captured_output", None)
        while captured_output and self._guard is not None:
            if not self._guard.keep_text(captured_output[0][0]):
                return
            captured_output.pop(0)

    def _write_failures(self):
        """Write what has failed (the details are gone with the view)."""
        colored = self.config.has_colored_mode(sys.stdout)
        write_failures(self.status_tree, sys.stdout, colored=colored)

    def _write_kept_output(self, guard):
        self._write_failures()
        # -- HINT: Whatever could not be moved to the kept output in time.
        kept_output = guard.read_output() + u"".join(self._kept_texts)
        sys.stdout.write(kept_output)
        for text, is_stderr in getattr(self.app, "captured_output", ()):
            stream = sys.stderr if is_stderr else sys.stdout
            stream.write(text)
        sys.stdout.flush()
        sys.stderr.flush()
