# -*- coding: UTF-8 -*-
"""
Functional tests of "behave" WITHOUT a terminal (CI, pipes, output files):
The "live" formatter writes plain, append-only status lines then.

The first three tests are ported from "features/formatter.live.feature"
of the behave fork where this formatter was developed.
"""

from conftest import run_behave

RUNNER_NAME = "USING RUNNER: behave_live_view.runner:LiveRunner"
LIVE_FORMAT = "behave_live_view:LiveFormatter"


def write_feature_with_skipped_scenario(workdir):
    workdir.write_file("features/charly.feature", u"""
        Feature: Charly
          Scenario: C1
            Given a step passes
            When a step fails
            Then a step passes
          @skip
          Scenario: C2
            Given a step passes
        """)


# -----------------------------------------------------------------------------
# FORMATTER: Plain status lines
# -----------------------------------------------------------------------------
def test_passed_feature_is_shown_as_one_status_line(workdir):
    """Passed feature is shown as one status line"""
    result = run_behave(workdir, "--no-color features/bob.feature")
    assert result.returncode == 0, result
    assert RUNNER_NAME in result.output
    assert "Feature: Bob  1/1" in result.output
    assert "Scenario: B1" not in result.output


def test_failed_feature_is_expanded_down_to_the_failed_step(workdir):
    """Failed feature is expanded down to the failed step"""
    write_feature_with_skipped_scenario(workdir)
    result = run_behave(workdir, "--no-color --tags='not @skip' "
                                 "features/charly.feature")
    assert result.returncode != 0, result
    assert "Feature: Charly  0/2 · 1 failed · 1 skipped" in result.output
    assert "Scenario: C1" in result.output
    assert "When a step fails" in result.output
    assert "XFAIL-STEP" in result.output
    assert "Then a step passes" not in result.output


def test_live_formatter_can_write_to_an_output_file(workdir):
    """Live formatter can write to an output file"""
    result = run_behave(workdir, "-f %s -o live.output --no-color "
                                 "features/bob.feature" % LIVE_FORMAT)
    assert result.returncode == 0, result
    live_output = (workdir.path / "live.output").read_text(encoding="UTF-8")
    assert "Feature: Bob  1/1" in live_output


# -----------------------------------------------------------------------------
# RUNNER: Selects the formatter, runs the tests with the normal runner
# -----------------------------------------------------------------------------
def test_runner_is_all_that_needs_to_be_configured(workdir):
    # -- HINT: behave.ini contains only the "runner" setting.
    result = run_behave(workdir, "--no-color")
    assert result.returncode == 1, result
    assert "✘ Feature: Alice  1/2 · 1 failed" in result.output
    assert "✔ Feature: Bob  1/1" in result.output
    assert "1 feature passed, 1 failed, 0 skipped" in result.output


def test_another_console_formatter_is_respected(workdir):
    result = run_behave(workdir, "-f plain --no-color features/bob.feature")
    assert result.returncode == 0, result
    assert RUNNER_NAME in result.output
    assert "Scenario: B1" in result.output      # -- FORMATTER: plain
    assert "✔ Feature: Bob" not in result.output


def test_formatter_with_output_file_is_kept(workdir):
    result = run_behave(workdir, "-f json -o report.json --no-color "
                                 "features/bob.feature")
    assert result.returncode == 0, result
    assert (workdir.path / "report.json").read_text().lstrip().startswith("[")
    # -- HINT: behave adds its default formatter for the console in this
    # case only if formatters are specified in the config-file.
    assert "Traceback" not in result.output


def test_format_name_can_be_registered_in_config_file(workdir):
    workdir.write_file("behave.ini", u"""
        [behave]
        runner = behave_live_view:LiveRunner

        [behave.formatters]
        live = behave_live_view:LiveFormatter
        """)
    result = run_behave(workdir, "-f live --no-color features/bob.feature")
    assert result.returncode == 0, result
    assert "✔ Feature: Bob  1/1" in result.output


def test_formatter_works_without_the_runner(workdir):
    # -- HINT: Plain status lines only; the interactive view needs the runner.
    (workdir.path / "behave.ini").unlink()
    result = run_behave(workdir, "-f %s --no-color features/bob.feature"
                                 % LIVE_FORMAT)
    assert result.returncode == 0, result
    assert "USING RUNNER: behave.runner:Runner" in result.output
    assert "✔ Feature: Bob  1/1" in result.output


def test_dry_run_works(workdir):
    result = run_behave(workdir, "--dry-run --no-color features/bob.feature")
    assert result.returncode == 0, result
    assert "Traceback" not in result.output


def test_jobs_option_is_not_supported(workdir):
    result = run_behave(workdir, "--jobs=2 --no-color features/bob.feature")
    assert result.returncode == 0, result
    assert "live-view: --jobs=2 is not supported" in result.output
    assert "✔ Feature: Bob  1/1" in result.output


def test_another_runner_can_run_the_tests(workdir):
    workdir.write_file("my_runner.py", u"""
        from behave.runner import Runner

        class MyRunner(Runner):
            def run(self):
                print("MY-RUNNER: runs the tests")
                return super(MyRunner, self).run()
        """)
    result = run_behave(workdir, "-D live_view.runner=my_runner:MyRunner "
                                 "--no-color features/bob.feature")
    assert result.returncode == 0, result
    assert RUNNER_NAME in result.output
    assert "MY-RUNNER: runs the tests" in result.output
    assert "✔ Feature: Bob  1/1" in result.output


def test_undefined_steps_are_reported(workdir):
    workdir.write_file("features/undefined.feature", u"""
        Feature: Undefined
          Scenario: U1
            Given an unknown step is used
        """)
    result = run_behave(workdir, "--no-color features/undefined.feature")
    assert result.returncode != 0, result
    assert "You can implement step definitions for undefined steps" \
        in result.output
    assert "an unknown step is used" in result.output
