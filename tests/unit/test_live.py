# -*- coding: UTF-8 -*-
"""
Unit tests for the "live" formatter (:mod:`behave_live_view`):
status tree, event generation, plain renderer and host selection.
"""

import io
import os
import pickle

import pytest

from behave.configuration import Configuration
from behave.formatter import _registry as formatter_registry
from behave.formatter.base import StreamOpener
from behave_live_view import LiveFormatter, model
from behave_live_view.formatter import LiveEventFormatter, \
    make_feature_outline
from behave_live_view.host import LiveHost
from behave_live_view.plain import PlainRenderer, format_duration
from behave.parser import parse_feature
from behave.runner import Context, ModelRunner
from behave.step_registry import StepRegistry


FEATURE_TEXT = u"""
Feature: Alice
  Background:
    Given a step passes

  Scenario: A1
    When a step passes

  Scenario: A2
    When a step fails
    Then a step passes

  Rule: R1
    Scenario Outline: O-<name>
      When a step passes

      Examples:
        | name |
        | x    |
        | y    |
"""


def make_config(command_args=None):
    return Configuration(command_args or [], load_config=False)


def make_outline_a():
    step = model.make_outline(model.KIND_STEP, 3, u"Given", u"a step", 3)
    scenario1 = model.make_outline(model.KIND_SCENARIO, "a.feature:2",
                                   u"Scenario", u"S1", 2, [step])
    scenario2 = model.make_outline(model.KIND_SCENARIO, "a.feature:5",
                                   u"Scenario", u"S2", 5, [dict(step)])
    return model.make_outline(model.KIND_FEATURE, "a.feature", u"Feature",
                              u"Alice", 1, [scenario1, scenario2])


def apply_events(status_tree, events):
    for event in events:
        status_tree.apply(event)


