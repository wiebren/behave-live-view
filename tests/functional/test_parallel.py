# -*- coding: UTF-8 -*-
"""
Functional tests of the interactive view with a parallel test runner
(behave-parallel-runner): The features run in worker processes, the view
in the parent process.
"""

import pytest

from conftest import (
    needs_pseudo_terminal, run_behave, run_behave_in_terminal, STEPS_TEXT
)

pytest.importorskip("behave_parallel_runner")

PARALLEL_ARGS = ("--no-color --jobs=2 "
                 "-D live_view.runner=behave_parallel_runner:ParallelRunner")


@needs_pseudo_terminal
def test_view_shows_the_features_of_all_workers(workdir):
    testrun_done = lambda: workdir.read_calls().count("PASSES") == 4
    result = run_behave_in_terminal(workdir, PARALLEL_ARGS, [
        (testrun_done, ""),
        ("Bob", "q"),
    ])
    assert result.returncode == 1, result
    assert "q quit" in result.output            # -- VIEW: Was shown (footer).
    assert u"✔ Feature: Bob  1/1" in result.output
    assert u"✘ Feature: Alice  2/2 · 1 failed" in result.output
    # -- AFTER THE VIEW: Summary of the failures, then the merged summary.
    after_view = result.output[result.output.rindex("Failures:"):]
    assert "Alice › Scenario: A2  features/alice.feature:5" in after_view
    assert "ASSERT FAILED: XFAIL-STEP" in after_view
    assert "1 feature passed, 1 failed, 0 skipped" in after_view
    assert "Traceback" not in result.output
    # -- HINT: The workers send events, no plain status lines.
    assert u"✘ Feature: Alice  1/2" not in result.output


@needs_pseudo_terminal
def test_quitting_the_view_stops_the_workers(workdir):
    workdir.write_file("features/slow.feature", u"""
        Feature: Slow
          Scenario: S1
            Given I wait 60 seconds
            When a step passes
        """)
    step_is_running = lambda: "WAIT-STARTED" in workdir.read_calls()
    result = run_behave_in_terminal(
        workdir, PARALLEL_ARGS + " features/slow.feature", [
            (step_is_running, "q"),     # -- STOP: The test run.
            (lambda: True, "q"),        # -- QUIT: The view.
            (lambda: True, "q"),
        ], timeout=45.0)
    assert result.returncode == 1, result
    calls = workdir.read_calls()
    assert "WAIT-DONE" not in calls     # -- TERMINATED: Did not wait 60s.
    assert "PASSES" not in calls
    assert "Traceback" not in result.output


@needs_pseudo_terminal
def test_rerun_runs_in_new_workers_with_the_changed_step_file(workdir):
    def fix_the_failing_step():
        fixed_steps = STEPS_TEXT.replace('assert False, "XFAIL-STEP"',
                                         'pass  # -- FIXED')
        workdir.write_file("features/steps/steps.py", fixed_steps)

    first_run_done = lambda: workdir.read_calls().count("FAILS") == 1
    rerun_done = lambda: workdir.read_calls().count("FAILS") == 2
    result = run_behave_in_terminal(workdir, PARALLEL_ARGS, [
        (first_run_done, fix_the_failing_step),
        ("Bob", "R"),               # -- RERUN: All failures.
        (rerun_done, "q"),
    ])
    assert workdir.read_calls().count("FAILS") == 2, result
    assert result.returncode == 0, result
    assert "RERUN FAILED" not in result.output
    assert "Failures:" not in result.output


def test_without_terminal_workers_write_plain_status_lines(workdir):
    result = run_behave(workdir, PARALLEL_ARGS)
    assert result.returncode == 1, result
    assert u"✘ Feature: Alice  1/2 · 1 failed" in result.output
    assert u"✔ Feature: Bob  1/1" in result.output
    assert "1 feature passed, 1 failed, 0 skipped" in result.output
