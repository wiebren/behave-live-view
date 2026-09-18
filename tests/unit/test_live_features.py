# -*- coding: UTF-8 -*-
"""
Unit tests for the "live" formatter: filter, step data, text report,
summary of failures and clipboard support.
"""

import io
import subprocess

import pytest

from behave_live_view import clipboard, model
from behave_live_view.formatter import (
    make_feature_outline, make_scenario_data, make_step_data,
)
from behave_live_view.plain import write_failures
from behave.parser import parse_feature


FEATURE_TEXT = u"""
@dns
Feature: DNS zones

  @smoke
  Scenario: Create zone
    Given a zone with records:
      | type | content |
      | A    | 1.2.3.4 |
    When I send:
      \"\"\"
      {"name": "example.org"}
        indented
      \"\"\"

  Scenario Outline: Delete <kind> zone
    Given a step passes

    Examples:
      | kind   | ttl |
      | master | 300 |
      | slave  | 60  |
"""


@pytest.fixture
def feature():
    return parse_feature(FEATURE_TEXT.strip(), filename="features/dns.feature")


@pytest.fixture
def tree(feature):
    status_tree = model.StatusTree()
    status_tree.apply({"type": "testrun_started",
                       "features": [make_feature_outline(feature)]})
    other = model.make_outline(
        model.KIND_FEATURE, "features/users.feature", u"Feature", u"Users", 1,
        [model.make_outline(model.KIND_SCENARIO, "features/users.feature:3",
                            u"Scenario", u"Create user", 3, tags=["smoke"])])
    status_tree.apply({"type": "testrun_started", "features": [other]})
    return status_tree


def visible_names(tree, text=None, states=None):
    visible = model.select_visible_nodes(tree.root, text, states)
    if visible is None:
        return None
    return [node.name for node in tree.root.walk()
            if id(node) in visible and node.kind != model.KIND_ROOT]


# -----------------------------------------------------------------------------
# STEP DATA: Table, text, example row
# -----------------------------------------------------------------------------
class TestStepData:
    def test_table_is_formatted_with_aligned_columns(self, feature):
        step = feature.scenarios[0].steps[0]
        assert make_step_data(step) == [u"| type | content |",
                                        u"| A    | 1.2.3.4 |"]

    def test_text_keeps_its_indentation(self, feature):
        step = feature.scenarios[0].steps[1]
        assert make_step_data(step) == [
            u'"""', u'{"name": "example.org"}', u"  indented", u'"""']

    def test_step_without_data(self, feature):
        step = feature.scenarios[1].scenarios[0].steps[0]
        assert make_step_data(step) == []

    def test_example_row_of_scenario_outline(self, feature):
        scenario = feature.scenarios[1].scenarios[1]
        assert make_scenario_data(scenario) == [u"Example: kind=slave, ttl=60"]
        assert make_scenario_data(feature.scenarios[0]) == []

    def test_data_becomes_detail_lines_of_the_nodes(self, tree):
        feature_node = tree.features["features/dns.feature"]
        scenario = feature_node.scenarios()[0]
        step = [child for child in scenario.children
                if child.kind == model.KIND_STEP][0]
        assert [(child.keyword, child.name) for child in step.children] == [
            (model.DETAIL_DATA, u"| type | content |"),
            (model.DETAIL_DATA, u"| A    | 1.2.3.4 |")]
        example = feature_node.scenarios()[1]
        assert example.children[0].name == u"Example: kind=master, ttl=300"
        assert example.children[0].keyword == model.DETAIL_DATA

    def test_step_with_data_still_gets_error_and_output(self, tree):
        scenario = tree.features["features/dns.feature"].scenarios()[0]
        step = [child for child in scenario.children
                if child.kind == model.KIND_STEP][0]
        tree.apply({"type": "step_finished", "filename": "features/dns.feature",
                    "key": scenario.key, "line": step.key, "status": "failed",
                    "error": u"XFAIL", "output": u"stdout:\nhello"})
        assert [child.keyword for child in step.children] == [
            model.DETAIL_DATA, model.DETAIL_DATA, model.DETAIL_ERROR,
            model.DETAIL_OUTPUT, model.DETAIL_OUTPUT]

        # -- RERUN: Keeps the data, drops error and output.
        tree.apply({"type": "testrun_finished"})
        tree.apply({"type": "rerun_started", "locations": [scenario.key]})
        assert [child.keyword for child in step.children] == \
               [model.DETAIL_DATA] * 2
        assert step.state == model.PENDING

    def test_tags_are_stored_as_plain_strings(self, tree):
        feature_node = tree.features["features/dns.feature"]
        assert feature_node.tags == ["dns"]
        assert feature_node.scenarios()[0].tags == ["smoke"]
        assert all(type(tag) is str for tag in feature_node.tags)