# -----------------------------------------------------------------------------
# STATUS TREE:
# -----------------------------------------------------------------------------
class TestStatusTree:
    def test_pending_files_become_feature_nodes(self):
        tree = model.StatusTree()
        update = tree.apply({"type": "testrun_started",
                             "files": ["a.feature", "./b.feature"]})
        assert list(tree.features) == ["a.feature", "b.feature"]
        assert [node.state for node in update.added] == [model.PENDING] * 2
        assert tree.features["a.feature"].name == "a.feature"

    def test_feature_started_replaces_pending_file_node(self):
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "files": ["a.feature"]})
        update = tree.apply({"type": "feature_started",
                             "feature": make_outline_a()})
        feature = tree.features["a.feature"]
        assert feature.name == u"Alice"
        assert feature.state == model.RUNNING
        assert [node.name for node in feature.scenarios()] == [u"S1", u"S2"]
        assert update.rebuilt == [feature]
        assert len(tree.features) == 1

    def test_scenario_lifecycle_updates_ancestors(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        feature = tree.features["a.feature"]
        scenario = feature.scenarios()[0]
        update = tree.apply({"type": "scenario_started",
                             "filename": "a.feature", "key": "a.feature:2"})
        assert scenario.state == model.RUNNING
        assert scenario in update.changed and feature in update.changed

        tree.apply({"type": "step_finished", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3, "status": "passed",
                    "duration": 0.5, "error": None})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "passed",
                    "duration": 0.5})
        assert scenario.state == model.PASSED
        assert scenario.children[0].state == model.PASSED
        assert feature.state == model.RUNNING   # -- S2 is still pending.
        assert feature.counts()["done"] == 1

    def test_failed_step_gets_error_details(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        update = tree.apply({
            "type": "step_finished", "filename": "a.feature",
            "key": "a.feature:2", "line": 3, "status": "failed",
            "duration": 0.1, "error": u"Assertion Failed: X\nline 2"})
        step = tree.features["a.feature"].scenarios()[0].children[0]
        assert step.state == model.FAILED
        assert [node.name for node in step.children] == \
               [u"Assertion Failed: X", u"line 2"]
        assert all(node.kind == model.KIND_DETAIL for node in update.added)
        assert all(node.state == model.FAILED for node in step.children)

    def test_feature_finished_finalizes_unannounced_scenarios(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "passed", "duration": 0})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "passed", "duration": 1.5,
                    "statuses": {"a.feature:5": "skipped"}})
        feature = tree.features["a.feature"]
        assert [node.state for node in feature.scenarios()] == \
               [model.PASSED, model.SKIPPED]
        assert feature.state == model.PASSED
        assert feature.duration == 1.5
        # -- STEPS: Of a skipped scenario are skipped, too.
        assert feature.scenarios()[1].children[0].state == model.SKIPPED

    def test_feature_with_hook_error_is_failed(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "hook_error", "duration": 0,
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "passed"}})
        assert tree.features["a.feature"].state == model.FAILED
        assert tree.failed_nodes() == [tree.features["a.feature"]]

    def test_testrun_finished_marks_not_run_nodes_as_untested(self):
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "files": ["b.feature"]})
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "testrun_finished"})
        assert tree.finished
        assert tree.features["b.feature"].state == model.UNTESTED
        assert tree.features["a.feature"].state == model.UNTESTED
        assert all(node.state == model.UNTESTED
                   for node in tree.features["a.feature"].scenarios())

    def test_scenario_counts_are_kept_up_to_date(self):
        # -- HINT: Counts are cached per node (no tree walk per event).
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        feature = tree.features["a.feature"]
        assert feature.counts()[model.PENDING] == 2
        tree.apply({"type": "scenario_started", "filename": "a.feature",
                    "key": "a.feature:2"})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:5", "status": "failed", "duration": 0})
        for node in (feature, tree.root):
            counts = node.counts()
            assert (counts[model.RUNNING], counts[model.FAILED],
                    counts[model.PENDING]) == (1, 1, 0)
            assert (counts["total"], counts["done"]) == (2, 1)
        assert tree.counts() == tree.root.counts()

    def test_running_step_is_the_execution_point(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        scenario = tree.features["a.feature"].scenarios()[0]
        tree.apply({"type": "scenario_started", "filename": "a.feature",
                    "key": "a.feature:2"})
        assert tree.execution_point is scenario
        update = tree.apply({"type": "step_started", "filename": "a.feature",
                             "key": "a.feature:2", "line": 3})
        step = scenario.children[0]
        assert step.state == model.RUNNING and step in update.changed
        assert tree.execution_point is step
        tree.apply({"type": "step_finished", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3, "status": "passed"})
        assert tree.execution_point is scenario
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "passed", "duration": 0})
        assert tree.execution_point is None

    def test_passed_step_keeps_its_captured_output(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "step_finished", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3, "status": "passed",
                    "output": u"stdout:\nhello"})
        step = tree.features["a.feature"].scenarios()[0].children[0]
        assert [(node.keyword, node.name) for node in step.children] == [
            (model.DETAIL_OUTPUT, u"stdout:"), (model.DETAIL_OUTPUT, u"hello")]

    def test_failed_step_has_error_before_output(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "step_finished", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3, "status": "failed",
                    "error": u"XFAIL", "output": u"stdout:\nhello"})
        step = tree.features["a.feature"].scenarios()[0].children[0]
        assert [node.keyword for node in step.children] == [
            model.DETAIL_ERROR, model.DETAIL_OUTPUT, model.DETAIL_OUTPUT]

    def test_step_knows_where_it_is_written_and_defined(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        feature = tree.features["a.feature"]
        scenario = feature.scenarios()[0]
        step = scenario.children[0]
        assert feature.source_location == ("a.feature", 1)
        assert scenario.source_location == ("a.feature", 2)
        assert step.source_location == ("a.feature", 3)
        assert step.definition_location is None     # -- NOT RUN YET.
        assert tree.root.source_location is None

        tree.apply({"type": "step_started", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3,
                    "definition": "features/steps/x_steps.py:42"})
        assert step.definition_location == ("features/steps/x_steps.py", 42)
        assert scenario.definition_location is None

        # -- DETAIL LINE: Uses the locations of its step.
        tree.apply({"type": "step_finished", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3, "status": "failed",
                    "error": u"XFAIL"})
        detail = step.children[0]
        assert detail.source_location == ("a.feature", 3)
        assert detail.definition_location == ("features/steps/x_steps.py", 42)

    def test_undefined_step_has_no_definition_location(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "step_started", "filename": "a.feature",
                    "key": "a.feature:2", "line": 3, "definition": None})
        step = tree.features["a.feature"].scenarios()[0].children[0]
        assert step.definition_location is None

    def test_node_location_selects_what_to_rerun(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        feature = tree.features["a.feature"]
        scenario = feature.scenarios()[0]
        assert feature.location == "a.feature"
        assert scenario.location == "a.feature:2"
        assert scenario.children[0].location == "a.feature:2"
        assert tree.root.location is None

    def test_rerun_resets_the_selected_scenarios(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "step_finished", "filename": "a.feature",
                    "key": "a.feature:5", "line": 3, "status": "failed",
                    "error": u"XFAIL"})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "failed", "duration": 1,
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "failed"}})
        tree.apply({"type": "testrun_finished"})
        feature = tree.features["a.feature"]
        passed_scenario, failed_scenario = feature.scenarios()
        assert feature.state == model.FAILED and tree.finished

        update = tree.apply({"type": "rerun_started",
                             "locations": ["a.feature:5"]})
        assert not tree.finished
        assert failed_scenario.state == model.PENDING
        assert failed_scenario.children[0].state == model.PENDING
        assert failed_scenario.children[0].children == []
        assert passed_scenario.state == model.PASSED    # -- UNTOUCHED.
        assert update.rebuilt == [failed_scenario]
        assert feature.counts()[model.PENDING] == 1

        # -- RERUN: Only the selected scenario runs (other one: skipped).
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:5", "status": "passed", "duration": 0})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "passed", "duration": 1,
                    "statuses": {"a.feature:2": "skipped",
                                 "a.feature:5": "passed"}})
        assert [node.state for node in feature.scenarios()] == \
               [model.PASSED, model.PASSED]
        assert feature.state == model.PASSED
        assert tree.failed_nodes() == []

    def test_rerun_of_a_feature_resets_all_its_scenarios(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "output", "filename": "a.feature", "text": u"X"})
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "rerun_started", "locations": ["a.feature"]})
        feature = tree.features["a.feature"]
        assert [node.state for node in feature.scenarios()] == \
               [model.PENDING] * 2
        assert all(child.kind != model.KIND_DETAIL
                   for child in feature.children)

    def test_rerun_forgets_the_outline_of_a_changed_feature(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "failed", "duration": 1,
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "failed"}})
        tree.apply({"type": "testrun_finished"})
        feature = tree.features["a.feature"]

        update = tree.apply({"type": "rerun_started",
                             "locations": ["a.feature"],
                             "changed_features": ["a.feature"]})
        assert feature.children == [] and feature.state == model.PENDING
        assert feature in update.rebuilt
        assert tree.counts()["total"] == 0      # -- COUNTS: Are corrected.

        # -- CHANGED FEATURE FILE: Other lines, another scenario.
        step = model.make_outline(model.KIND_STEP, 4, u"Given", u"a step", 4)
        outline = model.make_outline(
            model.KIND_FEATURE, "a.feature", u"Feature", u"Alice v2", 1,
            [model.make_outline(model.KIND_SCENARIO, "a.feature:3",
                                u"Scenario", u"S1 moved", 3, [step])])
        tree.apply({"type": "feature_started", "feature": outline})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:3", "status": "passed", "duration": 0})
        tree.apply({"type": "scenario_started", "filename": "a.feature",
                    "key": "a.feature:5"})      # -- OLD KEY: Is ignored.
        assert [node.name for node in feature.scenarios()] == [u"S1 moved"]
        assert feature.name == u"Alice v2"
        assert tree.counts()[model.PASSED] == 1
        assert tree.counts()["total"] == 1

    def test_partial_rerun_keeps_the_result_of_the_other_scenarios(self):
        # -- REGRESSION: behave shows the scenarios that are not selected
        # as skipped. A failed one became "skipped": exit status 0.
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        for key, status in (("a.feature:2", "passed"), ("a.feature:5", "failed")):
            tree.apply({"type": "scenario_finished", "filename": "a.feature",
                        "key": key, "status": status, "duration": 0})
        tree.apply({"type": "testrun_finished"})

        tree.apply({"type": "rerun_started", "locations": ["a.feature:2"]})
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        for key, status in (("a.feature:2", "passed"), ("a.feature:5", "skipped")):
            tree.apply({"type": "scenario_started", "filename": "a.feature",
                        "key": key})
            tree.apply({"type": "step_started", "filename": "a.feature",
                        "key": key, "line": 3})
            tree.apply({"type": "step_finished", "filename": "a.feature",
                        "key": key, "line": 3, "status": status})
            tree.apply({"type": "scenario_finished", "filename": "a.feature",
                        "key": key, "status": status, "duration": 0})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "passed", "duration": 0,
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "skipped"}})
        tree.apply({"type": "testrun_finished"})
        feature = tree.features["a.feature"]
        assert [node.state for node in feature.scenarios()] == \
               [model.PASSED, model.FAILED]
        assert feature.state == model.FAILED
        assert [node.key for node in tree.failed_nodes()] == ["a.feature:5"]
        assert tree.execution_point is None

    def test_feature_result_fills_in_what_was_not_reported(self):
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "features": [make_outline_a()]})
        tree.apply({"type": "testrun_finished"})    # -- LIKE: Sequential run.
        feature = tree.features["a.feature"]
        assert feature.state == model.UNTESTED
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "skipped",
                    "statuses": {"a.feature:2": "skipped",
                                 "a.feature:5": "skipped"}})
        assert feature.state == model.SKIPPED
        assert [node.state for node in feature.scenarios()] == \
               [model.SKIPPED] * 2

    def test_feature_result_keeps_what_was_reported(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "failed", "duration": 0,
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "failed"}})
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "skipped"})
        assert tree.features["a.feature"].state == model.FAILED

    def test_early_feature_result_does_not_decide_about_scenarios(self):
        # -- PARALLEL RUN: The result of the runner may arrive before the
        # events of the worker. REGRESSION: A skipped scenario was shown as
        # untested (its later events were ignored).
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "passed"})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "passed", "duration": 0,
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "skipped"}})
        assert [node.state for node in
                tree.features["a.feature"].scenarios()] == \
               [model.PASSED, model.SKIPPED]

    def test_feature_result_of_a_rerun_does_not_speak_for_other_scenarios(
            self):
        # -- REGRESSION: Test run was stopped (a.feature:5 never ran). Then
        # a.feature:2 is run again; behave reports the other scenario as
        # skipped. It must stay "not run": exit status was 0 otherwise.
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "failed", "duration": 0})
        tree.apply({"type": "testrun_finished"})
        feature = tree.features["a.feature"]
        assert [node.state for node in feature.scenarios()] == \
               [model.FAILED, model.UNTESTED]

        tree.apply({"type": "rerun_started", "locations": ["a.feature:2"]})
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "passed", "duration": 0})
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "passed",
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "skipped"}})
        assert [node.state for node in feature.scenarios()] == \
               [model.PASSED, model.UNTESTED]
        assert tree.counts()[model.UNTESTED] == 1

    def test_feature_result_of_a_rerun_speaks_for_a_changed_feature(self):
        # -- CHANGED FEATURE FILE: All of it is run again, its scenarios are
        # new. One that is excluded (not reported) is skipped, not "not run".
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "rerun_started", "locations": ["a.feature"],
                    "changed_features": ["a.feature"]})
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "passed", "duration": 0})
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "passed",
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "skipped"}})
        assert [node.state for node in
                tree.features["a.feature"].scenarios()] == \
               [model.PASSED, model.SKIPPED]

    def test_feature_result_keeps_a_feature_failed_by_hook_error(self):
        # -- REGRESSION: Feature was shown as passed, its hook error was
        # missing in the summary of failures.
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "features": [make_outline_a()]})
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "scenario_finished", "filename": "a.feature",
                    "key": "a.feature:2", "status": "passed", "duration": 0})
        tree.apply({"type": "feature_finished", "filename": "a.feature",
                    "status": "hook_error", "duration": 0,
                    "statuses": {"a.feature:2": "passed"}})
        tree.apply({"type": "testrun_finished"})
        feature = tree.features["a.feature"]
        assert feature.state == model.FAILED
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "hook_error",
                    "statuses": {"a.feature:2": "passed",
                                 "a.feature:5": "skipped"}})
        assert feature.state == model.FAILED
        assert tree.failed_nodes() == [feature]

    def test_feature_result_of_a_stopped_run_stays_untested(self):
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "features": [make_outline_a()]})
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "untested",
                    "statuses": {"a.feature:2": "untested",
                                 "a.feature:5": "untested"}})
        assert tree.features["a.feature"].state == model.UNTESTED

    def test_feature_result_of_unknown_feature_sets_its_state(self):
        # -- PARALLEL RUN: Worker did not show the (excluded) feature.
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "files": ["a.feature"]})
        tree.apply({"type": "feature_result", "filename": "a.feature",
                    "status": "skipped"})
        tree.apply({"type": "testrun_finished"})
        assert tree.features["a.feature"].state == model.SKIPPED

    def test_absolute_and_relative_names_are_the_same_feature(
            self, monkeypatch, tmp_path):
        # -- REGRESSION: Parallel runner uses absolute names, the model of
        # behave relative ones -> each feature was shown twice.
        monkeypatch.setattr(model, "BASE_DIR", str(tmp_path))
        absolute_name = str(tmp_path / "features" / "a.feature")
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started", "files": [absolute_name]})
        outline = make_outline_a()
        outline["key"] = model.normalize_filename("features/a.feature")
        tree.apply({"type": "feature_started", "feature": outline})
        tree.apply({"type": "output", "filename": absolute_name, "text": u"X"})
        assert len(tree.features) == 1
        import os
        assert list(tree.features) == [os.path.join("features", "a.feature")]
        # -- OUTSIDE OF THE START DIRECTORY: Like behave's model does it.
        outside = str(tmp_path.parent / "other" / "b.feature")
        assert model.normalize_filename(outside) == \
               os.path.join(os.pardir, "other", "b.feature")
        assert model.normalize_filename("../other/b.feature") == \
               os.path.join(os.pardir, "other", "b.feature")

    @pytest.mark.parametrize("output_first", [True, False])
    def test_rerun_output_is_kept_in_any_event_order(self, output_first):
        # -- PARALLEL RUN: Output of a feature may arrive before it starts.
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        tree.apply({"type": "output", "filename": "a.feature",
                    "text": u"RUN-1"})
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "rerun_started", "locations": ["a.feature:2"]})
        events = [{"type": "output", "filename": "a.feature", "text": u"RUN-2"},
                  {"type": "feature_started", "feature": make_outline_a()}]
        if not output_first:
            events.reverse()
        for event in events:
            tree.apply(event)
        details = [child.name for child in tree.features["a.feature"].children
                   if child.kind == model.KIND_DETAIL]
        assert details == [u"RUN-2"]

    def test_output_is_attached_to_its_feature(self):
        tree = model.StatusTree()
        tree.apply({"type": "output", "filename": "a.feature",
                    "text": u"HOOK: hello\n\n"})
        tree.apply({"type": "output", "filename": None, "text": u"GLOBAL\n"})
        tree.apply({"type": "output", "filename": "a.feature", "text": u" \n"})
        feature = tree.features["a.feature"]
        assert [node.name for node in feature.children] == [u"HOOK: hello"]
        assert tree.output == [u"GLOBAL\n"]

    def test_details_survive_feature_outline(self):
        tree = model.StatusTree()
        tree.apply({"type": "output", "filename": "a.feature", "text": u"X"})
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        kinds = [node.kind for node in tree.features["a.feature"].children]
        assert kinds == [model.KIND_SCENARIO, model.KIND_SCENARIO,
                         model.KIND_DETAIL]

    @pytest.mark.parametrize("event", [
        {"type": "unknown"}, {},
        {"type": "scenario_started", "filename": "a.feature", "key": "?"},
        {"type": "step_finished", "filename": "a.feature", "key": "?",
         "line": 1, "status": "passed"},
    ])
    def test_odd_events_are_ignored(self, event):
        tree = model.StatusTree()
        tree.apply(event)   # -- SHOULD NOT RAISE.

    @pytest.mark.parametrize("status, state", [
        ("passed", model.PASSED), ("failed", model.FAILED),
        ("error", model.FAILED), ("hook_error", model.FAILED),
        ("undefined", model.FAILED), ("skipped", model.SKIPPED),
        ("untested", model.UNTESTED),
    ])
    def test_state_from_status(self, status, state):
        assert model.state_from_status(status) == state


