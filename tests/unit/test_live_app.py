# -*- coding: UTF-8 -*-
"""
Unit tests for :mod:`behave_live_view.app` (interactive "live" view).

REQUIRES: textual (otherwise: these tests are skipped).

HINT: This repository does not use "pytest-asyncio".
Therefore, each test function is a normal (sync) function that runs its
async part with :func:`asyncio.run` (using: Textual's headless test-pilot).
"""

import asyncio
import threading
import time

import pytest

pytest.importorskip("textual")

# pylint: disable=wrong-import-position
from behave_live_view.app import (      # noqa: E402
    FILTER_DELAY, FLUSH_INTERVAL, HEADER_INTERVAL, SPINNER_FRAMES,
    FilterInput, HelpScreen,
    LiveApp, ReloadDialog, format_duration, format_elapsed, format_locations,
    format_names, make_label
)
from behave_live_view.model import (    # noqa: E402
    FAILED, StatusTree, make_outline
)


# -----------------------------------------------------------------------------
# TEST SUPPORT
# -----------------------------------------------------------------------------
ALICE = "features/alice.feature"
BOB = "features/bob.feature"
STEP_KEYWORDS = ["Given", "When", "Then"]


def scenario_line(index):
    return 10 + index * 10


def scenario_key(filename, index):
    """Key (and location) of a scenario, like: "alice.feature:20"."""
    return "%s:%d" % (filename, scenario_line(index))


def make_scenario(filename, index, name=None, step_count=3, tags=None,
                  data=None, step_data=None):
    """Make the outline of one scenario (with its steps)."""
    line = scenario_line(index)
    steps = [make_outline("step", line + offset, STEP_KEYWORDS[offset],
                          "step %d" % (offset + 1), line + offset,
                          data=step_data if offset == 0 else None)
             for offset in range(step_count)]
    return make_outline("scenario", scenario_key(filename, index), "Scenario",
                        name or "scenario %d" % (index + 1), line, steps,
                        tags=tags, data=data)


def make_feature(filename, name, scenario_count=2, step_count=3,
                 scenarios=None, tags=None):
    """Make the outline of one feature (with its scenarios and steps)."""
    if scenarios is None:
        scenarios = [make_scenario(filename, index, step_count=step_count)
                     for index in range(scenario_count)]
    return make_outline("feature", filename, "Feature", name, 1, scenarios,
                        tags=tags)


def step_definition(filename, index, offset=0):
    """Location of the step definition of a step (as text)."""
    # pylint: disable=unused-argument
    return "features/steps/steps_%s.py:%d" % (index, 40 + offset)


def step_events(filename, index, failed_step=None, step_count=3,
                output=None, finished=True):
    """Make the events of one scenario run (as list).

    :param failed_step:  Offset of the step that fails (or None).
    :param output:  Captured output of each step (or None).
    :param finished:  False: Stop after the first step_started event.
    """
    key = scenario_key(filename, index)
    events = [{"type": "scenario_started", "filename": filename, "key": key}]
    status = "passed"
    for offset in range(step_count):
        line = scenario_line(index) + offset
        events.append({"type": "step_started", "filename": filename,
                       "key": key, "line": line,
                       "definition": step_definition(filename, index, offset)})
        if not finished:
            return events
        event = {"type": "step_finished", "filename": filename, "key": key,
                 "line": line, "status": "passed", "duration": 0.05,
                 "output": output}
        if offset == failed_step:
            event.update(
                status="failed", duration=0.03,
                error="Assertion Failed: 1 != 2\nFile 'example.py':42")
            events.append(event)
            status = "failed"
            break
        events.append(event)
    events.append({"type": "scenario_finished", "filename": filename,
                   "key": key, "status": status, "duration": 0.12})
    return events


async def settle(pilot, delay=None):
    """Wait until the app has applied all pending updates.

    HINT: The app coalesces its updates and applies them with a timer
    (see: FLUSH_INTERVAL). Flushing it directly (instead of waiting for
    its timer) keeps these tests fast and deterministic.
    Use ``delay=FLUSH_INTERVAL*2`` to check the timer itself.
    """
    if delay is None:
        # pylint: disable=protected-access
        pilot.app._flush_updates()
    else:
        await pilot.pause(delay)
    await pilot.pause()


async def send_events(pilot, events):
    """Send some events to the app and wait until they are shown."""
    for event in events:
        pilot.app.handle_event(event)
    await settle(pilot)


async def send_run(pilot, events):
    """Send events step-by-step (like a real, slower test run).

    HINT: The app coalesces its updates, therefore a test that needs the
    intermediate states (running step, ...) must not send all events at once.
    """
    batch = []
    for event in events:
        batch.append(event)
        if event["type"] in ("step_started", "scenario_finished"):
            await send_events(pilot, batch)
            batch = []
    if batch:
        await send_events(pilot, batch)


def run_app(test_coroutine, status_tree=None, on_interrupt=None,
            on_rerun=None, on_check_changes=None, on_open=None,
            on_copy=None, notifications=False, size=(100, 40)):
    """Run one async test function with a LiveApp (headless).

    :param test_coroutine:  Async function: ``func(pilot, app)``.
    :return: The LiveApp that was used (for post-mortem checks).
    """
    app = LiveApp(status_tree=status_tree, on_interrupt=on_interrupt,
                  on_rerun=on_rerun, on_check_changes=on_check_changes,
                  on_open=on_open, on_copy=on_copy)

    async def run_test():
        async with app.run_test(size=size,
                                notifications=notifications) as pilot:
            await settle(pilot)
            await test_coroutine(pilot, app)

    asyncio.run(run_test())
    return app


def visible_labels(app):
    """Plain text of all currently visible tree lines (as list)."""
    labels = []
    line = 0
    while True:
        tree_node = app.tree_view.get_node_at_line(line)
        if tree_node is None:
            return labels
        labels.append(tree_node.label.plain)
        line += 1


def header_text(app):
    """Plain text of the one-line header."""
    from textual.widgets import Static
    header = app.query_one("#live-header", Static)
    return header.content.plain


def select_tree_node(app, model_node):
    """TreeNode of a model node (or None, if it is not materialized)."""
    # pylint: disable=protected-access
    return app._tree_nodes.get(id(model_node))


def select_scenario(app, filename, index):
    feature = app.status_tree.features[filename]
    for scenario in feature.scenarios():
        if scenario.key == scenario_key(filename, index):
            return scenario
    return None


def footer_text(app):
    """Text of the footer line (the key hints)."""
    # pylint: disable=protected-access
    lines = [strip.text.rstrip()
             for strip in app.screen._compositor.render_strips()
             if strip is not None]
    return lines[-1].strip()


def is_spinner_icon(label):
    return label[0] in SPINNER_FRAMES


# -----------------------------------------------------------------------------
# TESTS: Utility functions
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("seconds, expected", [
    (0.0, "0.00s"), (0.123, "0.12s"), (12.5, "12.50s"),
    (62.0, "1m 02s"), (3601.0, "60m 01s"),
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("seconds, expected", [
    (0, "00:00"), (12, "00:12"), (615, "10:15"), (3723, "1:02:03"),
])
def test_format_elapsed(seconds, expected):
    assert format_elapsed(seconds) == expected


@pytest.mark.parametrize("names, max_count, expected", [
    (["a.py", "b.py"], 5, ["a.py", "b.py"]),
    (["a.py", "b.py", "c.py"], 2, ["a.py", "b.py", u"… and 1 more"]),
    ([], 2, []),
])
def test_format_names(names, max_count, expected):
    assert format_names(names, max_count) == expected


def test_make_label_for_detail_line_has_no_icon():
    status_tree = StatusTree()
    status_tree.apply({"type": "output", "filename": ALICE, "text": "HELLO"})
    feature = status_tree.features[ALICE]
    detail = feature.children[-1]
    assert make_label(detail).plain == "HELLO"


# -----------------------------------------------------------------------------
# TESTS: Tree rendering
# -----------------------------------------------------------------------------
def test_testrun_started_shows_feature_lines():
    async def check(pilot, app):
        # -- HINT: Use the flush timer of the app here (not: settle()).
        app.handle_event({"type": "testrun_started", "files": [ALICE, BOB]})
        await settle(pilot, delay=FLUSH_INTERVAL * 2)
        assert visible_labels(app) == [
            u"○ %s" % ALICE,
            u"○ %s" % BOB,
        ]

    run_app(check)


def test_feature_started_replaces_filename_line():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "testrun_started", "files": [ALICE, BOB]},
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)},
        ])
        labels = visible_labels(app)
        assert len(labels) == 2
        assert is_spinner_icon(labels[0])
        assert labels[0][1:] == u" Feature: Alice  0/2  %s" % ALICE
        assert labels[1] == u"○ %s" % BOB

        # -- SCENARIOS: Available now (shown when the feature is expanded).
        feature_tree_node = app.tree_view.get_node_at_line(0)
        feature_tree_node.expand()
        await settle(pilot)
        labels = visible_labels(app)
        assert labels[1] == u"○ Scenario: scenario 1"
        assert labels[2] == u"○ Scenario: scenario 2"

    run_app(check)


def test_running_passed_failed_labels():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        app.tree_view.get_node_at_line(0).expand()
        await settle(pilot)

        # -- RUNNING: Spinner icon.
        await send_events(pilot, [
            {"type": "scenario_started", "filename": ALICE,
             "key": scenario_key(ALICE, 0)}])
        running_label = visible_labels(app)[1]
        assert is_spinner_icon(running_label)
        assert running_label[1:] == u" Scenario: scenario 1"

        # -- PASSED: Has a duration now.
        await send_events(pilot, step_events(ALICE, 0)[1:])
        assert visible_labels(app)[1] == u"✔ Scenario: scenario 1  0.12s"

        # -- FAILED:
        await send_events(pilot, step_events(ALICE, 1, failed_step=1))
        labels = visible_labels(app)
        assert u"✘ Scenario: scenario 2  0.12s" in labels
        assert labels[0] == u"✘ Feature: Alice  2/2 · 1 failed  %s" % ALICE

    run_app(check)


def test_steps_are_materialized_lazily():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        app.tree_view.get_node_at_line(0).expand()
        await settle(pilot)

        scenario = select_scenario(app, ALICE, 0)
        scenario_tree_node = select_tree_node(app, scenario)
        assert scenario_tree_node is not None
        # -- LAZY: No TreeNodes for its steps (but: expandable).
        assert len(scenario_tree_node.children) == 0
        assert scenario_tree_node.allow_expand is True
        assert select_tree_node(app, scenario.children[0]) is None

        scenario_tree_node.expand()
        await settle(pilot)
        assert len(scenario_tree_node.children) == 3
        assert visible_labels(app)[2:5] == [
            u"○ Given step 1", u"○ When step 2", u"○ Then step 3"]

    run_app(check)


