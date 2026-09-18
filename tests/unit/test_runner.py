# -*- coding: UTF-8 -*-
"""
Unit tests for :mod:`behave_live_view.runner`: The test runner that shows
the interactive view and lets the normal test runner run the tests.
"""

import pytest

from behave.configuration import Configuration
from behave.exception import ConfigError
from behave.runner import Runner
from behave.runner_plugin import RunnerPlugin
from behave_live_view import LiveRunner
from behave_live_view import runner as runner_module
from behave_live_view.runner import LIVE_FORMAT, RUNNER_PARAM_NAME


def make_config(command_args=None, **kwargs):
    config = Configuration(command_args or [], load_config=False, **kwargs)
    # -- LIKE: behave.__main__.run_behave() before a runner is created.
    if not config.format:
        config.format = [config.default_format]
    return config


class FakeHost:
    """Fake of the LiveHost: Runs the test run without any view."""
    instances = []

    def __init__(self, reruns=0):
        self.reruns = reruns
        self.runner = None
        self.runners = []
        FakeHost.instances.append(self)

    def run(self, runner, make_runner=None):
        self.runner = runner
        self.runners.append(runner)
        failed = runner.run()
        for _ in range(self.reruns):
            self.runner = make_runner()
            self.runners.append(self.runner)
            failed = self.runner.run()
        return failed


class FakeTestRunner(Runner):
    results = []    #: Results of the next test runs (class attribute).

    def run(self):
        return type(self).results.pop(0)


@pytest.fixture
def use_fake_runner(monkeypatch):
    monkeypatch.setattr(runner_module, "DEFAULT_RUNNER_CLASS_NAME",
                        "%s:FakeTestRunner" % __name__)
    FakeTestRunner.results = []
    FakeHost.instances = []
    return FakeTestRunner


class TestLiveRunnerClass:
    def test_can_be_selected_as_behave_runner(self):
        config = make_config(["--runner=behave_live_view:LiveRunner"])
        runner = RunnerPlugin().make_runner(config)
        assert isinstance(runner, LiveRunner)
        assert RunnerPlugin.is_class_valid(LiveRunner)

    def test_uses_the_normal_test_runner_to_run_the_tests(self):
        runner = LiveRunner(make_config())
        assert type(runner.runner) is Runner    # pylint: disable=C0123

    def test_another_test_runner_can_be_selected(self, use_fake_runner):
        userdata = "%s=%s:FakeTestRunner" % (RUNNER_PARAM_NAME, __name__)
        runner = LiveRunner(make_config(["-D", userdata]))
        assert isinstance(runner.runner, FakeTestRunner)

    def test_live_runner_cannot_run_the_tests_itself(self):
        userdata = "%s=behave_live_view:LiveRunner" % RUNNER_PARAM_NAME
        with pytest.raises(ConfigError, match="live_view.runner"):
            LiveRunner(make_config(["-D", userdata]))

    def test_runner_alias_can_be_used_to_select_the_test_runner(self):
        config = make_config(["-D", "%s=default" % RUNNER_PARAM_NAME])
        assert type(LiveRunner(config).runner) is Runner    # pylint: disable=C0123

    def test_unknown_runner_alias_is_a_config_error(self):
        config = make_config(["-D", "%s=unknown_alias" % RUNNER_PARAM_NAME])
        with pytest.raises(ConfigError, match="RUNNER-ALIAS NOT FOUND"):
            LiveRunner(config)

    def test_provides_what_the_test_runner_provides(self):
        runner = LiveRunner(make_config())
        runner.runner.undefined_steps.append("STEP")
        assert runner.undefined_steps == ["STEP"]
        assert runner.features is runner.runner.features
        assert runner.aborted is False
        with pytest.raises(AttributeError):
            runner.no_such_attribute    # pylint: disable=pointless-statement


class TestUseLiveFormat:
    @pytest.mark.parametrize("command_args", [
        [],
        ["-f", "json", "-o", "report.json"],
    ])
    def test_default_formatter_on_console_is_replaced(self, command_args,
                                                      tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = make_config(command_args)
        if len(config.format) == len(config.outputs) and command_args:
            # -- LIKE: Formatter for the console in addition to a report.
            config.format.append(config.default_format)
        LiveRunner(config)
        assert config.format[-1] == LIVE_FORMAT
        assert config.format[:-1] == command_args[1:2]

    @pytest.mark.parametrize("command_args, expected", [
        (["-f", "plain"], ["plain"]),
        (["-f", "progress", "-f", "plain"], ["progress", "plain"]),
        (["-f", "live"], ["live"]),
        (["-f", LIVE_FORMAT], [LIVE_FORMAT]),
    ])
    def test_selected_console_formatter_is_kept(self, command_args, expected):
        config = make_config(command_args)
        LiveRunner(config)
        assert config.format == expected

    def test_formatter_with_output_file_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = make_config(["-f", "pretty", "-o", "pretty.txt"])
        LiveRunner(config)
        assert config.format == ["pretty"]


class TestLiveRunnerRun:
    def test_runs_without_host_if_view_cannot_be_used(self, monkeypatch,
                                                      use_fake_runner):
        monkeypatch.setattr(runner_module.LiveHost, "make_for",
                            classmethod(lambda cls, config: None))
        use_fake_runner.results = [True]
        assert LiveRunner(make_config()).run() is True
        use_fake_runner.results = [False]
        assert LiveRunner(make_config()).run() is False

    def test_host_runs_the_test_runner(self, monkeypatch, use_fake_runner):
        monkeypatch.setattr(runner_module.LiveHost, "make_for",
                            classmethod(lambda cls, config: FakeHost()))
        use_fake_runner.results = [True]
        runner = LiveRunner(make_config())
        test_runner = runner.runner
        assert runner.run() is True
        assert FakeHost.instances[0].runners == [test_runner]

    def test_rerun_uses_a_new_test_runner(self, monkeypatch, use_fake_runner):
        monkeypatch.setattr(runner_module.LiveHost, "make_for",
                            classmethod(lambda cls, config: FakeHost(reruns=1)))
        use_fake_runner.results = [True, False]
        runner = LiveRunner(make_config())
        first_runner = runner.runner
        assert runner.run() is False    # -- RESULT: Of the rerun.
        runners = FakeHost.instances[0].runners
        assert len(runners) == 2 and runners[0] is first_runner
        assert runners[1] is not first_runner
        assert isinstance(runners[1], FakeTestRunner)
        # -- HINT: Runner of the last run provides the undefined steps, etc.
        assert runner.runner is runners[1]

    @pytest.mark.parametrize("command_args, expected", [
        (["--jobs=3"], "--jobs=3 is not supported"),
        ([], ""),
    ])
    def test_warns_if_jobs_are_used(self, monkeypatch, capsys, command_args,
                                    expected):
        monkeypatch.setattr(runner_module.LiveHost, "make_for",
                            classmethod(lambda cls, config: None))
        runner = LiveRunner(make_config(command_args))
        monkeypatch.setattr(runner.runner, "run", lambda: False)
        runner.run()
        captured = capsys.readouterr()
        assert expected in captured.err
        assert bool(expected) == ("not supported" in captured.err)