# -----------------------------------------------------------------------------
# FORMATTER: Events of a real (model) test run
# -----------------------------------------------------------------------------
class EventCollector(LiveEventFormatter):
    def __init__(self, config):
        super(EventCollector, self).__init__(
            StreamOpener(stream=io.StringIO()), config)
        self.events = []

    def emit(self, event):
        self.events.append(event)


def run_feature_with(formatter, config, text=FEATURE_TEXT):
    step_registry = StepRegistry()
    step_registry.add_step_definition(
        "step", u"a step passes", lambda ctx: None)

    def step_fails(ctx):
        assert False, "XFAIL-STEP"

    step_registry.add_step_definition("step", u"a step fails", step_fails)
    feature = parse_feature(text.strip(), filename="features/alice.feature")
    runner = ModelRunner(config, features=[feature],
                         step_registry=step_registry)
    runner.formatters = [formatter]
    runner.context = Context(runner)
    runner.run_model()
    return feature


class TestLiveEventFormatter:
    @pytest.fixture
    def events(self):
        config = make_config()
        config.reporters = []
        formatter = EventCollector(config)
        run_feature_with(formatter, config)
        return formatter.events

    def test_events_are_picklable(self, events):
        assert pickle.loads(pickle.dumps(events)) == events

    def test_event_sequence(self, events):
        types = [event["type"] for event in events]
        assert types[0] == "feature_started"
        assert types[-1] == "feature_finished"
        assert types.count("scenario_started") == 4
        assert types.count("scenario_finished") == 4
        assert types.count("feature_finished") == 1

    def test_outline_describes_the_feature(self, events):
        outline = events[0]["feature"]
        assert outline["key"] == "features/alice.feature"
        assert outline["name"] == u"Alice"
        kinds = [child["kind"] for child in outline["children"]]
        assert kinds == [model.KIND_SCENARIO, model.KIND_SCENARIO,
                         model.KIND_RULE]
        rule = outline["children"][2]
        scenario_outline = rule["children"][0]
        assert scenario_outline["kind"] == model.KIND_OUTLINE
        assert len(scenario_outline["children"]) == 2
        # -- STEPS: Background steps are part of each scenario.
        steps = outline["children"][0]["children"]
        assert [step["name"] for step in steps] == [u"a step passes"] * 2

    def test_events_build_the_expected_status_tree(self, events):
        tree = model.StatusTree()
        apply_events(tree, events)
        feature = tree.features["features/alice.feature"]
        assert feature.state == model.FAILED
        states = dict((node.name, node.state) for node in feature.scenarios())
        assert states[u"A1"] == model.PASSED
        assert states[u"A2"] == model.FAILED
        assert list(states.values()).count(model.PASSED) == 3
        failed_scenario = tree.failed_nodes()[0]
        step_states = [step.state for step in failed_scenario.children]
        assert step_states == [model.PASSED, model.FAILED, model.UNTESTED]
        failed_step = failed_scenario.children[1]
        assert any(u"XFAIL-STEP" in node.name for node in failed_step.children)

    def test_step_definition_location_is_sent(self, events):
        definitions = [event["definition"] for event in events
                       if event["type"] == "step_started"]
        assert definitions and all(definitions)
        filename, _, line = definitions[0].rpartition(":")
        assert filename.endswith("test_live.py") and line.isdigit()

    def test_steps_are_announced_before_they_run(self, events):
        step_events = [(event["type"], event["line"]) for event in events
                       if event["type"].startswith("step_")
                       and event["key"].endswith(":5")]    # -- Scenario: A1
        assert step_events == [("step_started", 3), ("step_finished", 3),
                               ("step_started", 6), ("step_finished", 6)]

    def test_skipped_scenarios_are_reported_at_feature_end(self):
        config = make_config(["--tags=not @skip"])
        config.reporters = []
        formatter = EventCollector(config)
        text = u"""
            Feature: F
              Scenario: S1
                Given a step passes
              @skip
              Scenario: S2
                Given a step passes
            """
        run_feature_with(formatter, config, text)
        tree = model.StatusTree()
        apply_events(tree, formatter.events)
        feature = list(tree.features.values())[0]
        assert [node.state for node in feature.scenarios()] == \
               [model.PASSED, model.SKIPPED]
        assert feature.state == model.PASSED


