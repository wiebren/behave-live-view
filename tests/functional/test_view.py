# -*- coding: UTF-8 -*-
"""
Functional tests of the interactive view: "behave" runs in a pseudo-terminal
with :class:`behave_live_view.LiveRunner` as its test runner.

The view runs in the main thread and the test run in a background thread;
both are started by ``LiveRunner.run()`` (called by behave).
"""

from conftest import needs_pseudo_terminal, run_behave_in_terminal, STEPS_TEXT

pytestmark = needs_pseudo_terminal
RUNNER_NAME = "USING RUNNER: behave_live_view.runner:LiveRunner"


def test_view_shows_the_testrun_until_it_is_quit(workdir):
    testrun_done = lambda: workdir.read_calls().count("PASSES") == 4
    result = run_behave_in_terminal(workdir, "--no-color", [
        (testrun_done, ""),
        ("Bob", "q"),
    ])
    assert result.returncode == 1, result
    assert RUNNER_NAME in result.output
    assert "q quit" in result.output            # -- VIEW: Was shown (footer).
    # -- AFTER THE VIEW: Summary of the failures, then what behave has printed.
    after_view = result.output[result.output.rindex("Failures:"):]
    assert "Alice › Scenario: A2  features/alice.feature:5" in after_view
    assert "ASSERT FAILED: XFAIL-STEP" in after_view
    assert "1 feature passed, 1 failed, 0 skipped" in after_view
    assert "4 steps passed, 1 failed, 0 skipped" in after_view
    assert "Traceback" not in result.output


def test_passing_testrun_has_exit_status_zero(workdir):
    result = run_behave_in_terminal(
        workdir, "--no-color features/bob.feature", [("Bob", "q")])
    assert result.returncode == 0, result
    assert "1 feature passed, 0 failed, 0 skipped" in result.output
    assert "Failures:" not in result.output


def test_rerun_uses_the_changed_step_file(workdir):
    """Rerun of the failures in the same process: The step file is executed
    again, its step definitions must neither be ambiguous nor stay the old
    ones. The exit status reflects the final state.
    """
    def fix_the_failing_step():
        fixed_steps = STEPS_TEXT.replace('assert False, "XFAIL-STEP"',
                                         'pass  # -- FIXED')
        assert fixed_steps != STEPS_TEXT
        workdir.write_file("features/steps/steps.py", fixed_steps)

    first_run_done = lambda: workdir.read_calls().count("FAILS") == 1
    rerun_done = lambda: workdir.read_calls().count("FAILS") == 2
    result = run_behave_in_terminal(workdir, "--no-color", [
        (first_run_done, fix_the_failing_step),
        ("Bob", "R"),               # -- RERUN: All failures.
        (rerun_done, "q"),
    ])
    assert workdir.read_calls().count("FAILS") == 2, result
    assert result.returncode == 0, result
    assert "AmbiguousStep" not in result.output
    assert "RERUN FAILED" not in result.output
    assert "Failures:" not in result.output


def test_rerun_without_changes_keeps_the_failure(workdir):
    first_run_done = lambda: workdir.read_calls().count("FAILS") == 1
    rerun_done = lambda: workdir.read_calls().count("FAILS") == 2
    result = run_behave_in_terminal(workdir, "--no-color", [
        (first_run_done, ""),
        ("Bob", "R"),
        (rerun_done, "q"),
    ])
    assert workdir.read_calls().count("FAILS") == 2, result
    assert result.returncode == 1, result
    assert "AmbiguousStep" not in result.output
    assert "Failures:" in result.output


def test_quit_stops_a_running_testrun(workdir):
    """The test run is executed in a background thread: It gets no
    KeyboardInterrupt from a signal, the view must stop it.
    """
    workdir.write_file("features/slow.feature", u"""
        Feature: Slow
          Scenario: S1
            Given I wait 60 seconds
            When a step passes
        """)
    step_is_running = lambda: "WAIT-STARTED" in workdir.read_calls()
    result = run_behave_in_terminal(
        workdir, "--no-color features/slow.feature", [
            (step_is_running, "q"),     # -- STOP: The test run.
            (lambda: True, "q"),        # -- QUIT: The view.
            (lambda: True, "q"),
        ], timeout=45.0)
    assert result.returncode == 1, result
    calls = workdir.read_calls()
    assert "WAIT-DONE" not in calls     # -- INTERRUPTED: Did not wait 60s.
    assert "PASSES" not in calls        # -- NOT RUN: Step after it.