# -----------------------------------------------------------------------------
# FILTER:
# -----------------------------------------------------------------------------
class TestFilter:
    def test_no_filter_shows_everything(self, tree):
        assert model.select_visible_nodes(tree.root) is None
        assert model.select_visible_nodes(tree.root, u"  ", None) is None

    def test_text_selects_scenarios_and_their_ancestors(self, tree):
        assert visible_names(tree, u"create zone") == \
               [u"DNS zones", u"Create zone"]

    def test_matching_feature_shows_all_its_scenarios(self, tree):
        names = visible_names(tree, u"dns")
        assert u"Create zone" in names and u"Users" not in names
        assert u"Delete slave zone -- @1.2 " in names or \
            any(name.startswith(u"Delete slave zone") for name in names)

    def test_feature_filename_matches(self, tree):
        assert u"Create user" in visible_names(tree, u"users.feature")

    def test_words_are_combined_with_and(self, tree):
        # -- HINT: One word matches the feature, the other the scenario.
        assert visible_names(tree, u"dns create") == \
               [u"DNS zones", u"Create zone"]
        assert visible_names(tree, u"dns user") == []

    def test_tag_filter(self, tree):
        names = visible_names(tree, u"@smoke")
        assert u"Create zone" in names and u"Create user" in names
        assert not any(name.startswith(u"Delete") for name in names)
        # -- INHERITED: Tag of the feature selects all its scenarios.
        assert len(visible_names(tree, u"@dns")) == 5

    def test_text_is_case_insensitive(self, tree):
        assert visible_names(tree, u"CREATE ZONE") == \
               visible_names(tree, u"create zone")

    def test_state_filter_selects_scenarios_by_state(self, tree):
        scenario = tree.features["features/dns.feature"].scenarios()[0]
        tree.apply({"type": "scenario_finished",
                    "filename": "features/dns.feature", "key": scenario.key,
                    "status": "failed", "duration": 0})
        assert visible_names(tree, None, (model.FAILED,)) == \
               [u"DNS zones", u"Create zone"]
        assert visible_names(tree, u"user", (model.FAILED,)) == []

    def test_feature_that_is_not_parsed_yet_is_shown_by_its_filename(self):
        tree = model.StatusTree()
        tree.apply({"type": "testrun_started",
                    "files": ["features/a.feature", "features/b.feature"]})
        assert visible_names(tree, u"a.feature") == \
               [u"features/a.feature"]
        assert visible_names(tree, None, (model.FAILED,)) == []

    def test_steps_and_details_are_never_selected(self, tree):
        visible = model.select_visible_nodes(tree.root, u"zone with records")
        assert visible == set()

    def test_view_modes(self):
        assert list(model.VIEW_MODES) == ["all", "failed", "not passed"]
        assert model.VIEW_MODES["all"] is None
        assert model.PASSED not in model.VIEW_MODES["not passed"]


# -----------------------------------------------------------------------------
# TEXT REPORT + SUMMARY OF FAILURES:
# -----------------------------------------------------------------------------
@pytest.fixture
def failed_tree(tree):
    filename = "features/dns.feature"
    scenario = tree.features[filename].scenarios()[0]
    steps = [child for child in scenario.children
             if child.kind == model.KIND_STEP]
    tree.apply({"type": "step_started", "filename": filename,
                "key": scenario.key, "line": steps[0].key,
                "definition": "features/steps/dns_steps.py:12"})
    tree.apply({"type": "step_finished", "filename": filename,
                "key": scenario.key, "line": steps[0].key, "status": "failed",
                "error": u"ASSERT FAILED: zone exists\nsecond line",
                "output": u"stdout:\nPOST /zones -> 409"})
    tree.apply({"type": "scenario_finished", "filename": filename,
                "key": scenario.key, "status": "failed", "duration": 0.1})
    return tree