# -----------------------------------------------------------------------------
# FORMATTER CLASS: Format name, plain mode
# -----------------------------------------------------------------------------
class TestLiveFormatter:
    def test_can_be_selected_by_its_scoped_class_name(self):
        from behave_live_view.runner import LIVE_FORMAT
        assert LIVE_FORMAT == "behave_live_view:LiveFormatter"
        assert formatter_registry.select_formatter_class(LIVE_FORMAT) \
            is LiveFormatter

    def test_writes_plain_status_lines_without_host(self):
        config = make_config(["--no-color"])
        config.reporters = []
        stream = io.StringIO()
        formatter = LiveFormatter(StreamOpener(stream=stream), config)
        assert formatter.host is None
        run_feature_with(formatter, config)
        output = stream.getvalue()
        assert u"✘ Feature: Alice  3/4 · 1 failed" in output
        assert u"features/alice.feature" in output
        assert u"  ✘ Scenario: A2\n    ✘ When a step fails\n" in output
        assert u"XFAIL-STEP" in output
        assert u"A1" not in output  # -- COMPACT: Passed scenarios are hidden.

    def test_shows_events_as_plain_status_lines(self):
        config = make_config(["--no-color"])
        stream = io.StringIO()
        formatter = LiveFormatter(StreamOpener(stream=stream), config)
        formatter.emit(
            {"type": "testrun_started", "files": ["a.feature", "b.feature"]})
        formatter.emit(
            {"type": "feature_started", "feature": make_outline_a()})
        formatter.emit(
            {"type": "feature_finished", "filename": "a.feature",
             "status": "passed", "duration": 0.25,
             "statuses": {"a.feature:2": "passed", "a.feature:5": "passed"}})
        formatter.close()
        lines = stream.getvalue().splitlines()
        assert lines[0].startswith(u"✔ Feature: Alice  2/2  0.25s")
        # -- NOT RUN: Feature is shown as untested at the end.
        assert lines[1] == u"○ b.feature"



class TestPlainRenderer:
    def test_format_duration(self):
        assert format_duration(0.123) == u"0.12s"
        assert format_duration(None) == u"0.00s"
        assert format_duration(62.4) == u"1m 02s"

    def test_global_output_is_written(self):
        stream = io.StringIO()
        renderer = PlainRenderer(stream, make_config(["--no-color"]))
        renderer.process_event({"type": "output", "filename": None,
                                "text": u"HOOK: WORKER-STARTED\n"})
        assert stream.getvalue() == u"HOOK: WORKER-STARTED\n"

    def test_no_color_without_terminal(self):
        stream = io.StringIO()
        renderer = PlainRenderer(stream, make_config())
        assert renderer.colored is False