def test_auto_expand_on_failure_shows_error_details():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        assert len(visible_labels(app)) == 1    # -- COLLAPSED: By default.

        await send_events(pilot, step_events(ALICE, 0, failed_step=1))
        labels = visible_labels(app)
        assert u"✘ Scenario: scenario 1  0.12s" in labels
        assert u"✘ When step 2  0.03s" in labels
        assert u"Assertion Failed: 1 != 2" in labels
        assert u"File 'example.py':42" in labels
        # -- OTHER SCENARIO: Still collapsed.
        assert u"○ Scenario: scenario 2" in labels

    run_app(check)


def test_user_collapse_of_failed_scenario_is_respected():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        await send_events(pilot, step_events(ALICE, 0, failed_step=1))

        # -- USER: Collapses the failed scenario again.
        scenario = select_scenario(app, ALICE, 0)
        scenario_tree_node = select_tree_node(app, scenario)
        scenario_tree_node.collapse()
        await settle(pilot)
        assert u"Assertion Failed: 1 != 2" not in visible_labels(app)

        # -- LATER UPDATES: Do not expand it again.
        await send_events(pilot, step_events(ALICE, 1))
        await send_events(pilot, [
            {"type": "feature_finished", "filename": ALICE,
             "status": "failed", "duration": 1.5,
             "statuses": {scenario_key(ALICE, 0): "failed",
                          scenario_key(ALICE, 1): "passed"}}])
        labels = visible_labels(app)
        assert u"Assertion Failed: 1 != 2" not in labels
        assert scenario_tree_node.is_expanded is False
        assert labels[0] == \
            u"✘ Feature: Alice  2/2 · 1 failed  1.50s  %s" % ALICE

    run_app(check)


def test_running_step_shows_spinner():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1)}])
        # -- HINT: Follow mode expands the running scenario.
        await send_events(pilot, step_events(ALICE, 0, finished=False))
        labels = visible_labels(app)
        assert is_spinner_icon(labels[0])       # -- Feature
        assert is_spinner_icon(labels[1])       # -- Scenario
        assert labels[2][1:] == u" Given step 1"
        assert is_spinner_icon(labels[2])       # -- Step (running)
        assert labels[3] == u"○ When step 2"

    run_app(check)


def test_passed_step_with_output_is_expandable():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=1)}])
        await send_run(pilot, step_events(ALICE, 0, step_count=1,
                                          output=u"HELLO\nWORLD"))
        scenario = select_scenario(app, ALICE, 0)
        step = scenario.children[0]
        step_tree_node = select_tree_node(app, step)
        assert step_tree_node is not None
        assert step_tree_node.allow_expand is True

        # -- USER: Opens the step to see its captured output.
        # HINT: Follow mode has collapsed the feature (it has passed).
        select_tree_node(app, app.status_tree.features[ALICE]).expand()
        select_tree_node(app, scenario).expand()
        step_tree_node.expand()
        await settle(pilot)
        labels = visible_labels(app)
        assert u"✔ Given step 1  0.05s" in labels
        assert labels[-2:] == [u"HELLO", u"WORLD"]

    run_app(check)


def test_failed_step_shows_error_lines_and_output_lines():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1)}])
        await send_events(pilot, step_events(ALICE, 0, failed_step=0,
                                             output=u"stdout line"))
        labels = visible_labels(app)
        assert u"✘ Given step 1  0.03s" in labels
        index = labels.index(u"✘ Given step 1  0.03s")
        assert labels[index + 1:index + 4] == [
            u"Assertion Failed: 1 != 2",
            u"File 'example.py':42",
            u"stdout line",
        ]

        # -- DETAIL CATEGORY: Error lines are red, output lines are dim.
        step = select_scenario(app, ALICE, 0).children[0]
        error_line, _, output_line = step.children[0:3]
        assert make_label(error_line).spans[0].style == "red"
        assert make_label(output_line).spans[0].style == "dim"

    run_app(check)


def test_details_of_materialized_step_are_added():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1)}])
        app.tree_view.get_node_at_line(0).expand()
        await settle(pilot)
        scenario_tree_node = app.tree_view.get_node_at_line(1)
        scenario_tree_node.expand()
        await settle(pilot)
        assert visible_labels(app)[2] == u"○ Given step 1"

        # -- STEP FAILS: Its detail lines must show up (already materialized).
        await send_events(pilot, step_events(ALICE, 0, failed_step=0))
        labels = visible_labels(app)
        assert labels[2] == u"✘ Given step 1  0.03s"
        assert labels[3] == u"Assertion Failed: 1 != 2"
        assert labels[4] == u"File 'example.py':42"

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Key bindings
# -----------------------------------------------------------------------------
def test_next_failure_moves_cursor_to_failed_scenario():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "testrun_started", "files": [ALICE, BOB]},
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)},
        ])
        await send_events(pilot, step_events(ALICE, 0))
        await send_events(pilot, step_events(ALICE, 1, failed_step=1))
        await pilot.press("c")      # -- COLLAPSE ALL: Hide the failure.
        await settle(pilot)
        assert len(visible_labels(app)) == 2

        await pilot.press("n")
        await settle(pilot)
        cursor_node = app.tree_view.cursor_node
        assert cursor_node is not None
        assert cursor_node.data is select_scenario(app, ALICE, 1)
        assert cursor_node.label.plain == u"✘ Scenario: scenario 2  0.12s"

    run_app(check)


def test_expand_and_collapse_keys():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        app.tree_view.cursor_line = 0
        await settle(pilot)

        await pilot.press("right")
        await settle(pilot)
        assert len(visible_labels(app)) == 3     # -- feature + 2 scenarios.

        await pilot.press("left")
        await settle(pilot)
        assert len(visible_labels(app)) == 1

        # -- COLLAPSED ALREADY: Cursor moves to the parent (root: no move).
        await pilot.press("l")
        await settle(pilot)
        assert len(visible_labels(app)) == 3
        await pilot.press("h")
        await settle(pilot)
        assert len(visible_labels(app)) == 1

    run_app(check)


def test_collapse_all_key():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        await send_events(pilot, step_events(ALICE, 0, failed_step=1))
        assert len(visible_labels(app)) > 1

        await pilot.press("c")
        await settle(pilot)
        labels = visible_labels(app)
        assert len(labels) == 1     # -- ONLY: The feature line (running).
        assert is_spinner_icon(labels[0])
        assert labels[0][1:] == u" Feature: Alice  1/2 · 1 failed  %s" % ALICE

    run_app(check)


def test_expand_failures_key():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        await send_events(pilot, step_events(ALICE, 0, failed_step=1))
        await pilot.press("c")
        await settle(pilot)
        assert len(visible_labels(app)) == 1

        await pilot.press("e")
        await settle(pilot)
        assert u"Assertion Failed: 1 != 2" in visible_labels(app)

    run_app(check)


def test_quit_during_testrun_interrupts_once_then_exits():
    calls = []

    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        await send_events(pilot, [
            {"type": "scenario_started", "filename": ALICE,
             "key": scenario_key(ALICE, 0)}])

        # -- FIRST PRESS: Interrupt the test run (but: do not exit).
        await pilot.press("q")
        await settle(pilot)
        assert calls == ["interrupt"]
        assert app.is_running is True
        assert u"stopping…" in header_text(app)

        # -- SECOND PRESS: Exit now (on_interrupt is not called again).
        await pilot.press("q")
        await settle(pilot)
        assert calls == ["interrupt"]
        assert app.is_running is False

    run_app(check, on_interrupt=lambda: calls.append("interrupt"))


def test_quit_after_testrun_done_exits():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1)}])
        app.testrun_done(False)
        await settle(pilot)
        assert app.is_running is True

        await pilot.press("q")
        await settle(pilot)
        assert app.is_running is False

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Follow mode
# -----------------------------------------------------------------------------
def start_scenario_events(filename, index):
    """Events: Scenario is started and its first step is running."""
    key = scenario_key(filename, index)
    return [{"type": "scenario_started", "filename": filename, "key": key},
            {"type": "step_started", "filename": filename, "key": key,
             "line": scenario_line(index),
             "definition": step_definition(filename, index, 0)}]


def test_follow_mode_follows_the_execution_point():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "testrun_started", "files": [ALICE, BOB]},
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        await send_events(pilot, start_scenario_events(ALICE, 0))

        feature = app.status_tree.features[ALICE]
        scenario = select_scenario(app, ALICE, 0)
        assert app.follow is True
        assert app.tree_view.cursor_node.data is scenario.children[0]
        assert select_tree_node(app, feature).is_expanded is True
        assert select_tree_node(app, scenario).is_expanded is True

        # -- SCENARIO PASSED: Follow mode collapses it again.
        await send_run(pilot, step_events(ALICE, 0, step_count=2)[2:])
        assert select_tree_node(app, scenario).is_expanded is False
        await send_events(pilot, [
            {"type": "feature_finished", "filename": ALICE,
             "status": "passed", "duration": 1.0,
             "statuses": {scenario_key(ALICE, 0): "passed"}}])
        assert select_tree_node(app, feature).is_expanded is False

        # -- NEXT FEATURE: A failed scenario stays expanded.
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(BOB, "Bob", scenario_count=1,
                                     step_count=2)}])
        await send_run(pilot, step_events(BOB, 0, failed_step=1,
                                          step_count=2))
        bob_scenario = select_scenario(app, BOB, 0)
        assert select_tree_node(app, bob_scenario).is_expanded is True
        assert u"Assertion Failed: 1 != 2" in visible_labels(app)

    run_app(check)


def test_navigation_stops_follow_mode():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2,
                                     step_count=2)}])
        await send_events(pilot, start_scenario_events(ALICE, 0))
        assert app.follow is True
        assert u" · follow" in header_text(app)

        await pilot.press("down")
        await settle(pilot)
        assert app.follow is False
        assert u"follow off" in header_text(app)

        # -- LATER EVENTS: Do not move the cursor any more.
        cursor_node = app.tree_view.cursor_node
        await send_run(pilot, step_events(ALICE, 0, step_count=2)[2:])
        await send_events(pilot, start_scenario_events(ALICE, 1))
        assert app.tree_view.cursor_node is cursor_node

        # -- FOLLOW AGAIN: Jump to the execution point ("t").
        await pilot.press("f")
        await settle(pilot)
        assert app.follow is True
        assert app.tree_view.cursor_node.data is \
            app.status_tree.execution_point

    run_app(check)


def test_follow_mode_does_not_collapse_what_the_user_expanded():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        await pilot.press("c")      # -- COLLAPSE ALL (and: follow off).
        await settle(pilot)
        assert app.follow is False

        # -- USER: Expands the feature and its scenario.
        feature = app.status_tree.features[ALICE]
        select_tree_node(app, feature).expand()
        await settle(pilot)
        scenario = select_scenario(app, ALICE, 0)
        select_tree_node(app, scenario).expand()
        await settle(pilot)

        await pilot.press("f")      # -- FOLLOW MODE: ON again.
        await settle(pilot)
        await send_run(pilot, step_events(ALICE, 0, step_count=2))
        assert app.follow is True
        assert scenario.state == "passed"
        assert select_tree_node(app, scenario).is_expanded is True
        assert select_tree_node(app, feature).is_expanded is True

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Rerun
# -----------------------------------------------------------------------------
def make_rerun_callback(calls, accepted=True):
    """Make an on_rerun() callback that records its (locations, reload)."""
    def on_rerun(locations, reload=False):
        calls.append((locations, reload))
        return accepted
    return on_rerun