class TestTextReport:
    def test_report_of_a_failed_step(self, failed_tree):
        scenario = failed_tree.failed_nodes()[0]
        step = [child for child in scenario.children
                if child.kind == model.KIND_STEP][0]
        assert model.make_text_report(step) == [
            u"Given a zone with records:  [failed]",
            u"  features/dns.feature:6 -> features/steps/dns_steps.py:12",
            u"    | type | content |",
            u"    | A    | 1.2.3.4 |",
            u"    ASSERT FAILED: zone exists",
            u"    second line",
            u"    stdout:",
            u"    POST /zones -> 409",
        ]

    def test_report_of_a_detail_line_is_the_report_of_its_step(
            self, failed_tree):
        scenario = failed_tree.failed_nodes()[0]
        step = [child for child in scenario.children
                if child.kind == model.KIND_STEP][0]
        assert model.make_text_report(step.children[-1]) == \
               model.make_text_report(step)

    def test_report_of_a_scenario_contains_its_steps(self, failed_tree):
        lines = model.make_text_report(failed_tree.failed_nodes()[0])
        assert lines[0] == u"Scenario: Create zone  [failed]"
        assert lines[1] == u"  features/dns.feature:5"
        assert u"  Given a zone with records:  [failed]" in lines
        assert any(u"When I send:" in line for line in lines)

    def test_report_of_a_feature_contains_only_failures(self, failed_tree):
        feature_node = failed_tree.features["features/dns.feature"]
        text = u"\n".join(model.make_text_report(feature_node))
        assert u"Feature: DNS zones" in text
        assert u"Create zone" in text and u"Delete" not in text

    def test_report_of_root_is_empty(self, failed_tree):
        assert model.make_text_report(failed_tree.root) == []

    @pytest.mark.parametrize("location, expected", [
        (("a.feature", 3), u"a.feature:3"), (("a.feature", None), u"a.feature"),
        (None, u""),
    ])
    def test_format_location(self, location, expected):
        assert model.format_location(location) == expected


class TestWriteFailures:
    def test_writes_what_failed_where_and_why(self, failed_tree):
        stream = io.StringIO()
        assert write_failures(failed_tree, stream) == 1
        assert stream.getvalue().splitlines() == [
            u"Failures:",
            u"\u2718 DNS zones \u203a Scenario: Create zone  "
            u"features/dns.feature:5",
            u"    \u2718 Given a zone with records:  features/dns.feature:6 "
            u"\u2192 features/steps/dns_steps.py:12",
            u"      ASSERT FAILED: zone exists",
            u"      second line",
            u"",
        ]

    def test_writes_nothing_without_failures(self, tree):
        stream = io.StringIO()
        assert write_failures(tree, stream) == 0
        assert stream.getvalue() == u""

    def test_colored_output_uses_ansi_colors(self, failed_tree):
        stream = io.StringIO()
        write_failures(failed_tree, stream, colored=True)
        assert u"\x1b[31m" in stream.getvalue()


# -----------------------------------------------------------------------------
# CLIPBOARD:
# -----------------------------------------------------------------------------
class TestClipboard:
    @pytest.mark.parametrize("platform, environ, expected", [
        ("darwin", {}, [["pbcopy"]]),
        ("win32", {}, [["clip"]]),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, [["wl-copy"]]),
        ("linux", {"DISPLAY": ":0"},
         [["xclip", "-selection", "clipboard"],
          ["xsel", "--clipboard", "--input"]]),
        ("linux", {}, []),
    ])
    def test_select_clipboard_commands(self, platform, environ, expected):
        assert clipboard.select_clipboard_commands(platform, environ) == \
               expected

    def test_copy_uses_the_first_tool_that_works(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs["input"]))
            return subprocess.CompletedProcess(command, 0)

        copied = clipboard.copy_to_clipboard(
            u"h\u00e9llo", commands=[["missing"], ["xclip", "-sel", "c"]],
            which=lambda name: None if name == "missing" else "/bin/" + name,
            run=run)
        assert copied is True
        assert calls == [(["xclip", "-sel", "c"], u"h\u00e9llo".encode("utf-8"))]

    def test_windows_clip_gets_utf16(self):
        # -- HINT: clip.exe decodes bytes with the console code page.
        calls = []

        def run(command, **kwargs):
            calls.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0)

        clipboard.copy_to_clipboard(u"h\u00e9llo", commands=[["clip"]],
                                    which=lambda name: name, run=run)
        assert calls == [u"h\u00e9llo".encode("utf-16")]

    def test_copy_fails_without_a_tool(self):
        assert clipboard.copy_to_clipboard(u"x", commands=[]) is False
        assert clipboard.copy_to_clipboard(
            u"x", commands=[["pbcopy"]], which=lambda name: None) is False

    def test_copy_fails_if_the_tool_fails(self):
        def run_failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1)

        def run_broken(command, **kwargs):
            raise OSError("XFAIL")

        for run in (run_failing, run_broken):
            assert clipboard.copy_to_clipboard(
                u"x", commands=[["pbcopy"]], which=lambda name: name,
                run=run) is False