# -----------------------------------------------------------------------------
# HOST SELECTION:
# -----------------------------------------------------------------------------
class TestLiveHostSelection:
    def test_no_host_without_terminal(self, monkeypatch):
        from behave_live_view import host as host_module
        monkeypatch.setattr(host_module, "has_terminal", lambda: False)
        assert LiveHost.make_for(make_config(["-f", "live"])) is None

    def test_no_host_without_textual(self, monkeypatch, capsys):
        from behave_live_view import host as host_module
        monkeypatch.setattr(host_module, "has_terminal", lambda: True)
        monkeypatch.setattr(host_module, "load_app_class", lambda: None)
        assert LiveHost.make_for(make_config(["-f", "live"])) is None
        assert "textual" in capsys.readouterr().err

    def test_host_with_terminal_and_app(self, monkeypatch):
        from behave_live_view import host as host_module
        monkeypatch.setattr(host_module, "has_terminal", lambda: True)
        monkeypatch.setattr(host_module, "load_app_class", lambda: object)
        assert LiveHost.make_for(make_config(["-f", "live"])) is not None

    @pytest.mark.parametrize("command_args", [
        ["-f", "live", "-f", "plain"],              # -- 2 console formatters.
        ["-f", "live", "-o", "live.txt"],           # -- Output file.
        # -- REGRESSION: "live" goes to a file, "plain" is on the console.
        ["-f", "live", "-o", "live.txt", "-f", "plain"],
        ["-f", "plain"],
        ["-f", "live", "--dry-run"],
    ])
    def test_no_host_if_view_cannot_own_the_console(self, monkeypatch,
                                                    command_args, tmp_path):
        from behave_live_view import host as host_module
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(host_module, "has_terminal", lambda: True)
        monkeypatch.setattr(host_module, "load_app_class", lambda: object)
        assert LiveHost.make_for(make_config(command_args)) is None



# -----------------------------------------------------------------------------
# HOST: Runs the test run in a thread, the view in the main thread
# -----------------------------------------------------------------------------
class FakeApp:
    """Fake of the interactive view: Polls like the real one (timer)."""
    quit_when_done = True

    def __init__(self, on_interrupt=None, on_rerun=None,
                 on_check_changes=None, on_open=None, on_copy=None):
        import threading
        self.on_open = on_open
        self.on_copy = on_copy
        self.on_interrupt = on_interrupt
        self.on_rerun = on_rerun
        self.on_check_changes = on_check_changes
        self.status_tree = model.StatusTree()
        self.events = []
        self.done = []
        self.captured_output = []
        self.interval_callbacks = []
        self._exit = threading.Event()

    def set_interval(self, interval, callback):
        self.interval_callbacks.append(callback)

    def on_mount(self):
        pass

    def handle_event(self, event):
        self.events.append(event)

    def testrun_done(self, failed):
        self.done.append(failed)
        if self.quit_when_done:
            self.exit()

    def exit(self):
        self._exit.set()

    def mount(self):
        # -- LIKE TEXTUAL: on_mount() handlers of all classes are called.
        for cls in type(self).__mro__:
            on_mount = cls.__dict__.get("on_mount")
            if on_mount is not None:
                on_mount(self)
        self.tick()

    def tick(self):
        for callback in self.interval_callbacks:
            callback()

    def run(self):
        self.mount()
        while not self._exit.wait(0.005):
            self.tick()


def make_failure_events(error=u"ASSERT FAILED: XFAIL-HERE"):
    """Events of a test run with one failed scenario (a.feature:5)."""
    return [
        {"type": "feature_started", "feature": make_outline_a()},
        {"type": "step_finished", "filename": "a.feature",
         "key": "a.feature:5", "line": 3, "status": "failed", "error": error},
        {"type": "scenario_finished", "filename": "a.feature",
         "key": "a.feature:5", "status": "failed", "duration": 0},
    ]


class FakeRunner:
    def __init__(self, host, failed=False, error=None, features=None,
                 events=None):
        import threading
        self.host = host
        self.events = events or []
        self.failed = failed
        self.error = error
        self.features = features or []
        self.interrupted = threading.Event()
        self.thread_name = None

    def interrupt(self):
        self.interrupted.set()

    def run(self):
        import threading
        self.thread_name = threading.current_thread().name
        self.host.post_event({"type": "testrun_started", "files": ["x"]})
        for event in self.events:
            self.host.post_event(event)
        print("SUMMARY: printed while the view is shown")
        if self.error is not None:
            raise self.error    # pylint: disable=raising-bad-type
        return self.failed