def make_check_callback(calls, changes=None):
    """Make an on_check_changes() callback with a canned answer."""
    def on_check_changes(locations):
        calls.append(locations)
        if isinstance(changes, Exception):
            raise changes
        return changes
    return on_check_changes


async def run_one_failure(pilot, scenario_count=2):
    """Run a feature with a failed scenario (and: one passed scenario)."""
    await send_events(pilot, [
        {"type": "feature_started",
         "feature": make_feature(ALICE, "Alice", scenario_count=scenario_count,
                                 step_count=2)}])
    await send_run(pilot, step_events(ALICE, 0, failed_step=1, step_count=2))
    if scenario_count > 1:
        await send_run(pilot, step_events(ALICE, 1, step_count=2))


def test_rerun_keys_do_nothing_while_testrun_is_active():
    calls = []

    async def check(pilot, app):
        await run_one_failure(pilot)
        assert app.check_action("rerun_failed", ()) is False
        assert app.check_action("rerun_selected", ()) is False
        await pilot.press("R")
        await pilot.press("r")
        await settle(pilot)
        assert calls == []
        assert app.is_running is True

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_rerun_failed_calls_on_rerun_with_failed_locations():
    calls = []

    async def check(pilot, app):
        await run_one_failure(pilot)
        app.testrun_done(True)
        await settle(pilot)
        assert app.check_action("rerun_failed", ()) is True

        await pilot.press("R")      # -- RERUN: All failures.
        await settle(pilot)
        assert calls == [([scenario_key(ALICE, 0)], False)]
        # -- HINT: The app waits for the "rerun_started" event (no reset).
        assert app.is_done is True

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_rerun_selected_uses_the_node_under_the_cursor():
    calls = []

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        app.testrun_done(True)
        await settle(pilot)

        # -- CURSOR: On the feature line -> rerun the whole feature file.
        app.tree_view.cursor_line = 0
        await settle(pilot)
        await pilot.press("r")      # -- RERUN: The selected node.
        await settle(pilot)
        assert calls[-1] == ([ALICE], False)

        # -- CURSOR: On a step line -> rerun its scenario.
        step = select_scenario(app, ALICE, 0).children[1]
        visible_labels(app)     # -- HINT: Ensure the tree is built.
        app.tree_view.move_cursor(select_tree_node(app, step))
        await settle(pilot)
        assert app.tree_view.cursor_node.data is step
        await pilot.press("r")
        await settle(pilot)
        assert calls[-1] == ([scenario_key(ALICE, 0)], False)

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_fast_testrun_leaves_the_cursor_on_a_line():
    # -- REGRESSION: All events of a fast test run arrive in one batch.
    # FOLLOW MODE never moved the cursor, "r" had nothing to run again.
    calls = []

    async def check(pilot, app):
        events = [{"type": "feature_started",
                   "feature": make_feature(ALICE, "Alice", 1)}]
        events += step_events(ALICE, 0)
        events += [{"type": "feature_finished", "filename": ALICE,
                    "status": "passed", "duration": 0.1, "statuses": {}},
                   {"type": "testrun_finished"}]
        for event in events:
            app.handle_event(event)     # -- ONE BATCH: No settle() between.
        app.testrun_done(False)
        await settle(pilot)
        assert app.tree_view.cursor_node is not None
        await pilot.press("r")
        await settle(pilot)
        assert calls == [([ALICE], False)]

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_fast_testrun_with_failure_leaves_the_cursor_on_the_failure():
    async def check(pilot, app):
        events = [{"type": "feature_started",
                   "feature": make_feature(ALICE, "Alice", 2)}]
        events += step_events(ALICE, 0) + step_events(ALICE, 1, failed_step=1)
        events += [{"type": "feature_finished", "filename": ALICE,
                    "status": "failed", "duration": 0.1, "statuses": {}},
                   {"type": "testrun_finished"}]
        for event in events:
            app.handle_event(event)
        app.testrun_done(True)
        await settle(pilot)
        cursor_node = app.tree_view.cursor_node
        assert cursor_node is not None
        assert cursor_node.data is select_scenario(app, ALICE, 1)

    run_app(check)


def test_followed_testrun_ends_with_the_cursor_on_the_first_failure():
    # -- HINT: FOLLOW MODE leaves the cursor on the test that ran last.
    async def check(pilot, app):
        await send_run(pilot, [{"type": "feature_started",
                                "feature": make_feature(ALICE, "Alice", 2)}]
                       + step_events(ALICE, 0, failed_step=1)
                       + step_events(ALICE, 1))
        assert app.follow
        assert app.tree_view.cursor_node.data is not \
            select_scenario(app, ALICE, 0)
        await send_events(pilot, [
            {"type": "feature_finished", "filename": ALICE,
             "status": "failed", "duration": 0.1, "statuses": {}},
            {"type": "testrun_finished"}])
        app.testrun_done(True)
        await settle(pilot)
        assert app.tree_view.cursor_node.data is select_scenario(app, ALICE, 0)

    run_app(check)


def test_testrun_end_keeps_the_cursor_where_the_user_has_put_it():
    async def check(pilot, app):
        await send_run(pilot, [{"type": "feature_started",
                                "feature": make_feature(ALICE, "Alice", 2)}]
                       + step_events(ALICE, 0, failed_step=1)
                       + step_events(ALICE, 1))
        await pilot.press("up")     # -- USER NAVIGATES: Follow mode is off.
        await settle(pilot)
        cursor_node = app.tree_view.cursor_node
        assert not app.follow
        app.testrun_done(True)
        await settle(pilot)
        assert app.tree_view.cursor_node is cursor_node

    run_app(check)


def test_rerun_failed_without_failures_does_not_call_on_rerun():
    calls = []

    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        await send_run(pilot, step_events(ALICE, 0, step_count=2))
        app.testrun_done(False)
        await settle(pilot)

        await pilot.press("R")
        await settle(pilot)
        assert calls == []

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_rerun_keys_without_on_rerun_are_harmless():
    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        app.testrun_done(True)
        await settle(pilot)
        assert app.check_action("rerun_failed", ()) is False

        await pilot.press("R")
        await pilot.press("r")
        await settle(pilot)
        assert app.is_running is True
        assert app.is_done is True

    run_app(check)


def test_rerun_started_event_restarts_the_view():
    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        app.testrun_done(True)
        await settle(pilot)
        assert header_text(app).startswith(u"behave  ✘ failed")
        assert u"Assertion Failed: 1 != 2" in visible_labels(app)

        # -- RERUN: The caller has accepted (and resets the status tree).
        await send_events(pilot, [
            {"type": "rerun_started",
             "locations": [scenario_key(ALICE, 0)]}])
        assert app.is_done is False
        assert app.testrun_failed is None
        assert app.follow is True
        header = header_text(app)
        assert u" running  0/1 scenarios" in header
        assert u" · follow" in header
        labels = visible_labels(app)
        assert u"Assertion Failed: 1 != 2" not in labels
        assert u"○ Scenario: scenario 1" in labels
        assert u"○ When step 2" in labels

        # -- RUN AGAIN: This time, it passes.
        await send_run(pilot, step_events(ALICE, 0, step_count=2))
        app.testrun_done(False)
        await settle(pilot)
        assert app.is_done is True
        assert header_text(app).startswith(u"behave  ✔ passed  1 passed")

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Rerun with changed source files (reload dialog)
# -----------------------------------------------------------------------------
def make_changes(reload=None, warn=None, features=None):
    """Make the answer of an on_check_changes() callback."""
    return {"reload": list(reload or []), "warn": list(warn or []),
            "features": list(features or [])}


def select_dialog(app):
    """The reload dialog (or None, if it is not shown)."""
    screen = app.screen
    return screen if isinstance(screen, ReloadDialog) else None


def dialog_text(app):
    """Text of the reload dialog (as one string)."""
    from textual.widgets import Static
    dialog = select_dialog(app)
    assert dialog is not None, "REQUIRE: Reload dialog is shown"
    return u"\n".join(widget.content.plain
                      for widget in dialog.query(Static))


def notifications(app):
    """Notifications of the app, as list of (severity, message) tuples."""
    # pylint: disable=protected-access
    return [(item.severity, item.message) for item in app._notifications]


async def rerun_all_failures(pilot):
    """Press the key that reruns all failed scenarios."""
    await pilot.press("R")
    await settle(pilot)


async def run_until_done_with_one_failure(pilot, app):
    """Run one feature with a failed scenario (and: end the test run)."""
    await run_one_failure(pilot, scenario_count=1)
    app.testrun_done(True)
    await settle(pilot)


def test_rerun_without_check_changes_callback():
    calls = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert calls == [([scenario_key(ALICE, 0)], False)]
        assert select_dialog(app) is None

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_rerun_with_unchanged_source_files():
    calls = []
    checked = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert checked == [[scenario_key(ALICE, 0)]]
        assert calls == [([scenario_key(ALICE, 0)], False)]
        assert select_dialog(app) is None

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback(checked, make_changes()))


@pytest.mark.parametrize("answer_key", ["y", "enter"])
def test_rerun_with_changed_modules_asks_and_reloads(answer_key):
    calls = []
    checked = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        # -- DIALOG: Is shown (and: no rerun yet).
        assert select_dialog(app) is not None
        assert calls == []
        text = dialog_text(app)
        assert u"Source files changed since they were loaded" in text
        assert u"steps/alice_steps.py" in text
        assert u"environment.py" in text

        await pilot.press(answer_key)
        await settle(pilot)
        assert calls == [([scenario_key(ALICE, 0)], True)]
        assert select_dialog(app) is None
        assert app.dialog_is_open is False

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback(
                checked, make_changes(reload=["steps/alice_steps.py",
                                              "environment.py"])))


def test_reload_dialog_answer_no_runs_with_the_old_code():
    calls = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        cursor_node = app.tree_view.cursor_node
        await rerun_all_failures(pilot)
        assert select_dialog(app) is not None

        await pilot.press("n")      # -- HINT: "n" belongs to the dialog now.
        await settle(pilot)
        assert calls == [([scenario_key(ALICE, 0)], False)]
        assert select_dialog(app) is None
        assert app.tree_view.cursor_node is cursor_node

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback([], make_changes(
                reload=["steps/alice_steps.py"])))


def test_reload_dialog_escape_cancels_the_rerun():
    calls = []
    checked = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert select_dialog(app) is not None

        await pilot.press("escape")
        await settle(pilot)
        assert calls == []
        assert select_dialog(app) is None
        assert app.is_done is True

        # -- KEYS WORK AGAIN: The dialog is shown again ("q" cancels, too).
        await rerun_all_failures(pilot)
        assert len(checked) == 2
        assert select_dialog(app) is not None
        await pilot.press("q")
        await settle(pilot)
        assert calls == []
        assert select_dialog(app) is None
        assert app.is_running is True

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback(
                checked, make_changes(reload=["steps/alice_steps.py"])))


