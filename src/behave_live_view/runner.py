# -*- coding: UTF-8 -*-
"""
Test runner that shows a test run in the interactive live view.

The interactive view needs the main thread of the process. behave calls
``runner.run()`` in the main thread and lets the user select the runner
class, therefore this runner is the entry point of the live view:

.. code-block:: ini

    # -- FILE: behave.ini
    [behave]
    runner = behave_live_view:LiveRunner

:class:`LiveRunner` does not run any tests on its own. It lets the normal
behave runner do this -- in a background thread while the view is shown,
otherwise (no terminal, another console formatter, ...) directly.
"""

import os.path
import sys

from behave.api.runner import ITestRunner
from behave.configuration import setup_parser
from behave.exception import ConfigError
from behave.runner_plugin import RunnerPlugin

from behave_live_view.host import LiveHost


#: Scoped class name of the formatter (as format name for behave).
LIVE_FORMAT = "behave_live_view:LiveFormatter"
DEFAULT_RUNNER_CLASS_NAME = "behave.runner:Runner"
#: Name of the userdata parameter that selects another test runner class.
RUNNER_PARAM_NAME = "live_view.runner"


class LiveRunner(ITestRunner):
    """Runs the tests with the normal test runner and shows the live view.

    * Console formatter: If no other formatter was selected for the console,
      the "live" formatter is used (instead of behave's default formatter).
      Formatters that write to an output file are not changed.
    * Test runner: ``behave.runner:Runner`` is used to run the tests. Select
      another one with the userdata parameter "live_view.runner", like:
      ``behave -D live_view.runner=my.package:MyRunner``
    """
    # pylint: disable=super-init-not-called

    def __init__(self, config, **kwargs):
        self.config = config
        self.runner_kwargs = kwargs
        self.runner_class_name = self.select_runner_class_name(config)
        self.use_live_format(config)
        self.runner = self.make_runner()

    # -- INTERFACE FOR: ITestRunner
    def run(self):
        """Run the selected features.

        :return: True, if test-run failed. False, on success.
        """
        self.warn_if_jobs_are_used()
        host = LiveHost.make_for(self.config)
        if host is None:
            # -- WITHOUT VIEW: Plain status lines or another formatter.
            return self.runner.run()

        try:
            return host.run(self.runner, make_runner=self.make_runner)
        finally:
            # -- HINT: The runner of the last (re-)run has the final results.
            self.runner = host.runner or self.runner

    @property
    def undefined_steps(self):
        return self.runner.undefined_steps

    def __getattr__(self, name):
        # -- HINT: Anything else is provided by the runner that runs the tests.
        if name.startswith("__") or "runner" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.runner, name)

    # -- SPECIFIC PARTS:
    @staticmethod
    def select_runner_class_name(config):
        userdata = getattr(config, "userdata", None) or {}
        return userdata.get(RUNNER_PARAM_NAME) or DEFAULT_RUNNER_CLASS_NAME

    def make_runner(self):
        """Make a new test runner that runs the tests (for each (re-)run)."""
        runner_class = self.select_runner_class()
        runner_plugin = RunnerPlugin(runner_class=runner_class)
        return runner_plugin.make_runner(self.config, **self.runner_kwargs)

    def select_runner_class(self):
        """Select the class of the test runner that runs the tests.

        :raises ConfigError: If this class is selected (or an unknown alias).
        """
        runner_name = self.runner_class_name
        runner_aliases = getattr(self.config, "runner_aliases", None) or {}
        scoped_class_name = runner_aliases.get(runner_name, runner_name)
        if ":" not in scoped_class_name:
            raise ConfigError("%s=%s (RUNNER-ALIAS NOT FOUND)"
                              % (RUNNER_PARAM_NAME, runner_name))
        runner_class = RunnerPlugin.load_class(scoped_class_name)
        if isinstance(runner_class, type) and issubclass(runner_class,
                                                         LiveRunner):
            # -- HINT: Must be checked before such a runner is created.
            raise ConfigError("%s=%s (needs a runner that runs the tests)"
                              % (RUNNER_PARAM_NAME, runner_name))
        return runner_class

    @staticmethod
    def select_command_line_formats(config):
        """Select the formats that were selected on the command line.

        :return: List of format names (or None, if this is unknown).
        """
        # -- HINT: A configuration may describe how it was built.
        command_args = getattr(config, "command_args", None)
        if not isinstance(command_args, (list, tuple)):
            # -- SAME RULE AS BEHAVE: Command line is only used by "behave".
            command_name = os.path.basename(sys.argv[0])
            if not ("behave" in command_name or "behave" in sys.argv
                    or "behave/__main__" in sys.argv[0].replace("\\", "/")):
                return None
            command_args = sys.argv[1:]
        try:
            # -- HINT: Knows abbreviated and clustered options, too.
            args, _ = setup_parser().parse_known_args(list(command_args))
        except (Exception, SystemExit):     # pylint: disable=broad-except
            return None
        return list(args.format or [])

    @classmethod
    def use_live_format(cls, config):
        """Use the "live" formatter on the console, unless the user has
        selected another formatter for it.

        HINT: behave has already replaced "no formatter selected" with its
        default formatter when a runner is created. Therefore, the default
        formatter is only kept if it was selected on the command line.
        """
        formats = list(config.format or [config.default_format])
        command_line_formats = cls.select_command_line_formats(config) or []
        if config.default_format in command_line_formats:
            return      # -- USER: Has selected behave's default formatter.
        console_indexes = LiveHost.select_console_format_indexes(config)
        live_indexes = [index for index in console_indexes
                        if LiveHost.is_live_format(formats[index])]
        if live_indexes:
            return      # -- USER: Has selected the "live" formatter.

        default_indexes = [index for index in console_indexes
                           if formats[index] == config.default_format]
        if len(console_indexes) == 1 and default_indexes:
            formats[default_indexes[0]] = LIVE_FORMAT
            config.format = formats

    def warn_if_jobs_are_used(self):
        jobs = getattr(self.config, "jobs", 1)
        if (isinstance(jobs, int) and jobs > 1
                and self.runner_class_name == DEFAULT_RUNNER_CLASS_NAME):
            sys.stderr.write("live-view: --jobs=%d is not supported, "
                             "the tests run sequentially.\n" % jobs)