class TestLiveHost:
    @pytest.fixture(autouse=True)
    def without_terminal_guard(self, monkeypatch):
        from behave_live_view.host import TerminalGuard
        monkeypatch.setattr(TerminalGuard, "is_supported",
                            staticmethod(lambda: False))

    def test_runs_testrun_in_background_thread(self):
        host = LiveHost(make_config(), FakeApp)
        runner = FakeRunner(host, failed=True)
        assert host.run(runner) is True
        assert runner.thread_name == "behave-testrun"
        assert host.app.events == [{"type": "testrun_started", "files": ["x"]}]
        assert host.app.done == [True]
        assert LiveHost.current is None

    def test_host_is_current_while_testrun_is_active(self):
        host = LiveHost(make_config(), FakeApp)
        seen = []
        runner = FakeRunner(host)
        runner.run = lambda: seen.append(LiveHost.current) or False
        host.run(runner)
        assert seen == [host]

    def test_parsed_features_are_announced_as_pending(self):
        feature = parse_feature(FEATURE_TEXT.strip(), filename="alice.feature")
        host = LiveHost(make_config(), FakeApp)
        host.run(FakeRunner(host, features=[feature]))
        first_event = host.app.events[0]
        assert first_event["type"] == "testrun_started"
        assert first_event["features"] == [make_feature_outline(feature)]

    def test_testrun_error_closes_view_and_is_raised(self):
        host = LiveHost(make_config(), FakeApp)
        with pytest.raises(RuntimeError, match="XFAIL-CRASH"):
            host.run(FakeRunner(host, error=RuntimeError("XFAIL-CRASH")))
        assert host.app.done == []

    def test_quitting_the_view_interrupts_the_testrun(self):
        import threading

        class QuittingApp(FakeApp):
            def run(self):
                self.mount()
                started.wait(5)     # -- USER QUITS: While test run is active.

        started = threading.Event()
        host = LiveHost(make_config(), QuittingApp)
        runner = FakeRunner(host)

        def run_until_interrupted():
            started.set()
            assert runner.interrupted.wait(5)
            return True

        runner.run = run_until_interrupted
        assert host.run(runner) is True
        assert runner.interrupted.is_set()

    def test_interrupt_raises_keyboard_interrupt_in_testrun_thread(self):
        # -- LIKE A NORMAL RUN: The code that runs now gets the interrupt
        # (a step fails with it), not only a flag for the steps that follow.
        import threading
        import time

        class SequentialRunner:
            def __init__(self):
                self.reason = None
                self.interrupted_in_step = False
                self.step_started = threading.Event()

            def abort(self, reason=None):
                self.reason = reason

            def run(self):
                try:
                    self.step_started.set()
                    for _ in range(500):
                        time.sleep(0.01)
                except KeyboardInterrupt:
                    self.interrupted_in_step = True
                return True

        class InterruptingApp(FakeApp):
            def run(self):
                self.mount()
                assert host.runner.step_started.wait(5)
                self.on_interrupt()
                while not self._exit.wait(0.005):
                    self.tick()

        host = LiveHost(make_config(), InterruptingApp)
        runner = SequentialRunner()
        assert host.run(runner) is True
        assert runner.reason == "KeyboardInterrupt"
        assert runner.interrupted_in_step

    def test_interrupt_outside_of_a_step_fails_the_testrun(self):
        host = LiveHost(make_config(), FakeApp)
        runner = FakeRunner(host, error=KeyboardInterrupt())
        assert host.run(runner) is True     # -- NOT RAISED.

    def test_final_failures_fail_the_testrun(self):
        # -- HINT: The final state counts, not only the result of the last
        # run (a partial rerun may have passed).
        host = LiveHost(make_config(), FakeApp)
        runner = FakeRunner(host, failed=False, events=make_failure_events())
        assert host.run(runner) is True

    def test_rerun_starts_a_new_testrun_with_the_locations(self, monkeypatch):
        from behave_live_view import host as host_module
        resets = []
        monkeypatch.setattr(host_module,
                            "forget_reloadable_step_definitions",
                            lambda: resets.append(True))

        class RerunningApp(FakeApp):
            quit_when_done = False

            def testrun_done(self, failed):
                super(RerunningApp, self).testrun_done(failed)
                if len(self.done) == 1:
                    # -- USER: Reruns the failures (key: R).
                    assert self.on_rerun(["a.feature:5", None]) is True
                else:
                    self.exit()

        runners = []

        def make_runner():
            runners.append(FakeRunner(host, failed=False))
            return runners[-1]

        host = LiveHost(make_config(), RerunningApp)
        first_runner = FakeRunner(host, failed=True)
        failed = host.run(first_runner, make_runner=make_runner)
        assert failed is False      # -- RERUN PASSED: Final state counts.
        assert host.app.done == [True, False]
        assert len(runners) == 1 and resets == [True]
        assert host.config.paths == ["a.feature:5"]
        assert {"type": "rerun_started", "locations": ["a.feature:5"],
                "changed_features": []} in host.app.events

    def test_rerun_with_reload_forgets_the_project_modules(self, monkeypatch):
        from behave_live_view import host as host_module
        monkeypatch.setattr(host_module,
                            "forget_reloadable_step_definitions", lambda: 0)
        reloads = []
        host = LiveHost(make_config(), FakeApp)
        host.app = FakeApp()
        host.make_runner = lambda: FakeRunner(host)
        monkeypatch.setattr(host.source_watcher, "reload_project_modules",
                            lambda: reloads.append(True))
        assert host.rerun(["a.feature"], reload=False) is True
        host._testrun_thread.join()
        assert reloads == []
        assert host.rerun(["a.feature"], reload=True) is True
        host._testrun_thread.join()
        assert reloads == [True]

    def test_rerun_runs_all_of_a_changed_feature_file(self, monkeypatch,
                                                      tmp_path):
        from behave_live_view import host as host_module
        monkeypatch.setattr(host_module,
                            "forget_reloadable_step_definitions", lambda: 0)
        monkeypatch.chdir(tmp_path)
        for name in ("a.feature", "b.feature"):
            (tmp_path / name).write_text(u"Feature: X\n")
        host = LiveHost(make_config(), FakeApp)
        host.app = FakeApp()
        host.make_runner = lambda: FakeRunner(host)
        for name in ("a.feature", "b.feature"):
            host.post_event({"type": "feature_started",
                             "feature": {"key": name}})
        import os
        import time
        future = time.time() + 10
        os.utime("a.feature", (future, future))

        assert host.check_changes(["a.feature:5", None])["features"] == \
               ["a.feature"]
        host.rerun(["a.feature:2", "a.feature:5", "b.feature:3"])
        host._testrun_thread.join()
        assert host.config.paths == ["a.feature", "b.feature:3"]
        events = []
        while not host._events.empty():
            events.append(host._events.get())
        rerun_events = [event for event in events
                        if isinstance(event, dict)
                        and event.get("type") == "rerun_started"]
        assert rerun_events == [{"type": "rerun_started",
                                 "locations": ["a.feature", "b.feature:3"],
                                 "changed_features": ["a.feature"]}]

    def test_rerun_is_refused_while_a_testrun_is_active(self):
        import threading
        answers = []

        class ImpatientApp(FakeApp):
            def run(self):
                self.mount()
                started.wait(5)
                answers.append(self.on_rerun(["a.feature"]))
                release.set()
                while not self._exit.wait(0.005):
                    self.tick()

        started, release = threading.Event(), threading.Event()
        host = LiveHost(make_config(), ImpatientApp)
        runner = FakeRunner(host)
        runner.run = lambda: started.set() or release.wait(5) and False
        host.run(runner, make_runner=lambda: None)
        assert answers == [False]

    def test_open_in_editor_starts_a_gui_editor(self, monkeypatch, tmp_path):
        from behave_live_view import editor as editor_module
        opened = []
        monkeypatch.setenv("BEHAVE_EDITOR", "code")
        monkeypatch.setattr(
            editor_module, "open_in_editor",
            lambda filename, line, **kwargs: opened.append(
                (filename, line, kwargs)))
        host = LiveHost(make_config(), FakeApp)
        assert host.open_in_editor("a.feature", 3) is None
        import os
        assert opened == [(os.path.join(host.base_dir, "a.feature"), 3,
                           {"command": "code"})]

    def test_open_in_editor_suspends_the_view_for_a_terminal_editor(
            self, monkeypatch):
        import contextlib
        import sys
        from behave_live_view import editor as editor_module
        steps = []

        class FakeDriver:
            can_suspend = True

            @staticmethod
            def suspend_application_mode():
                steps.append("suspend")

            @staticmethod
            def resume_application_mode():
                steps.append("resume")

            @staticmethod
            @contextlib.contextmanager
            def no_automatic_restart():
                yield

        class SuspendableApp(FakeApp):
            _driver = FakeDriver()

            def _suspend_signal(self):
                pass

            def _resume_signal(self):
                pass

            def refresh(self, **kwargs):
                steps.append("refresh")

        def fake_open_in_editor(filename, line, **kwargs):
            # -- HINT: Streams of the test run must not be touched.
            steps.append(("edit", sys.stdout is kept_stream))

        monkeypatch.setenv("BEHAVE_EDITOR", "vim")
        monkeypatch.setattr(editor_module, "open_in_editor",
                            fake_open_in_editor)
        kept_stream = object()
        monkeypatch.setattr(sys, "stdout", kept_stream)
        host = LiveHost(make_config(), SuspendableApp)
        host.app = SuspendableApp()
        assert host.open_in_editor("a.feature", 3) is None
        assert steps == ["suspend", ("edit", True), "resume", "refresh"]
        assert sys.stdout is kept_stream

    def test_terminal_editor_needs_a_view_that_can_be_suspended(
            self, monkeypatch):
        monkeypatch.setenv("BEHAVE_EDITOR", "vim")
        host = LiveHost(make_config(), FakeApp)
        host.app = FakeApp()        # -- WITHOUT: driver
        message = host.open_in_editor("a.feature", 3)
        assert "editor with its own window" in message

    def test_open_in_editor_uses_the_configured_editor(self, monkeypatch):
        from behave_live_view import editor as editor_module
        opened = []
        monkeypatch.setenv("BEHAVE_EDITOR", "vim")
        monkeypatch.setattr(
            editor_module, "open_in_editor",
            lambda filename, line, **kwargs: opened.append(kwargs["command"]))
        config = make_config(["-D", "live.editor=subl"])
        host = LiveHost(config, FakeApp)
        host.open_in_editor("a.feature", 3)
        assert opened == ["subl"]

    def test_open_in_editor_returns_the_problem_as_message(self, monkeypatch):
        for name in ("BEHAVE_EDITOR", "VISUAL", "EDITOR", "TERM_PROGRAM",
                     "TERMINAL_EMULATOR"):
            monkeypatch.delenv(name, raising=False)
        host = LiveHost(make_config(), FakeApp)
        assert "BEHAVE_EDITOR" in host.open_in_editor("a.feature", 3)

    def test_rerun_that_cannot_run_keeps_the_view(self, monkeypatch, capsys):
        # -- LIKE: Syntax error in a feature/step file that was just edited.
        from behave_live_view import host as host_module
        monkeypatch.setattr(host_module,
                            "forget_reloadable_step_definitions", lambda: 0)
        notices = []

        class RerunningApp(FakeApp):
            quit_when_done = False

            def notify(self, text, **kwargs):
                notices.append((text, kwargs.get("severity")))

            def testrun_done(self, failed):
                super(RerunningApp, self).testrun_done(failed)
                if len(self.done) == 1:
                    assert self.on_rerun(["a.feature"]) is True
                else:
                    self.exit()

        host = LiveHost(make_config(), RerunningApp)
        broken_runner = FakeRunner(host, error=SyntaxError("XFAIL-SYNTAX"))
        failed = host.run(FakeRunner(host, failed=False),
                          make_runner=lambda: broken_runner)
        assert failed is True           # -- NOT RAISED: View was kept.
        assert host.app.done == [False, True]
        assert notices == [("Rerun failed: SyntaxError: XFAIL-SYNTAX",
                            "error")]
        # -- HINT: Kept output is written to stdout when the view is closed.
        assert "RERUN FAILED: SyntaxError" in capsys.readouterr().out

    def test_feature_results_are_reported_when_the_testrun_ends(self):
        # -- CASE: Feature is excluded by tags and --no-skipped is used.
        # It is never shown to the formatter, but it is not "not run".
        feature = parse_feature(FEATURE_TEXT.strip(), filename="alice.feature")
        feature.mark_skipped()
        host = LiveHost(make_config(), FakeApp)
        host.run(FakeRunner(host, features=[feature]))
        results = [event for event in host.app.events
                   if event["type"] == "feature_result"]
        assert len(results) == 1
        assert results[0]["filename"] == "alice.feature"
        assert results[0]["status"] == "skipped"
        assert set(results[0]["statuses"].values()) == {"skipped"}

    def test_excluded_feature_does_not_fail_the_testrun(self):
        # -- REGRESSION: Exit status 1 for a passing test run.
        feature = parse_feature(FEATURE_TEXT.strip(), filename="alice.feature")
        feature.mark_skipped()
        host = LiveHost(make_config(), FakeApp)
        runner = FakeRunner(host, failed=False, features=[feature],
                            events=[{"type": "testrun_finished"}])
        assert host.run(runner) is False
        counts = host.status_tree.counts()
        assert counts["skipped"] == counts["total"] > 0

    def test_final_state_does_not_depend_on_the_view(self, capsys):
        # -- CASE: View is quit before it has seen all events (or it
        # ignores them because it is closing).
        class BlindApp(FakeApp):
            def handle_event(self, event):
                pass

        host = LiveHost(make_config(["--no-color"]), BlindApp)
        runner = FakeRunner(host, failed=False, events=make_failure_events())
        assert host.run(runner) is True
        assert u"Failures:" in capsys.readouterr().out

    def test_ctrl_c_while_waiting_for_the_testrun_does_not_escape(self):
        # -- REGRESSION: During the first seconds (before the message).
        class FakeThread:
            @staticmethod
            def is_alive():
                return True

            @staticmethod
            def join(timeout=None):
                raise KeyboardInterrupt()

        host = LiveHost(make_config(), FakeApp)
        host._testrun_thread = FakeThread()
        host.runner = FakeRunner(host)
        host._wait_for_testrun(None)    # -- SHOULD NOT RAISE.
        assert host.failed is True and host._gave_up

    def test_not_run_scenarios_fail_the_testrun(self):
        # -- CASE: Test run was stopped, then the failures were run again
        # and passed. The scenarios that never ran must not be forgotten.
        events = [
            {"type": "feature_started", "feature": make_outline_a()},
            {"type": "scenario_finished", "filename": "a.feature",
             "key": "a.feature:2", "status": "passed", "duration": 0},
            {"type": "testrun_finished"},
        ]
        host = LiveHost(make_config(), FakeApp)
        assert host.run(FakeRunner(host, failed=False, events=events)) is True

    def test_user_can_give_up_waiting_for_a_blocked_step(self, monkeypatch):
        import threading
        release = threading.Event()
        messages = []

        class QuittingApp(FakeApp):
            def run(self):
                self.mount()
                started.wait(5)     # -- USER QUITS: While a step blocks.

        class FakeThread:
            joins = 0

            @staticmethod
            def is_alive():
                return True

            def join(self, timeout=None):
                FakeThread.joins += 1
                if FakeThread.joins >= 3:
                    raise KeyboardInterrupt()   # -- USER: Gives up.

        class FakeGuard:
            class terminal_file:
                write = staticmethod(messages.append)
                flush = staticmethod(lambda: None)

        started = threading.Event()
        host = LiveHost(make_config(), QuittingApp)
        host._testrun_thread = FakeThread()
        host.runner = FakeRunner(host)
        host._wait_for_testrun(FakeGuard())
        release.set()
        assert host.failed is True and host._gave_up
        assert "ctrl+c to give up" in messages[0]

    def test_rerun_is_refused_without_runner_factory(self):
        host = LiveHost(make_config(), FakeApp)
        assert host.rerun(["a.feature"]) is False

    def test_captured_output_is_written_after_the_view(self, capsys):
        class PrintCapturingApp(FakeApp):
            def testrun_done(self, failed):
                self.captured_output.append((u"SUMMARY\n", False))
                self.captured_output.append((u"WARNING\n", True))
                super(PrintCapturingApp, self).testrun_done(failed)

        host = LiveHost(make_config(), PrintCapturingApp)
        host.run(FakeRunner(host))
        captured = capsys.readouterr()
        assert "SUMMARY\n" in captured.out
        assert "WARNING\n" in captured.err