def test_keys_do_not_reach_the_tree_while_the_dialog_is_shown():
    calls = []
    checked = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert select_dialog(app) is not None
        cursor_node = app.tree_view.cursor_node
        labels = visible_labels(app)

        for key in ("R", "r", "down", "c", "e"):
            await pilot.press(key)
            await settle(pilot)
        assert len(checked) == 1        # -- No other check/rerun.
        assert calls == []
        assert select_dialog(app) is not None
        assert app.tree_view.cursor_node is cursor_node
        assert visible_labels(app) == labels

        await pilot.press("escape")
        await settle(pilot)
        assert select_dialog(app) is None

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback(
                checked, make_changes(reload=["steps/alice_steps.py"])))


def test_ctrl_q_quits_while_the_reload_dialog_is_shown():
    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert select_dialog(app) is not None

        await pilot.press("ctrl+q")
        await settle(pilot)
        assert app.is_running is False

    run_app(check, on_rerun=make_rerun_callback([]),
            on_check_changes=make_check_callback(
                [], make_changes(reload=["steps/alice_steps.py"])))


def test_changed_files_that_cannot_be_reloaded_are_notified():
    calls = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert select_dialog(app) is None
        assert calls == [([scenario_key(ALICE, 0)], False)]
        warnings = [message for severity, message in notifications(app)
                    if severity == "warning"]
        assert len(warnings) == 1
        assert u"cannot be reloaded" in warnings[0]
        assert u"_speedups.so" in warnings[0]

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback([], make_changes(
                warn=["_speedups.so", "behave_main.py"])))


def test_changed_feature_files_are_notified():
    calls = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert calls == [([scenario_key(ALICE, 0)], False)]
        messages = [message for severity, message in notifications(app)
                    if severity == "information"]
        assert any(u"Feature file changed, running all of it" in message
                   and ALICE in message for message in messages)

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback(
                [], make_changes(features=[ALICE])))


def test_check_changes_that_fails_does_not_stop_the_rerun():
    calls = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert calls == [([scenario_key(ALICE, 0)], False)]
        assert select_dialog(app) is None

    run_app(check, on_rerun=make_rerun_callback(calls),
            on_check_changes=make_check_callback(
                [], RuntimeError("OOPS: Cannot check the source files")))


def test_reload_dialog_shortens_a_long_file_list():
    filenames = ["steps/step_%02d.py" % index for index in range(10)]

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        text = dialog_text(app)
        assert u"steps/step_07.py" in text
        assert u"steps/step_08.py" not in text
        assert u"… and 2 more" in text
        await pilot.press("escape")
        await settle(pilot)

    run_app(check, on_rerun=make_rerun_callback([]),
            on_check_changes=make_check_callback(
                [], make_changes(reload=filenames)))


def test_rerun_started_with_changed_features_rebuilds_the_feature():
    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        feature = app.status_tree.features[ALICE]
        select_tree_node(app, feature).expand()
        await settle(pilot)
        assert u"✘ Scenario: scenario 1  0.12s" in visible_labels(app)

        # -- FEATURE FILE WAS CHANGED: Its contents are unknown now.
        await send_events(pilot, [
            {"type": "rerun_started", "locations": [ALICE],
             "changed_features": [ALICE]}])
        assert visible_labels(app) == [u"○ Feature: Alice  0/0  %s" % ALICE]
        assert feature.children == []

        # -- FEATURE STARTED AGAIN: With its new contents.
        new_outline = make_outline(
            "feature", ALICE, "Feature", "Alice (changed)", 1,
            [make_outline("scenario", "%s:42" % ALICE, "Scenario",
                          "new scenario", 42,
                          [make_outline("step", 43, "Given", "a new step")])])
        await send_events(pilot, [
            {"type": "feature_started", "feature": new_outline}])
        labels = visible_labels(app)
        assert labels[0][1:] == u" Feature: Alice (changed)  0/1  %s" % ALICE
        assert labels[1] == u"○ Scenario: new scenario"

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Location bar and "open in editor"
# -----------------------------------------------------------------------------
def make_located_tree(with_definition=True):
    """Status tree with one feature, one scenario and two steps."""
    status_tree = StatusTree()
    status_tree.apply({"type": "feature_started",
                       "feature": make_feature(ALICE, "Alice",
                                               scenario_count=1,
                                               step_count=2)})
    key = scenario_key(ALICE, 0)
    status_tree.apply({"type": "scenario_started", "filename": ALICE,
                       "key": key})
    if with_definition:
        status_tree.apply({
            "type": "step_started", "filename": ALICE, "key": key,
            "line": scenario_line(0),
            "definition": u"features/steps/alice_steps.py:48"})
        status_tree.apply({
            "type": "step_finished", "filename": ALICE, "key": key,
            "line": scenario_line(0), "status": "failed", "duration": 0.03,
            "error": u"Assertion Failed: 1 != 2"})
    return status_tree


def location_bar_text(app):
    """Plain text of the location bar."""
    from textual.widgets import Static
    return app.query_one("#live-location", Static).content.plain


def move_cursor_to(app, model_node):
    """Move the tree cursor to the TreeNode of this model node."""
    visible_labels(app)     # -- HINT: Ensure that the tree is built.
    tree_node = select_tree_node(app, model_node)
    assert tree_node is not None, "REQUIRE: %r is materialized" % model_node
    app.tree_view.move_cursor(tree_node)


def test_format_locations_without_node():
    assert format_locations(None) == u""


def test_format_locations_for_feature_and_scenario():
    status_tree = make_located_tree()
    feature = status_tree.features[ALICE]
    scenario = feature.scenarios()[0]
    assert format_locations(feature) == u"%s:1" % ALICE
    assert format_locations(scenario) == u"%s:%d" % (ALICE, scenario_line(0))


def test_format_locations_for_step_with_and_without_definition():
    status_tree = make_located_tree()
    scenario = status_tree.features[ALICE].scenarios()[0]
    step, next_step = scenario.children[0], scenario.children[1]
    assert format_locations(step) == \
        u"%s:%d → features/steps/alice_steps.py:48" % (ALICE,
                                                       scenario_line(0))
    # -- STEP WAS NOT RUN (yet): Its step definition is unknown.
    assert format_locations(next_step) == \
        u"%s:%d" % (ALICE, scenario_line(0) + 1)


def test_format_locations_for_detail_lines():
    status_tree = make_located_tree()
    scenario = status_tree.features[ALICE].scenarios()[0]
    detail = scenario.children[0].children[0]
    assert detail.name.startswith(u"Assertion Failed")
    # -- DETAIL LINE: Uses the location of its owner (here: the step).
    assert format_locations(detail) == \
        u"%s:%d → features/steps/alice_steps.py:48" % (ALICE,
                                                       scenario_line(0))

    # -- FEATURE DETAIL LINE (hook output): Uses the feature location.
    status_tree.apply({"type": "output", "filename": ALICE,
                       "text": u"HOOK-ERROR: before_feature"})
    feature_detail = status_tree.features[ALICE].children[-1]
    assert format_locations(feature_detail) == u"%s:1" % ALICE


def test_format_locations_without_line():
    status_tree = StatusTree()
    status_tree.apply({"type": "testrun_started", "files": [ALICE]})
    feature = status_tree.features[ALICE]
    assert format_locations(feature) == ALICE


def test_format_locations_is_truncated_on_the_left():
    status_tree = make_located_tree()
    step = status_tree.features[ALICE].scenarios()[0].children[0]
    text = format_locations(step, width=20)
    assert len(text) == 20
    assert text.startswith(u"…")
    assert text.endswith(u"features/steps/alice_steps.py:48"[-19:])
    # -- HINT: No truncation, if it fits.
    assert format_locations(step, width=200) == format_locations(step)


def test_location_bar_shows_the_node_under_the_cursor():
    async def check(pilot, app):
        assert location_bar_text(app) == u""
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        await pilot.press("down")       # -- CURSOR: On the feature line.
        await settle(pilot)
        assert location_bar_text(app) == u"%s:1" % ALICE

        await pilot.press("right")      # -- EXPAND: The feature.
        await pilot.press("down")       # -- CURSOR: On the scenario line.
        await settle(pilot)
        assert location_bar_text(app) == \
            u"%s:%d" % (ALICE, scenario_line(0))

    run_app(check)


def test_location_bar_is_updated_when_the_step_definition_is_known():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        scenario = select_scenario(app, ALICE, 0)
        step = scenario.children[1]
        # -- USER: Navigates to the (pending) second step (follow mode: off).
        app._reveal(scenario, expand_node=True)     # noqa: protected-access
        await settle(pilot)
        move_cursor_to(app, step)
        await settle(pilot)
        assert app.follow is False
        assert location_bar_text(app) == \
            u"%s:%d" % (ALICE, scenario_line(0) + 1)

        # -- STEP STARTS: Its step definition is known now.
        await send_events(pilot, [
            {"type": "step_started", "filename": ALICE,
             "key": scenario_key(ALICE, 0), "line": scenario_line(0) + 1,
             "definition": u"features/steps/alice_steps.py:52"}])
        assert app.tree_view.cursor_node.data is step
        assert location_bar_text(app) == \
            u"%s:%d → features/steps/alice_steps.py:52" % (
                ALICE, scenario_line(0) + 1)

    run_app(check)


@pytest.mark.parametrize("node_name, expected_line", [
    ("feature", 1),
    ("scenario", scenario_line(0)),
    ("step", scenario_line(0)),
    ("detail", scenario_line(0) + 1),   # -- HINT: Of its (failed) step.
])
def test_open_source_uses_the_location_of_the_cursor_node(node_name,
                                                          expected_line):
    opened = []

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        feature = app.status_tree.features[ALICE]
        scenario = feature.scenarios()[0]
        step = scenario.children[0]
        nodes = {"feature": feature, "scenario": scenario, "step": step,
                 "detail": scenario.children[1].children[0]}
        move_cursor_to(app, nodes[node_name])
        await settle(pilot)

        await pilot.press("o")
        await settle(pilot)
        assert opened == [(ALICE, expected_line)]

    run_app(check, on_open=lambda filename, line: opened.append(
        (filename, line)) or None)


def test_open_definition_of_a_step():
    opened = []

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        scenario = app.status_tree.features[ALICE].scenarios()[0]
        move_cursor_to(app, scenario.children[0])
        await settle(pilot)

        await pilot.press("d")
        await settle(pilot)
        assert opened == [(u"features/steps/steps_0.py", 40)]
        assert not notifications(app)

    run_app(check, on_open=lambda filename, line: opened.append(
        (filename, line)) or None)