# -----------------------------------------------------------------------------
# REGRESSIONS: Found by review
# -----------------------------------------------------------------------------
class TestReviewFindings:
    def test_many_changed_nodes_are_marked_fast(self):
        # -- WAS QUADRATIC: Stopping a large test run seemed to hang.
        import time
        update = model.Update()
        nodes = [model.Node(model.KIND_SCENARIO, index)
                 for index in range(20000)]
        start = time.time()
        for node in nodes + nodes:
            update.mark_changed(node)
        assert len(update.changed) == 20000
        assert time.time() - start < 2.0

    def test_rerun_replaces_the_feature_details_of_the_last_run(self, tree):
        filename = "features/dns.feature"
        feature_node = tree.features[filename]
        scenario = feature_node.scenarios()[0]

        def run_feature(output):
            tree.apply({"type": "feature_started",
                        "feature": make_feature_outline_of(feature_node)})
            tree.apply({"type": "feature_finished", "filename": filename,
                        "status": "passed", "duration": 0, "statuses": {},
                        "output": output})
            tree.apply({"type": "testrun_finished"})

        run_feature(u"before_feature stdout:\nRUN 1")
        for run in (2, 3):
            tree.apply({"type": "rerun_started", "locations": [scenario.key]})
            run_feature(u"before_feature stdout:\nRUN %d" % run)
        details = [child.name for child in feature_node.children
                   if child.kind == model.KIND_DETAIL]
        assert details == [u"before_feature stdout:", u"RUN 3"]

    def test_feature_failed_by_hook_error_is_shown_in_failed_view(self, tree):
        filename = "features/dns.feature"
        feature_node = tree.features[filename]
        statuses = dict((node.key, "passed")
                        for node in feature_node.scenarios())
        tree.apply({"type": "feature_finished", "filename": filename,
                    "status": "hook_error", "duration": 0,
                    "statuses": statuses})
        assert feature_node.state == model.FAILED
        assert tree.failed_nodes() == [feature_node]
        names = visible_names(tree, None, model.VIEW_MODES["failed"])
        assert names[0] == u"DNS zones" and u"Users" not in names
        assert u"Create zone" in names   # -- ITS LINES: Can be looked at.
        assert visible_names(tree, u"users", model.VIEW_MODES["failed"]) == []

    def test_output_line_that_starts_with_dashes_is_kept(self):
        from behave_live_view.formatter import make_output_text
        text = u"----\nCAPTURED STDOUT: step\n---- BEGIN REQUEST\n--data\n----"
        assert make_output_text(text).splitlines() == [
            u"stdout:", u"---- BEGIN REQUEST", u"--data"]

    def test_failures_by_hook_error_are_written_with_their_reason(self, tree):
        filename = "features/dns.feature"
        scenario = tree.features[filename].scenarios()[0]
        tree.apply({"type": "scenario_finished", "filename": filename,
                    "key": scenario.key, "status": "hook_error",
                    "duration": 0,
                    "output": u"before_scenario stderr:\n"
                              u"HOOK-ERROR in before_scenario: XFAIL"})
        stream = io.StringIO()
        write_failures(tree, stream)
        output = stream.getvalue()
        assert u"Scenario: Create zone  [hook_error]" in output
        assert u"    HOOK-ERROR in before_scenario: XFAIL" in output
        assert u"| type | content |" not in output  # -- NOT: The data lines.


def make_feature_outline_of(feature_node):
    """Outline of a feature node that exists (children are kept)."""
    return model.make_outline(model.KIND_FEATURE, feature_node.key,
                              feature_node.keyword, feature_node.name,
                              feature_node.line)


class TestPlainOutputEncoding:
    """REGRESSION: UnicodeEncodeError on a console/pipe without UTF-8."""

    @staticmethod
    def make_stream(encoding):
        return io.TextIOWrapper(io.BytesIO(), encoding=encoding,
                                errors="strict", write_through=True)

    @pytest.mark.parametrize("encoding", ["ascii", "latin-1", "cp1252"])
    def test_status_lines_use_ascii_symbols(self, failed_tree, encoding):
        from behave_live_view.plain import PlainRenderer
        stream = self.make_stream(encoding)
        renderer = PlainRenderer(stream)
        renderer.status_tree = failed_tree
        feature_node = failed_tree.features["features/dns.feature"]
        feature_node.name = u"DNS z\u00f6nes \u2714"
        renderer.write_feature(feature_node)    # -- SHOULD NOT RAISE.
        write_failures(failed_tree, stream)     # -- SHOULD NOT RAISE.
        text = stream.buffer.getvalue().decode(encoding)
        assert text.startswith(u"o Feature: DNS z")
        assert u"x DNS z" in text and u" -> features/steps" in text

    def test_utf8_stream_keeps_the_symbols(self, failed_tree):
        stream = self.make_stream("utf-8")
        write_failures(failed_tree, stream)
        assert u"\u2718" in stream.buffer.getvalue().decode("utf-8")