@pytest.mark.skipif(not __import__("os").name == "posix", reason="POSIX only")
def test_terminal_guard_keeps_output_written_to_file_descriptors(capfd):
    import os
    import sys
    from behave_live_view.host import TerminalGuard
    original_streams = (sys.__stdout__, sys.__stderr__)
    guard = TerminalGuard()
    with guard:
        os.write(1, b"OUT: from fd 1\n")
        os.write(2, b"ERR: from fd 2\n")
        assert sys.__stdout__ is guard.terminal_file
        guard.terminal_file.write(u"VIEW")     # -- GOES TO: Real terminal.
    assert (sys.__stdout__, sys.__stderr__) == original_streams
    assert guard.read_output() == u"OUT: from fd 1\nERR: from fd 2\n"
    captured = capfd.readouterr()
    assert "OUT: from fd 1" not in captured.out
    assert "VIEW" in captured.err


@pytest.mark.skipif(not __import__("os").name == "posix", reason="POSIX only")
def test_terminal_guard_keeps_text_in_output_order(capfd):
    # -- HINT: print() output that the view captures and output that
    # bypasses it (file descriptors) must be written in their order.
    import os
    from behave_live_view.host import TerminalGuard
    guard = TerminalGuard()
    assert guard.keep_text(u"TOO EARLY") is False
    with guard:
        os.write(1, b"1: summary of first run\n")
        assert guard.keep_text(u"2: summary of rerun\n") is True
        os.write(1, b"3: more\n")
    assert guard.read_output() == \
        u"1: summary of first run\n2: summary of rerun\n3: more\n"
    capfd.readouterr()