@pytest.mark.parametrize("node_name", ["scenario", "pending_step"])
def test_open_definition_without_step_definition(node_name):
    opened = []

    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        scenario = select_scenario(app, ALICE, 0)
        app._reveal(scenario, expand_node=True)     # noqa: protected-access
        await settle(pilot)
        nodes = {"scenario": scenario, "pending_step": scenario.children[0]}
        move_cursor_to(app, nodes[node_name])
        await settle(pilot)

        await pilot.press("d")
        await settle(pilot)
        assert opened == []
        assert any(u"No step definition known" in message
                   for _severity, message in notifications(app))

    run_app(check, on_open=lambda filename, line: opened.append(
        (filename, line)) or None)


def test_open_reports_the_message_of_the_callback():
    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        move_cursor_to(app, app.status_tree.features[ALICE])
        await settle(pilot)

        await pilot.press("o")
        await settle(pilot)
        assert (u"warning", u"EDITOR NOT FOUND: vim") in notifications(app)

    run_app(check, on_open=lambda filename, line: u"EDITOR NOT FOUND: vim")


def test_open_survives_a_failing_callback():
    def on_open(filename, line):
        raise RuntimeError("OOPS")

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        move_cursor_to(app, app.status_tree.features[ALICE])
        await settle(pilot)

        await pilot.press("o")
        await settle(pilot)
        assert app.is_running is True
        assert any(u"Cannot open" in message and u"OOPS" in message
                   for severity, message in notifications(app)
                   if severity == "warning")

    run_app(check, on_open=on_open)


def test_open_keys_are_disabled_without_the_callback():
    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        assert app.check_action("open_source", ()) is False
        assert app.check_action("open_definition", ()) is False
        move_cursor_to(app, app.status_tree.features[ALICE])
        await settle(pilot)

        await pilot.press("o")
        await pilot.press("d")
        await settle(pilot)
        assert app.is_running is True
        assert not notifications(app)

    run_app(check)


def test_open_does_not_stop_the_follow_mode():
    opened = []

    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        await send_events(pilot, start_scenario_events(ALICE, 0))
        assert app.follow is True

        await pilot.press("o")
        await pilot.press("d")
        await settle(pilot)
        assert len(opened) == 2
        assert app.follow is True
        assert u" · follow" in header_text(app)

    run_app(check, on_open=lambda filename, line: opened.append(
        (filename, line)) or None)


def test_open_keys_are_blocked_while_the_reload_dialog_is_shown():
    opened = []

    async def check(pilot, app):
        await run_until_done_with_one_failure(pilot, app)
        await rerun_all_failures(pilot)
        assert select_dialog(app) is not None

        await pilot.press("o")
        await pilot.press("d")
        await settle(pilot)
        assert opened == []
        assert select_dialog(app) is not None
        await pilot.press("escape")
        await settle(pilot)

    run_app(check, on_rerun=make_rerun_callback([]),
            on_check_changes=make_check_callback(
                [], make_changes(reload=["steps/alice_steps.py"])),
            on_open=lambda filename, line: opened.append(
                (filename, line)) or None)


# -----------------------------------------------------------------------------
# TESTS: Step data (table, text, example row)
# -----------------------------------------------------------------------------
TABLE_LINES = [u"| name | value |", u"| a    | 1     |"]


def make_feature_with_data(filename=ALICE):
    """Feature outline: One scenario (example row) with a step (table)."""
    scenario = make_scenario(filename, 0, name="zone transfer", step_count=2,
                             data=[u"Example: name=a, value=1"],
                             step_data=TABLE_LINES)
    return make_feature(filename, "Alice", scenarios=[scenario])


def test_data_details_are_shown_with_a_pending_step():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started", "feature": make_feature_with_data()}])
        scenario = select_scenario(app, ALICE, 0)
        app._reveal(scenario, expand_node=True)     # noqa: protected-access
        await settle(pilot)
        labels = visible_labels(app)
        # -- EXAMPLE ROW: First child of the scenario.
        assert labels[2] == u"Example: name=a, value=1"
        assert labels[3] == u"○ Given step 1"

        # -- STEP WITH A TABLE: Expandable, although it is pending.
        step = scenario.children[1]
        step_tree_node = select_tree_node(app, step)
        assert step_tree_node.allow_expand is True
        step_tree_node.expand()
        await settle(pilot)
        assert visible_labels(app)[4:6] == TABLE_LINES

        # -- STYLE: Data lines are neutral (not an error, not output).
        data_line = step.children[0]
        assert u"cyan" in str(make_label(data_line).spans[0].style)

    run_app(check)


def test_rerun_keeps_the_data_details():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started", "feature": make_feature_with_data()}])
        await send_run(pilot, step_events(ALICE, 0, failed_step=0,
                                          step_count=2))
        app.testrun_done(True)
        await settle(pilot)
        labels = visible_labels(app)
        assert u"Assertion Failed: 1 != 2" in labels
        assert TABLE_LINES[0] in labels

        await send_events(pilot, [
            {"type": "rerun_started",
             "locations": [scenario_key(ALICE, 0)]}])
        labels = visible_labels(app)
        assert u"Assertion Failed: 1 != 2" not in labels
        assert u"Example: name=a, value=1" in labels

        # -- STEP DATA: Still there (only error/output lines are dropped).
        step = select_scenario(app, ALICE, 0).children[1]
        assert [child.name for child in step.children] == TABLE_LINES
        select_tree_node(app, step).expand()
        await settle(pilot)
        assert TABLE_LINES[0] in visible_labels(app)

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Filter and view mode
# -----------------------------------------------------------------------------
def make_filter_features():
    """Two features: Alice (dns lookup @smoke, zone transfer) and Bob."""
    alice = make_feature(ALICE, "Alice", scenarios=[
        make_scenario(ALICE, 0, name="dns lookup", step_count=2,
                      tags=["smoke", "fast"]),
        make_scenario(ALICE, 1, name="zone transfer", step_count=2),
    ])
    bob = make_feature(BOB, "Bob", scenarios=[
        make_scenario(BOB, 0, name="other thing", step_count=2),
    ])
    return [alice, bob]


async def send_filter_features(pilot):
    await send_events(pilot, [{"type": "feature_started", "feature": outline}
                              for outline in make_filter_features()])


async def use_filter(pilot, text):
    """Open the filter input and use this filter text (without typing).

    HINT: Applies the filter directly (instead of waiting for its
    debounce timer, see: FILTER_DELAY) to keep these tests fast.
    """
    app = pilot.app
    # pylint: disable=protected-access
    if not app.filter_is_open:
        await pilot.press("slash")
        await settle(pilot)
    app.query_one("#live-filter", FilterInput).value = text
    await settle(pilot)
    app._apply_filter_input()
    await settle(pilot)


def test_filter_shows_the_matching_scenarios_and_their_ancestors():
    async def check(pilot, app):
        await send_filter_features(pilot)
        assert len(visible_labels(app)) == 2     # -- Two feature lines.

        await use_filter(pilot, u"zone")
        labels = visible_labels(app)
        assert len(labels) == 1
        assert labels[0][1:] == u" Feature: Alice  0/2  %s" % ALICE
        feature = app.status_tree.features[ALICE]
        select_tree_node(app, feature).expand()
        await settle(pilot)
        assert visible_labels(app)[1:] == [u"○ Scenario: zone transfer"]

    run_app(check)


def test_filter_by_tag():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"@smo")
        feature = app.status_tree.features[ALICE]
        select_tree_node(app, feature).expand()
        await settle(pilot)
        assert visible_labels(app)[1:] == [u"○ Scenario: dns lookup"]

    run_app(check)


def test_filter_by_feature_name_shows_all_its_scenarios():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"alice")
        feature = app.status_tree.features[ALICE]
        select_tree_node(app, feature).expand()
        await settle(pilot)
        assert visible_labels(app)[1:] == [
            u"○ Scenario: dns lookup", u"○ Scenario: zone transfer"]

    run_app(check)


def test_filter_input_is_closed_with_enter_and_escape():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"zone")
        assert app.filter_is_open is True

        await pilot.press("enter")
        await settle(pilot)
        assert app.filter_is_open is False
        assert app.filter_text == u"zone"
        assert u" · filter: zone" in header_text(app)
        assert app.focused is app.tree_view

        # -- FILTER INPUT AGAIN: It has the current filter text.
        await pilot.press("slash")
        await settle(pilot)
        assert app.query_one("#live-filter", FilterInput).value == u"zone"
        await pilot.press("escape")
        await settle(pilot)
        assert app.filter_text == u""
        assert app.filter_is_open is False
        assert len(visible_labels(app)) == 2
        # -- LOCATION BAR: Is shown again (instead of the filter input).
        assert app.query_one("#live-location").has_class("-hidden") is False

    run_app(check)


def test_escape_in_the_tree_clears_the_filter():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"zone")
        await pilot.press("enter")
        await settle(pilot)
        assert len(visible_labels(app)) == 1

        await pilot.press("escape")
        await settle(pilot)
        assert app.filter_text == u""
        assert len(visible_labels(app)) == 2
        assert u"filter:" not in header_text(app)

    run_app(check)


def test_keys_typed_into_the_filter_input_are_text():
    calls = []

    async def check(pilot, app):
        await send_filter_features(pilot)
        await send_events(pilot, start_scenario_events(ALICE, 0))
        assert app.follow is True

        await pilot.press("slash")
        await settle(pilot)
        for key in "qfnr":
            await pilot.press(key)
        await settle(pilot, delay=FILTER_DELAY * 2)
        filter_input = app.query_one("#live-filter", FilterInput)
        assert filter_input.value == u"qfnr"
        # -- HINT: The filter was applied by its (debounce) timer.
        assert app.filter_text == u"qfnr"
        assert visible_labels(app) == []
        assert calls == []              # -- "q": No interrupt.
        assert app.interrupting is False
        assert app.follow is True       # -- "f": Follow mode is unchanged.
        assert app.is_running is True

    run_app(check, on_interrupt=lambda: calls.append("interrupt"),
            on_rerun=make_rerun_callback(calls))


def test_view_mode_cycles_and_is_shown_in_the_header():
    async def check(pilot, app):
        await send_filter_features(pilot)
        assert app.view_mode == "all"
        assert u"view:" not in header_text(app)

        await pilot.press("v")
        await settle(pilot)
        assert app.view_mode == "failed"
        assert u" · view: failed" in header_text(app)

        await pilot.press("v")
        await settle(pilot)
        assert app.view_mode == "not passed"
        assert u" · view: not passed" in header_text(app)

        await pilot.press("v")
        await settle(pilot)
        assert app.view_mode == "all"
        assert u"view:" not in header_text(app)

    run_app(check)


def test_failed_view_follows_the_test_run():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await pilot.press("v")      # -- VIEW MODE: failed
        await settle(pilot)
        assert visible_labels(app) == []
        assert location_bar_text(app) == u"No lines match the filter"

        # -- SCENARIO FAILS: It appears in this view.
        await send_run(pilot, step_events(ALICE, 1, failed_step=0,
                                          step_count=2))
        labels = visible_labels(app)
        assert labels[0].endswith(u"1 failed  %s" % ALICE)
        assert u"✘ Scenario: zone transfer  0.12s" in labels

        # -- RERUN: It passes now and disappears from this view.
        await send_events(pilot, [
            {"type": "rerun_started",
             "locations": [scenario_key(ALICE, 1)]}])
        await send_run(pilot, step_events(ALICE, 1, step_count=2))
        assert visible_labels(app) == []

    run_app(check)


def test_filter_moves_the_cursor_when_its_line_is_gone():
    async def check(pilot, app):
        await send_filter_features(pilot)
        bob = app.status_tree.features[BOB]
        move_cursor_to(app, bob)
        await settle(pilot)
        assert app.tree_view.cursor_node.data is bob

        await use_filter(pilot, u"alice")
        assert len(visible_labels(app)) == 1
        cursor_node = app.tree_view.cursor_node
        assert cursor_node is not None
        assert cursor_node.data is app.status_tree.features[ALICE]

    run_app(check)


def test_expansion_state_survives_a_filter():
    async def check(pilot, app):
        await send_filter_features(pilot)
        feature = app.status_tree.features[ALICE]
        scenario = select_scenario(app, ALICE, 1)
        app._reveal(scenario, expand_node=True)     # noqa: protected-access
        await settle(pilot)
        assert u"○ Given step 1" in visible_labels(app)

        await use_filter(pilot, u"zone")
        await pilot.press("enter")
        await settle(pilot)
        assert select_tree_node(app, feature).is_expanded is True
        assert select_tree_node(app, scenario).is_expanded is True
        assert u"○ Given step 1" in visible_labels(app)

        await pilot.press("escape")     # -- CLEAR THE FILTER:
        await settle(pilot)
        assert select_tree_node(app, scenario).is_expanded is True
        assert u"○ Scenario: dns lookup" in visible_labels(app)

    run_app(check)


def test_next_failure_and_rerun_use_the_visible_failures_only():
    calls = []

    async def check(pilot, app):
        await send_filter_features(pilot)
        await send_run(pilot, step_events(ALICE, 0, failed_step=0,
                                          step_count=2))
        await send_run(pilot, step_events(ALICE, 1, failed_step=0,
                                          step_count=2))
        app.testrun_done(True)
        await settle(pilot)
        assert len(app.status_tree.failed_nodes()) == 2

        await use_filter(pilot, u"zone")
        await pilot.press("enter")
        await settle(pilot)
        assert len(app.visible_failed_nodes()) == 1

        # -- NEXT FAILURE: Only the visible one.
        for _ in range(2):
            await pilot.press("n")
            await settle(pilot)
            assert app.tree_view.cursor_node.data is \
                select_scenario(app, ALICE, 1)

        # -- RERUN FAILED: Only the visible failures.
        await pilot.press("R")
        await settle(pilot)
        assert calls == [([scenario_key(ALICE, 1)], False)]
        assert any(u"Rerunning 1 visible failure" in message
                   for _severity, message in notifications(app))

    run_app(check, on_rerun=make_rerun_callback(calls))


def test_filter_without_any_match():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"nothing-matches-this")
        assert visible_labels(app) == []
        assert location_bar_text(app) == u"No lines match the filter"
        assert app.tree_view.cursor_node is None

        await pilot.press("escape")
        await settle(pilot)
        assert len(visible_labels(app)) == 2

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Regressions (review findings)
# -----------------------------------------------------------------------------
async def run_two_features_with_one_failure(pilot):
    """Run: Alice (scenario 1 fails, scenario 2 passes) and Bob (passes)."""
    await send_filter_features(pilot)
    await send_run(pilot, step_events(ALICE, 0, failed_step=0, step_count=2))
    await send_run(pilot, step_events(ALICE, 1, step_count=2))
    await send_run(pilot, step_events(BOB, 0, step_count=2))


def test_filter_is_closed_when_its_input_loses_the_focus():
    """REGRESSION: A mouse click on the tree disabled all keys."""
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"zone")
        assert app.filter_is_open is True

        # -- USER CLICKS THE TREE: The focus moves away from the input.
        await pilot.click("#live-tree")
        await settle(pilot)
        assert app.filter_is_open is False
        assert app.filter_is_shown is False
        assert app.filter_text == u"zone"       # -- KEPT (like: enter).
        assert app.query_one("#live-location").has_class("-hidden") is False

        # -- KEYS WORK AGAIN:
        assert app.check_action("clear_filter", ()) is True
        await pilot.press("v")
        await settle(pilot)
        assert app.view_mode == "failed"
        await pilot.press("escape")
        await settle(pilot)
        assert app.filter_text == u""

    run_app(check)


def test_expansion_and_cursor_survive_a_view_mode_round_trip():
    """REGRESSION: Cycling "v" collapsed what the user had expanded."""
    async def check(pilot, app):
        await run_two_features_with_one_failure(pilot)
        app.testrun_done(True)
        await settle(pilot)

        # -- USER: Expands the passed feature and its scenario.
        feature = app.status_tree.features[BOB]
        scenario = select_scenario(app, BOB, 0)
        select_tree_node(app, feature).expand()
        await settle(pilot)
        select_tree_node(app, scenario).expand()
        await settle(pilot)
        move_cursor_to(app, scenario)
        await settle(pilot)

        # -- VIEW MODE: all -> failed -> not passed -> all
        for _ in range(3):
            await pilot.press("v")
            await settle(pilot)
        assert app.view_mode == "all"
        assert select_tree_node(app, feature).is_expanded is True
        assert select_tree_node(app, scenario).is_expanded is True
        assert app.tree_view.cursor_node.data is scenario

    run_app(check)


def test_auto_expand_is_retried_when_the_failure_becomes_visible():
    """REGRESSION: A failure that failed while hidden was never expanded."""
    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"other")       # -- HIDES: Alice.
        await pilot.press("enter")
        await settle(pilot)
        assert ALICE not in u"".join(visible_labels(app))

        # -- SCENARIO FAILS: While it is filtered out.
        await send_run(pilot, step_events(ALICE, 0, failed_step=0,
                                          step_count=2))
        assert u"Assertion Failed: 1 != 2" not in visible_labels(app)

        # -- FILTER CLEARED: Now the failure is expanded (auto-expand).
        await pilot.press("escape")
        await settle(pilot)
        labels = visible_labels(app)
        assert u"✘ Scenario: dns lookup  0.12s" in labels
        assert u"Assertion Failed: 1 != 2" in labels

    run_app(check)


def test_rerun_with_changed_features_forgets_the_dropped_nodes():
    """REGRESSION: Stale ids of dropped nodes could alias new nodes.

    HINT: Nodes that a filter hides have no TreeNode -- only the (new)
    persistent bookkeeping knows them, so it must be purged, too.
    """
    async def check(pilot, app):
        # pylint: disable=protected-access
        await run_two_features_with_one_failure(pilot)
        app.testrun_done(True)
        await settle(pilot)

        # -- USER: Expands the other feature (it is remembered).
        feature = app.status_tree.features[BOB]
        scenario = select_scenario(app, BOB, 0)
        select_tree_node(app, feature).expand()
        await settle(pilot)
        select_tree_node(app, scenario).expand()
        await settle(pilot)
        assert id(scenario) in app._expanded_ids

        # -- FILTER: Hides that feature (its TreeNodes are gone).
        await use_filter(pilot, u"alice")
        await pilot.press("enter")
        await settle(pilot)
        assert select_tree_node(app, scenario) is None
        assert id(scenario) in app._expanded_ids

        # -- RERUN: Its feature file was changed (its nodes are dropped).
        await send_events(pilot, [
            {"type": "rerun_started", "locations": [BOB],
             "changed_features": [BOB]}])
        alive = set(id(node) for node in app.status_tree.root.walk())
        assert id(scenario) not in alive
        for known_ids in (app._expanded_ids, app._materialized_ever,
                          app._auto_expanded, app._followed,
                          app._user_expanded, set(app._tree_nodes)):
            assert known_ids <= alive

    run_app(check)


@pytest.mark.parametrize("failed, expected", [
    (True, u"✘ failed"),
    (False, u"✘ failed"),       # -- HINT: Another failure is still there.
])
def test_header_verdict_uses_the_status_tree(failed, expected):
    """REGRESSION: A partial rerun showed a green "passed"."""
    async def check(pilot, app):
        await run_two_features_with_one_failure(pilot)
        app.testrun_done(failed)
        await settle(pilot)
        assert app.status_tree.failed_nodes()
        assert header_text(app).startswith(u"behave  %s" % expected)

    run_app(check)


def test_header_verdict_is_incomplete_with_untested_scenarios():
    async def check(pilot, app):
        await send_filter_features(pilot)
        await send_run(pilot, step_events(ALICE, 0, step_count=2))
        # -- TEST RUN WAS STOPPED: Other scenarios were not run.
        await send_events(pilot, [{"type": "testrun_finished"}])
        app.testrun_done(False)
        await settle(pilot)
        assert not app.status_tree.failed_nodes()
        text = header_text(app)
        assert text.startswith(u"behave  ○ incomplete")
        assert u"1 passed · 2 untested" in text

    run_app(check)


def test_header_keeps_ticking_without_a_running_node():
    """REGRESSION: Spinner and clock froze in hook gaps (before_all)."""
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "testrun_started", "files": [ALICE, BOB]}])
        # pylint: disable=protected-access
        assert not app._running_nodes
        assert app.is_done is False
        before = header_text(app)

        await pilot.pause(HEADER_INTERVAL * 2)
        assert header_text(app) != before

    run_app(check)


def test_notifications_are_not_rendered_as_markup():
    """REGRESSION: "[...]" in file names was markup (and could crash)."""
    filename = u"features/a[b].feature"

    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(filename, "Alice", scenario_count=1,
                                     step_count=2)}])
        move_cursor_to(app, app.status_tree.features[filename])
        await settle(pilot)

        await pilot.press("Y")      # -- COPY: The location (with "[b]").
        await pilot.press("o")      # -- OPEN: The editor reports a problem.
        await settle(pilot)
        messages = [message for _severity, message in notifications(app)]
        assert any(filename in message for message in messages)
        assert any(u"[/red]" in message for message in messages)
        # pylint: disable=protected-access
        assert [item.markup for item in app._notifications] == [False, False]
        assert app.is_running is True

    run_app(check, on_copy=lambda text: True,
            on_open=lambda name, line: u"Cannot open: [/red] [bold]x",
            notifications=True)


def test_location_bar_truncation_counts_cells():
    """REGRESSION: Wide characters cut off the end of the location bar."""
    from rich.cells import cell_len

    wide_name = u"features/steps/" + u"ｗｉｄｅ" * 8 + u".py"
    status_tree = StatusTree()
    status_tree.apply({"type": "feature_started",
                       "feature": make_feature(ALICE, "Alice",
                                               scenario_count=1,
                                               step_count=2)})
    key = scenario_key(ALICE, 0)
    status_tree.apply({"type": "scenario_started", "filename": ALICE,
                       "key": key})
    status_tree.apply({"type": "step_started", "filename": ALICE, "key": key,
                       "line": scenario_line(0),
                       "definition": u"%s:48" % wide_name})
    step = status_tree.features[ALICE].scenarios()[0].children[0]

    text = format_locations(step, width=40)
    assert cell_len(text) <= 40
    assert cell_len(text) > 36          # -- HINT: Uses the space it has.
    assert text.startswith(u"…")
    assert text.endswith(u".py:48")

    async def check(pilot, app):
        move_cursor_to(app, step)
        await settle(pilot)
        assert cell_len(location_bar_text(app)) <= 100

    run_app(check, status_tree=status_tree)