class TestLiveHostOutput:
    @pytest.fixture(autouse=True)
    def without_terminal_guard(self, monkeypatch):
        from behave_live_view.host import TerminalGuard
        monkeypatch.setattr(TerminalGuard, "is_supported",
                            staticmethod(lambda: False))

    def test_testrun_starts_when_the_view_is_ready(self):
        # -- HINT: The test run captures output by replacing sys.stdout.
        # It must find the stream of the host there (not one of the view).
        import sys
        from behave_live_view.host import KeptOutputStream
        seen = []

        class CheckingApp(FakeApp):
            def mount(self):
                seen.append(("mount", host._testrun_thread))
                super(CheckingApp, self).mount()

        host = LiveHost(make_config(), CheckingApp)
        runner = FakeRunner(host)
        runner.run = lambda: seen.append(("run", type(sys.stdout))) or False
        original_stdout = sys.stdout
        host.run(runner)
        assert seen == [("mount", None), ("run", KeptOutputStream)]
        assert sys.stdout is original_stdout

    def test_output_of_testrun_is_written_after_the_view(self, capsys):
        host = LiveHost(make_config(), FakeApp)
        host.run(FakeRunner(host))
        assert "SUMMARY: printed while the view is shown" \
            in capsys.readouterr().out

    def test_failures_are_written_when_the_view_is_closed(self, capsys):
        # -- HINT: The details of a failure are gone with the view.
        host = LiveHost(make_config(["--no-color"]), FakeApp)
        host.run(FakeRunner(host, failed=True, events=make_failure_events()))
        output = capsys.readouterr().out
        assert u"Failures:" in output
        assert u"Alice \u203a Scenario: S2  a.feature:5" in output
        assert u"ASSERT FAILED: XFAIL-HERE" in output
        # -- ORDER: Failures first, then what was printed meanwhile.
        assert output.index(u"Failures:") < output.index(u"SUMMARY: printed")

    def test_nothing_is_written_about_failures_without_failures(self, capsys):
        host = LiveHost(make_config(), FakeApp)
        host.run(FakeRunner(host))
        assert u"Failures:" not in capsys.readouterr().out

    def test_view_gets_the_clipboard_support(self):
        from behave_live_view import clipboard
        host = LiveHost(make_config(), FakeApp)
        host.run(FakeRunner(host))
        assert host.app.on_copy is clipboard.copy_to_clipboard

    def test_testrun_runs_without_view_if_view_raises_on_start(self, capsys):
        class BrokenApp(FakeApp):
            def run(self):
                raise RuntimeError("XFAIL-VIEW")

        host = LiveHost(make_config(), BrokenApp)
        runner = FakeRunner(host, failed=False)
        assert host.run(runner) is False
        assert runner.thread_name == "MainThread"
        assert "XFAIL-VIEW" in capsys.readouterr().err

    def test_testrun_is_not_run_if_view_start_is_interrupted(self):
        class InterruptedApp(FakeApp):
            def run(self):
                raise KeyboardInterrupt()

        host = LiveHost(make_config(), InterruptedApp)
        runner = FakeRunner(host, failed=False)
        assert host.run(runner) is True
        assert runner.thread_name is None   # -- NOT RUN.

    def test_testrun_runs_without_view_if_view_cannot_start(self):
        class BrokenApp(FakeApp):
            def run(self):
                pass    # -- ENDS: Before it was mounted.

        host = LiveHost(make_config(), BrokenApp)
        assert host.run(FakeRunner(host, failed=True)) is True


class TestOutputText:
    def test_section_headers_are_compact(self):
        from behave_live_view.formatter import make_output_text
        text = u"""----
CAPTURED STDOUT: before_scenario
HOOK: hello
----
CAPTURED STDOUT: step
    indented line
CAPTURED STDERR: step
CAPTURED LOG: step
LOG_INFO:demo: message
----"""
        assert make_output_text(text).splitlines() == [
            u"before_scenario stdout:", u"HOOK: hello",
            u"stdout:", u"    indented line",
            u"log:", u"LOG_INFO:demo: message"]

    def test_error_message_is_not_repeated(self):
        from behave_live_view.formatter import make_output_text
        text = u"CAPTURED STDERR: step\nASSERT FAILED: X\n"
        assert make_output_text(text, u"ASSERT FAILED: X") is None

    @pytest.mark.parametrize("line, expected", [
        (u"\x1b[32mgreen\x1b[0m text", u"green text"),
        (u"\x1b]0;title\x07after", u"after"),
        (u"a\tb", u"a   b"),
        (u"bell\x07 and null\x00", u"bell and null"),
        (u"    keeps indentation", u"    keeps indentation"),
        (u"[INFO] keeps brackets", u"[INFO] keeps brackets"),
    ])
    def test_clean_text_line(self, line, expected):
        assert model.clean_text_line(line) == expected

    def test_details_use_the_last_part_of_a_progress_line(self):
        tree = model.StatusTree()
        tree.apply({"type": "output", "filename": "a.feature",
                    "text": u"progress 10%\rprogress 100%\r\nnext\n"})
        names = [node.name for node in tree.features["a.feature"].children]
        assert names == [u"progress 100%", u"next"]

    def test_hook_output_is_shown_with_its_scenario(self):
        tree = model.StatusTree()
        tree.apply({"type": "feature_started", "feature": make_outline_a()})
        update = tree.apply({
            "type": "scenario_finished", "filename": "a.feature",
            "key": "a.feature:2", "status": "passed", "duration": 0,
            "output": u"before_scenario stdout:\nHOOK: hello"})
        scenario = tree.features["a.feature"].scenarios()[0]
        kinds = [child.kind for child in scenario.children]
        assert kinds == [model.KIND_STEP, model.KIND_DETAIL, model.KIND_DETAIL]
        assert len(update.added) == 2
        # -- RERUN: Drops the hook output of the last run.
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "rerun_started", "locations": ["a.feature:2"]})
        assert [child.kind for child in scenario.children] == [model.KIND_STEP]