def test_events_after_exit_are_ignored():
    async def check(pilot, app):
        await send_filter_features(pilot)
        app.exit()
        assert app.is_exiting is True

        app.handle_event({"type": "scenario_started", "filename": ALICE,
                          "key": scenario_key(ALICE, 0)})
        app.testrun_done(True)
        assert app.is_done is False
        assert select_scenario(app, ALICE, 0).state == "pending"

    run_app(check)


def test_filter_survives_a_terminal_focus_round_trip():
    """REGRESSION: A window switch left the focus on the hidden input."""
    from textual import events

    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"zone")
        assert app.filter_is_open is True

        # -- TERMINAL: Loses and gets the focus again (window/tab switch).
        app.post_message(events.AppBlur())
        await settle(pilot)
        app.post_message(events.AppFocus())
        await settle(pilot)
        assert app.filter_is_shown is True
        assert app.filter_is_open is True
        assert app.focused is app.filter_input

        # -- TYPING CONTINUES: The keys are not swallowed.
        await pilot.press("s")
        await settle(pilot)
        assert app.filter_input.value == u"zones"
        await pilot.press("escape")
        await settle(pilot)
        assert app.filter_text == u""
        assert app.filter_is_shown is False

    run_app(check)


async def run_feature_with_hook_output(pilot, output=u"before_feature: hi"):
    """Run Alice (2 failed, 1 passed) -- the feature has hook output."""
    await send_events(pilot, [
        {"type": "feature_started",
         "feature": make_feature(ALICE, "Alice", scenario_count=3,
                                 step_count=2)}])
    await send_run(pilot, step_events(ALICE, 0, failed_step=0, step_count=2))
    await send_run(pilot, step_events(ALICE, 1, failed_step=0, step_count=2))
    await send_run(pilot, step_events(ALICE, 2, step_count=2))
    await send_events(pilot, [
        {"type": "feature_finished", "filename": ALICE, "status": "failed",
         "duration": 1.0, "output": output,
         "statuses": dict((scenario_key(ALICE, index), status)
                          for index, status in enumerate(
                              ["failed", "failed", "passed"]))}])


def test_rerun_keeps_the_state_of_a_feature_with_hook_output():
    """REGRESSION: A rerun collapsed everything in the feature."""
    async def check(pilot, app):
        # pylint: disable=protected-access
        await run_feature_with_hook_output(pilot)
        app.testrun_done(True)
        await settle(pilot)
        feature = app.status_tree.features[ALICE]
        assert any(child.kind == "detail" for child in feature.children)

        # -- USER: Collapses the first failure and expands the passed one.
        first_failure = select_scenario(app, ALICE, 0)
        passed_scenario = select_scenario(app, ALICE, 2)
        select_tree_node(app, first_failure).collapse()
        await settle(pilot)
        select_tree_node(app, passed_scenario).expand()
        await settle(pilot)
        assert id(first_failure) in app._auto_expanded

        # -- RERUN: Of another scenario (the feature drops its hook output).
        await send_events(pilot, [
            {"type": "rerun_started",
             "locations": [scenario_key(ALICE, 1)]}])
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=3,
                                     step_count=2)}])
        assert not any(child.kind == "detail" for child in feature.children)
        # -- THE OTHER SCENARIOS: Keep their state (they were not rerun).
        assert select_tree_node(app, passed_scenario).is_expanded is True
        assert select_tree_node(app, first_failure).is_expanded is False
        assert id(first_failure) in app._auto_expanded

        # -- AUTO-EXPAND (once): Does not come back with a view mode.
        await pilot.press("v")
        await settle(pilot)
        assert app.view_mode == "failed"
        assert select_tree_node(app, first_failure).is_expanded is False

    run_app(check)


def test_cursor_keys_of_the_tree_forget_the_wanted_line():
    """REGRESSION: end/home/page-up did not clear the remembered line."""
    async def check(pilot, app):
        # pylint: disable=protected-access
        await send_filter_features(pilot)
        alice_scenario = select_scenario(app, ALICE, 0)
        app._reveal(alice_scenario)
        await settle(pilot)
        move_cursor_to(app, alice_scenario)
        await settle(pilot)

        # -- FILTER: Hides the line under the cursor (it is remembered).
        await use_filter(pilot, u"other")
        await pilot.press("enter")
        await settle(pilot)
        assert app._wanted_cursor_id == id(alice_scenario)

        # -- USER: Moves the cursor with keys of the Tree widget.
        await pilot.press("right")      # -- EXPAND: The feature of Bob.
        await pilot.press("end")        # -- CURSOR: On the last line.
        await settle(pilot)
        bob_node = app.tree_view.cursor_node
        assert bob_node.data is select_scenario(app, BOB, 0)
        assert app._wanted_cursor_id is None

        # -- FILTER CLEARED: The cursor stays where the user has put it.
        await pilot.press("escape")
        await settle(pilot)
        assert app.tree_view.cursor_node.data is select_scenario(app, BOB, 0)

    run_app(check)


def test_collapse_all_also_forgets_the_hidden_nodes():
    """REGRESSION: Nodes hidden by a filter came back expanded."""
    async def check(pilot, app):
        await send_filter_features(pilot)
        for filename in (ALICE, BOB):
            scenario = select_scenario(app, filename, 0)
            app._reveal(scenario, expand_node=True)     # noqa: E501
        await settle(pilot)
        assert u"○ Given step 1" in visible_labels(app)

        await use_filter(pilot, u"other")   # -- HIDES: Alice.
        await pilot.press("enter")
        await settle(pilot)
        await pilot.press("c")              # -- COLLAPSE ALL:
        await settle(pilot)
        assert len(visible_labels(app)) == 1

        await pilot.press("escape")         # -- CLEAR THE FILTER:
        await settle(pilot)
        labels = visible_labels(app)
        assert len(labels) == 2             # -- ONLY: The feature lines.
        assert labels[0][1:] == u" Feature: Alice  0/2  %s" % ALICE
        assert labels[1][1:] == u" Feature: Bob  0/1  %s" % BOB

    run_app(check)


def test_rerun_keeps_the_follow_mode():
    """REGRESSION: Expand events of replaced TreeNodes stopped follow."""
    async def check(pilot, app):
        # pylint: disable=protected-access
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2,
                                     step_count=2)}])
        await send_run(pilot, step_events(ALICE, 0, failed_step=1,
                                          step_count=2))
        await send_run(pilot, step_events(ALICE, 1, step_count=2))
        app.testrun_done(True)
        await settle(pilot)
        scenario = select_scenario(app, ALICE, 0)
        failed_step = scenario.children[1]
        assert select_tree_node(app, failed_step).is_expanded is True

        # -- VIEW MODE: The rerun makes the scenario disappear (rebuild).
        await pilot.press("v")
        await settle(pilot)
        assert app.view_mode == "failed"

        await send_events(pilot, [
            {"type": "rerun_started",
             "locations": [scenario_key(ALICE, 0)]}])
        assert app.follow is True
        assert id(failed_step) not in app._user_expanded
        assert u" · follow" in header_text(app)

    run_app(check)


def test_line_shifts_keep_the_wanted_cursor_line():
    """REGRESSION: A line shift was taken for a cursor move by the user."""
    async def check(pilot, app):
        # pylint: disable=protected-access
        scenarios = [
            make_scenario(ALICE, 0, name="xray one", step_count=2),
            make_scenario(ALICE, 1, name="xray two", step_count=2),
            make_scenario(ALICE, 2, name="omega", step_count=2),
        ]
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenarios=scenarios)}])
        omega = select_scenario(app, ALICE, 2)
        app._reveal(omega)
        await settle(pilot)
        move_cursor_to(app, omega)
        await settle(pilot)

        # -- FILTER: Hides the line under the cursor (it is remembered).
        await use_filter(pilot, u"xray")
        await pilot.press("enter")
        await settle(pilot)
        assert app._wanted_cursor_id == id(omega)

        # -- FAILURE ABOVE THE CURSOR: Its details shift the cursor line.
        await send_run(pilot, step_events(ALICE, 0, failed_step=0,
                                          step_count=2))
        assert u"Assertion Failed: 1 != 2" in visible_labels(app)
        assert app._wanted_cursor_id == id(omega)

        # -- FILTER CLEARED: The cursor returns to its remembered line.
        await pilot.press("escape")
        await settle(pilot)
        assert app.tree_view.cursor_node.data is omega

    run_app(check)


def test_filter_survives_a_fast_terminal_focus_round_trip():
    """REGRESSION: AppBlur+AppFocus in one read closed the filter input."""
    from textual import events

    calls = []

    async def check(pilot, app):
        await send_filter_features(pilot)
        await use_filter(pilot, u"zone")
        assert app.filter_is_open is True

        # -- TERMINAL: Focus out and in, delivered back-to-back.
        app.post_message(events.AppBlur())
        app.post_message(events.AppFocus())
        await settle(pilot)
        assert app.filter_is_shown is True
        assert app.filter_is_open is True
        assert app.focused is app.filter_input

        # -- TYPING CONTINUES: "q" is text, not the quit command.
        await pilot.press("q")
        await settle(pilot)
        assert calls == []
        assert app.interrupting is False
        assert app.filter_input.value == u"zoneq"

    run_app(check, on_interrupt=lambda: calls.append("interrupt"))


def test_keys_do_not_act_while_the_filter_input_is_shown():
    """SAFETY NET: The keys belong to the filter input while it is shown."""
    calls = []

    async def check(pilot, app):
        await send_filter_features(pilot)
        # -- DEGENERATE STATE: The input line is shown, but the focus is
        # somewhere else (a blur that was ignored, see the finding above).
        app.filter_input.add_class("-active")
        app.tree_view.focus()
        await settle(pilot)
        assert app.filter_is_shown is True
        assert app.filter_is_open is False
        assert app.check_action("stop_or_quit", ()) is None

        await pilot.press("q")
        await settle(pilot)
        assert calls == []              # -- NOT: The quit command.
        assert app.interrupting is False
        assert app.filter_is_open is True   # -- The input has it back.

    run_app(check, on_interrupt=lambda: calls.append("interrupt"))


def test_dropped_running_nodes_do_not_keep_the_spinner_timer_alive():
    """REGRESSION: A dropped running node kept the spinner timer running."""
    async def check(pilot, app):
        # pylint: disable=protected-access
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2,
                                     step_count=2)}])
        # -- FILTER: Hides everything (running nodes without a TreeNode).
        await use_filter(pilot, u"no-match-at-all")
        await pilot.press("enter")
        await settle(pilot)
        await send_events(pilot, start_scenario_events(ALICE, 0))
        scenario = select_scenario(app, ALICE, 0)
        step = scenario.children[0]
        assert select_tree_node(app, scenario) is None
        assert id(scenario) in app._running_nodes
        assert id(step) in app._running_nodes
        assert app._spinner_timer._active.is_set() is True

        # -- RERUN: Its feature file was changed (its nodes are dropped).
        await send_events(pilot, [
            {"type": "rerun_started", "locations": [ALICE],
             "changed_features": [ALICE]}])
        alive = set(id(node) for node in app.status_tree.root.walk())
        assert id(scenario) not in alive
        assert set(app._running_nodes) <= alive
        # -- SPINNER TIMER: Nothing is running any more (it may sleep).
        assert app.is_done is False
        assert app._spinner_timer._active.is_set() is False

    run_app(check)


def test_header_verdict_is_passed_with_skipped_scenarios():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2,
                                     step_count=2)}])
        await send_run(pilot, step_events(ALICE, 0, step_count=2))
        await send_events(pilot, [
            {"type": "scenario_finished", "filename": ALICE,
             "key": scenario_key(ALICE, 1), "status": "skipped",
             "duration": 0.0},
            {"type": "testrun_finished"}])
        app.testrun_done(False)
        await settle(pilot)
        text = header_text(app)
        assert text.startswith(u"behave  ✔ passed  1 passed · 1 skipped")

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Copy to clipboard
# -----------------------------------------------------------------------------
def test_copy_report_of_a_failed_step():
    copied = []

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        scenario = app.status_tree.features[ALICE].scenarios()[0]
        move_cursor_to(app, scenario.children[1])       # -- The failed step.
        await settle(pilot)

        await pilot.press("y")
        await settle(pilot)
        assert copied == [u"\n".join([
            u"When step 2  [failed]",
            u"  %s:%d -> %s" % (ALICE, scenario_line(0) + 1,
                                step_definition(ALICE, 0, 1)),
            u"    Assertion Failed: 1 != 2",
            u"    File 'example.py':42",
        ])]
        assert any(u"Copied: 4 lines" in message
                   for _severity, message in notifications(app))

    run_app(check, on_copy=lambda text: copied.append(text) or True)


def test_copy_location_of_a_step_and_a_feature():
    copied = []

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        scenario = app.status_tree.features[ALICE].scenarios()[0]
        move_cursor_to(app, scenario.children[0])
        await settle(pilot)
        await pilot.press("Y")
        await settle(pilot)
        assert copied[-1] == u"%s:%d -> %s" % (ALICE, scenario_line(0),
                                               step_definition(ALICE, 0, 0))

        move_cursor_to(app, app.status_tree.features[ALICE])
        await settle(pilot)
        await pilot.press("Y")
        await settle(pilot)
        assert copied[-1] == u"%s:1" % ALICE

    run_app(check, on_copy=lambda text: copied.append(text) or True)


@pytest.mark.parametrize("on_copy_name", ["none", "false", "raises"])
def test_copy_falls_back_to_the_terminal(on_copy_name, monkeypatch):
    clipboard = []
    monkeypatch.setattr(LiveApp, "copy_to_clipboard",
                        lambda self, text: clipboard.append(text))

    def on_copy_false(text):
        return False

    def on_copy_raises(text):
        raise RuntimeError("OOPS: no clipboard")

    on_copy = {"none": None, "false": on_copy_false,
               "raises": on_copy_raises}[on_copy_name]

    async def check(pilot, app):
        await run_one_failure(pilot, scenario_count=1)
        move_cursor_to(app, app.status_tree.features[ALICE])
        await settle(pilot)

        await pilot.press("Y")
        await settle(pilot)
        assert clipboard == [u"%s:1" % ALICE]

    run_app(check, on_copy=on_copy)


def test_copy_does_not_stop_the_follow_mode():
    copied = []

    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=1,
                                     step_count=2)}])
        await send_events(pilot, start_scenario_events(ALICE, 0))
        assert app.follow is True

        await pilot.press("y")
        await pilot.press("Y")
        await settle(pilot)
        assert len(copied) == 2
        assert app.follow is True

    run_app(check, on_copy=lambda text: copied.append(text) or True)


# -----------------------------------------------------------------------------
# TESTS: Help overlay and footer
# -----------------------------------------------------------------------------
def help_text(app):
    from textual.widgets import Static
    screen = app.screen
    assert isinstance(screen, HelpScreen), "REQUIRE: Help is shown"
    return u"\n".join(widget.content.plain
                      for widget in screen.query(Static))


@pytest.mark.parametrize("close_key", ["question_mark", "escape", "q"])
def test_help_shows_the_keys_and_is_closed_again(close_key):
    calls = []

    async def check(pilot, app):
        await send_filter_features(pilot)
        await pilot.press("question_mark")
        await settle(pilot)
        text = help_text(app)
        for key in (u"↑/↓, k/j", u"n / N", u"/", u"v", u"o", u"d", u"y",
                    u"Y", u"r", u"R", u"q, ctrl+c", u"ctrl+q", u"?"):
            assert key in text
        assert u"follow mode" in text

        await pilot.press(close_key)
        await settle(pilot)
        assert isinstance(app.screen, HelpScreen) is False
        assert app.dialog_is_open is False
        # -- HINT: "q" must not stop the test run while the help is shown.
        assert calls == []
        assert app.interrupting is False
        assert app.is_running is True

    run_app(check, on_interrupt=lambda: calls.append("interrupt"))


def test_footer_shows_the_essential_keys():
    async def check(pilot, app):
        await send_filter_features(pilot)
        footer = footer_text(app)
        assert footer == (u"← collapse  → expand  q quit  / filter  v view"
                          u"  n next fail  f follow  ? help")

        app.testrun_done(True)
        await settle(pilot)
        # -- AFTER THE TEST RUN: No follow mode, but the rerun key.
        assert footer_text(app) == (
            u"← collapse  → expand  q quit  / filter  v view"
            u"  n next fail  r rerun  ? help")

    run_app(check, on_rerun=make_rerun_callback([]), size=(100, 40))


# -----------------------------------------------------------------------------
# TESTS: Header and print-capture
# -----------------------------------------------------------------------------
def test_header_while_running_and_after_testrun_done():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "feature_started",
             "feature": make_feature(ALICE, "Alice", scenario_count=2)}])
        await send_events(pilot, step_events(ALICE, 0, failed_step=1))
        text = header_text(app)
        assert text.startswith(u"behave  ")
        assert is_spinner_icon(text[len(u"behave  "):])
        assert u" running  1/2 scenarios · 1 failed · 00:0" in text

        await send_events(pilot, step_events(ALICE, 1))
        app.testrun_done(True)
        await settle(pilot)
        text = header_text(app)
        assert text.startswith(
            u"behave  ✘ failed  1 passed · 1 failed · 00:0")

    run_app(check)


def test_header_after_crashed_testrun():
    async def check(pilot, app):
        app.testrun_done(None)
        await settle(pilot)
        assert header_text(app).startswith(u"behave  ✘ crashed  0 passed")

    run_app(check)


def test_capture_stream_flush_does_not_raise():
    """REGRESSION: A LiveApp method must not shadow App internals.

    Textual's ``_PrintCapture`` (its stdout/stderr proxy) calls
    ``App._print(text, stderr=...)`` and ``App._flush(stderr=...)``,
    for example when a logging.StreamHandler is flushed at exit.
    """
    async def check(pilot, app):
        # pylint: disable=protected-access
        app._capture_stdout.flush()
        app._capture_stderr.flush()
        app._capture_stderr.write(u"OOPS\n")
        await settle(pilot)
        assert (u"OOPS\n", True) in app.captured_output

    run_app(check)


@pytest.mark.parametrize("this_class, base_class, intended", [
    (LiveApp, "textual.app.App", {"__init__", "compose", "check_action"}),
    (ReloadDialog, "textual.screen.ModalScreen", {"__init__", "compose"}),
    (HelpScreen, "textual.screen.ModalScreen", {"compose"}),
    (FilterInput, "textual.widgets.Input", set()),
])
def test_no_name_clash_with_textual(this_class, base_class, intended):
    """REGRESSION: Do not override Textual internals (by name)."""
    import importlib
    import inspect

    module_name, class_name = base_class.rsplit(".", 1)
    base = getattr(importlib.import_module(module_name), class_name)
    reserved = set()
    for klass in base.__mro__:
        reserved |= set(vars(klass))
    own_names = set(name for name, value in vars(this_class).items()
                    if inspect.isfunction(value)
                    or isinstance(value, property))
    assert (own_names & reserved) - intended == set()


def test_print_capture_from_background_thread():
    async def check(pilot, app):
        def print_some_text():
            print("HELLO from the runner thread")

        thread = threading.Thread(target=print_some_text)
        thread.start()
        thread.join()
        await settle(pilot)
        captured = u"".join(text for text, _stderr in app.captured_output)
        assert u"HELLO from the runner thread" in captured
        assert all(isinstance(is_stderr, bool)
                   for _text, is_stderr in app.captured_output)

    run_app(check)


def test_unknown_or_odd_events_are_ignored():
    async def check(pilot, app):
        await send_events(pilot, [
            {"type": "unknown_event", "data": 42},
            {},
            {"type": "scenario_started"},            # -- MISSING: filename.
            {"type": "scenario_started", "filename": ALICE,
             "key": "UNKNOWN"},                      # -- UNKNOWN: key.
            {"type": "testrun_started", "files": [ALICE]},
        ])
        assert visible_labels(app) == [u"○ %s" % ALICE]
        assert app.is_running is True

    run_app(check)


# -----------------------------------------------------------------------------
# TESTS: Performance (sanity check)
# -----------------------------------------------------------------------------
def test_many_events_burst_is_handled():
    """Sanity check: A burst of 2000 scenario runs must be fast enough.

    HINT: The scenarios are spread over some features, because
    :meth:`StatusTree.apply()` needs O(feature_size) per scenario event.
    """
    feature_count = 10
    scenario_count = 200
    filenames = ["features/many_%02d.feature" % index
                 for index in range(feature_count)]

    async def check(pilot, app):
        start_time = time.time()
        events = [{"type": "testrun_started", "files": filenames}]
        for filename in filenames:
            events.append({"type": "feature_started",
                           "feature": make_feature(filename, filename,
                                                   scenario_count, 2)})
            for index in range(scenario_count):
                key = scenario_key(filename, index)
                events.append({"type": "scenario_started",
                               "filename": filename, "key": key})
                status = "failed" if index == 3 else "passed"
                events.append({"type": "scenario_finished",
                               "filename": filename, "key": key,
                               "status": status, "duration": 0.01})
        for event in events:
            app.handle_event(event)
        await settle(pilot)
        app.testrun_done(True)
        await settle(pilot)
        duration = time.time() - start_time

        counts = app.status_tree.counts()
        assert counts["total"] == feature_count * scenario_count
        assert counts[FAILED] == feature_count
        assert len(visible_labels(app)) >= feature_count
        assert duration < 5.0, "TOO SLOW: %.2fs" % duration

    run_app(check)
