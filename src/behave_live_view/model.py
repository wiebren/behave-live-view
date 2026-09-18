# -*- coding: UTF-8 -*-
"""
Status tree of a test run (used by: "live" formatter).

This module is free of any terminal/UI logic and of behave model objects:

* EVENTS are small, picklable dicts that describe what happens in a test run
  (they cross a thread boundary and may cross a process boundary).
* :class:`StatusTree` applies events and keeps the state of each
  feature/rule/scenario/step as a tree of :class:`Node` objects.
* A renderer (interactive or plain) shows the tree; :meth:`StatusTree.apply()`
  tells it which nodes were added or changed.

EVENT TYPES (key: "type")::

    testrun_started:   files=[filename, ...] and/or features=[outline, ...]
    feature_started:   feature=outline (see: :func:`make_outline()`)
    scenario_started:  filename, key
    step_started:      filename, key (of scenario), line,
                       definition="file:line"|None (of the step definition)
    step_finished:     filename, key (of scenario), line, status, duration,
                       error=text|None, output=text|None (captured output)
    scenario_finished: filename, key, status, duration,
                       output=text|None (captured output of its hooks)
    feature_finished:  filename, status, duration, statuses={key: status},
                       output=text|None (captured output of its hooks)
    feature_result:    filename, status, statuses={key: status} (optional).
                       Final result of a feature, sent by a runner. Fills in
                       what was not reported, like a feature that is excluded
                       by tags (it is not shown to a formatter in some cases).
    output:            filename|None, text
    testrun_finished:  (no params)
    rerun_started:     locations=[filename or "filename:line", ...]
                       (these features/scenarios are run again),
                       changed_features=[filename, ...] (feature files that
                       were changed: what they contain is not known anymore)

An OUTLINE is a nested dict: kind, key, keyword, name, line, children=[...],
tags=[...] and data=[text line, ...] -- the data of a step (table, text) or
of a scenario (values of its example row in a scenario outline).
"""

import os.path
import re
from collections import OrderedDict


# -----------------------------------------------------------------------------
# NODE STATES:
# -----------------------------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
UNTESTED = "untested"   # -- FINAL: Not run (test run was stopped/aborted).

ALL_STATES = (PENDING, RUNNING, PASSED, FAILED, SKIPPED, UNTESTED)
FINAL_STATES = (PASSED, FAILED, SKIPPED, UNTESTED)
PASSED_STATUSES = ("passed", "xfailed")
SKIPPED_STATUSES = ("skipped",)
UNTESTED_STATUSES = ("untested", "untested_pending", "untested_undefined")

# -- NODE KINDS:
KIND_ROOT = "root"
KIND_FEATURE = "feature"
KIND_RULE = "rule"
KIND_OUTLINE = "outline"    # -- ScenarioOutline (children: its scenarios).
KIND_SCENARIO = "scenario"
KIND_STEP = "step"
KIND_DETAIL = "detail"      # -- Text line: error message, output, ...

# -- DETAIL CATEGORIES (stored in: Node.keyword of a detail node):
DETAIL_ERROR = "error"
DETAIL_OUTPUT = "output"
DETAIL_DATA = "data"        # -- Step table/text, example row of a scenario.
CONTAINER_KINDS = (KIND_ROOT, KIND_FEATURE, KIND_RULE, KIND_OUTLINE)


def state_from_status(status_name, final=True):
    """Map a behave status name to a node state."""
    if status_name in PASSED_STATUSES:
        return PASSED
    if status_name in SKIPPED_STATUSES:
        return SKIPPED
    if status_name in UNTESTED_STATUSES:
        return UNTESTED if final else PENDING
    # -- ANYTHING ELSE: failed, error, hook_error, undefined, pending, ...
    return FAILED


#: ANSI escape sequences (colors, cursor movement, ...) and other control
#: characters in captured output would garble a status line.
ANSI_ESCAPE_PATTERN = re.compile(
    u"\x1b\\[[0-9;?]*[ -/]*[@-~]|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)|\x1b[@-_]")
CONTROL_CHARS_PATTERN = re.compile(u"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text_line(line):
    """Remove escape sequences/control characters from a line of output."""
    line = ANSI_ESCAPE_PATTERN.sub(u"", line)
    return CONTROL_CHARS_PATTERN.sub(u"", line.expandtabs(4))


#: File names are relative to this directory if possible, like the file
#: names of behave's model (parallel runner: uses absolute names, too).
BASE_DIR = os.getcwd()


def normalize_filename(filename):
    """Use one spelling for the name of a feature file (the key of a feature)."""
    # -- HINT: Like behave's model does it (make_relpath_if_possible()),
    # also for a file outside of this directory ("../other/a.feature").
    filename = os.path.normpath(filename)
    if os.path.isabs(filename):
        try:
            filename = os.path.relpath(filename, BASE_DIR)
        except ValueError:
            pass    # -- WINDOWS: Another drive.
    return filename


def make_outline(kind, key, keyword, name, line=None, children=None,
                 tags=None, data=None):
    """Create an outline dict (the picklable description of a node)."""
    return {
        "kind": kind, "key": key, "keyword": keyword, "name": name,
        "line": line, "children": list(children or []),
        "tags": [u"%s" % tag for tag in tags or []],
        "data": list(data or []),
    }


# -----------------------------------------------------------------------------
# FILTER: Which status lines are shown
# -----------------------------------------------------------------------------
#: View modes, as: name -> states of the scenarios that are shown (None: all).
VIEW_MODES = OrderedDict([
    ("all", None),
    ("failed", (FAILED,)),
    ("not passed", (FAILED, RUNNING, PENDING, UNTESTED)),
])


def text_matches(node, text):
    """Check if a node matches a filter text (case-insensitive).

    * "@tag":  Matches a tag of this node (start of its name).
    * other:   Matches a part of the name, keyword or feature filename.
    """
    text = text.strip().lower()
    if not text:
        return True
    if text.startswith(u"@"):
        return any(tag.lower().startswith(text[1:]) for tag in node.tags)
    parts = [node.name or u"", node.keyword or u""]
    if node.kind == KIND_FEATURE:
        parts.append(str(node.key))
    return any(text in part.lower() for part in parts)


def select_visible_nodes(root, text=None, states=None):
    """Select the nodes that a filter shows.

    A feature, rule, scenario outline or scenario is selected by the filter
    text (each word must match, the node itself or one of its ancestors) and
    by the state of its scenarios. A node is shown if it is selected or if
    it contains something that is selected. Steps and details are not
    filtered: they are shown with their scenario.

    :param text:    Filter text, words are combined with AND (or None).
    :param states:  States of the scenarios to show (or None: all).
    :return: Set of ``id(node)`` of the visible nodes, or None (no filter:
        everything is visible).
    """
    words = (text or u"").split()
    if not words and not states:
        return None

    visible = set()

    def visit(node, open_words):
        # -- WORDS: That no ancestor has matched yet (inherited matches).
        if node.kind in (KIND_STEP, KIND_DETAIL):
            return False
        if node.kind != KIND_ROOT:
            open_words = [word for word in open_words
                          if not text_matches(node, word)]
        if node.kind == KIND_SCENARIO:
            selected = (not open_words
                        and (not states or node.state in states))
        else:
            selected = False
            for child in node.children:
                if visit(child, open_words):
                    selected = True
            if (not selected and not open_words and not states
                    and not node.scenarios()):
                selected = True     # -- FEATURE: Not parsed yet (file name).
        if (not selected and states and not open_words
                and node.kind == KIND_FEATURE and node.state in states):
            # -- FEATURE ITSELF: Like a feature that failed by a hook error
            # while none of its scenarios has failed.
            selected = True
            visible.update(id(child) for child in node.walk()
                           if child.kind not in (KIND_STEP, KIND_DETAIL))
        if selected:
            visible.add(id(node))
        return selected

    visit(root, words)
    return visible


# -----------------------------------------------------------------------------
# TEXT REPORT: Of a node (copy to clipboard, summary of failures)
# -----------------------------------------------------------------------------
def format_location(location):
    """Format a (filename, line) tuple as "file:line" (or "file")."""
    if not location:
        return u""
    filename, line = location
    return u"%s:%s" % (filename, line) if line else u"%s" % filename


def make_text_report(node, with_passed=False, indent=u""):
    """Describe a node as text lines: what it is, where it is, what failed.

    * detail line:  The report of its owner (step, scenario, feature).
    * step:         Its line, locations and details (data, error, output).
    * others:       Their line and the report of their failed children
                    (with_passed: of all children).
    """
    while node is not None and node.kind == KIND_DETAIL:
        node = node.parent
    if node is None or node.kind == KIND_ROOT:
        return []

    separator = u" " if node.kind == KIND_STEP else u": "
    title = u"%s%s%s" % (node.keyword, separator, node.name)
    if not node.keyword:
        title = node.name
    lines = [u"%s%s  [%s]" % (indent, title, node.status or node.state)]
    location = format_location(node.source_location)
    if node.kind == KIND_STEP and node.definition:
        location += u" -> %s" % node.definition
    if location:
        lines.append(u"%s  %s" % (indent, location))

    for child in node.children:
        if child.kind == KIND_DETAIL:
            if node.kind == KIND_STEP or child.keyword != DETAIL_OUTPUT \
                    or node.state == FAILED:
                lines.append(u"%s    %s" % (indent, child.name))
        elif with_passed or child.state == FAILED or node.kind == KIND_SCENARIO:
            lines.extend(make_text_report(child, with_passed, indent + u"  "))
    return lines


# -----------------------------------------------------------------------------
# STATUS TREE:
# -----------------------------------------------------------------------------
class Node:
    """One status line: feature, rule, scenario outline, scenario, step, ..."""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, kind, key, keyword=u"", name=u"", line=None,
                 parent=None):
        self.kind = kind
        self.key = key
        self.keyword = keyword
        self.name = name
        self.line = line
        self.parent = parent
        self.children = []
        self.state = PENDING
        self.status = None      # -- behave status name (if known).
        self.duration = 0.0
        self.tags = []          # -- Own tags (without inherited ones).
        #: Step only: Location of its step definition, like "steps/x.py:12"
        #: (known when the step is run).
        self.definition = None
        # -- SCENARIO COUNTS: Of this subtree, per state (kept up-to-date,
        # a feature with many scenarios must not be walked for each event).
        self._scenario_counts = dict.fromkeys(ALL_STATES, 0)
        if kind == KIND_SCENARIO:
            self._scenario_counts[self.state] = 1

    # -- TREE NAVIGATION:
    def add_child(self, node):
        node.parent = self
        self.children.append(node)
        for state, count in node._scenario_counts.items():
            if count:
                for ancestor in node.ancestors():
                    ancestor._scenario_counts[state] += count
        return node

    def walk(self):
        """Iterate over this node and all its descendants (depth-first)."""
        yield self
        for child in self.children:
            for node in child.walk():
                yield node

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def scenarios(self):
        return [node for node in self.walk() if node.kind == KIND_SCENARIO]

    @property
    def feature_file(self):
        """Name of the feature file that contains this node (or None)."""
        for node in [self] + list(self.ancestors()):
            if node.kind == KIND_FEATURE:
                return node.key
        return None

    @property
    def source_location(self):
        """Where this node is written in its feature file, as tuple
        (filename, line) -- line is None if unknown; None without a feature.

        HINT: A detail line (error, output) uses the location of its owner.
        """
        node = self
        while node is not None and node.kind == KIND_DETAIL:
            node = node.parent
        filename = node.feature_file if node is not None else None
        if filename is None:
            return None
        return (filename, node.line)

    @property
    def definition_location(self):
        """Where the step definition of this step is, as tuple
        (filename, line), or None (no step, not run yet, undefined step).
        """
        node = self
        while node is not None and node.kind == KIND_DETAIL:
            node = node.parent
        definition = getattr(node, "definition", None)
        if not definition:
            return None
        filename, _, line = definition.rpartition(":")
        if filename and line.isdigit():
            return (filename, int(line))
        return (definition, None)

    @property
    def location(self):
        """Location to select this node in a test run ("file" or "file:line").

        HINT: A step (or detail) is selected by its scenario (or feature).
        """
        node = self
        while node is not None and node.kind in (KIND_STEP, KIND_DETAIL):
            node = node.parent
        if node is None or node.kind == KIND_ROOT:
            return None
        return node.key

    # -- STATE:
    @property
    def is_final(self):
        return self.state in FINAL_STATES

    @property
    def is_container(self):
        return self.kind in CONTAINER_KINDS

    def remove_children(self):
        """Remove all children (and their scenarios from the counts)."""
        for state, count in self._scenario_counts.items():
            if count and self.kind != KIND_SCENARIO:
                for ancestor in self.ancestors():
                    ancestor._scenario_counts[state] -= count
                self._scenario_counts[state] = 0
        for child in self.children:
            child.parent = None
        self.children = []

    def set_state(self, state):
        """Change the state (HINT: Use this method, not the attribute)."""
        old_state = self.state
        self.state = state
        if self.kind == KIND_SCENARIO and old_state != state:
            for node in [self] + list(self.ancestors()):
                node._scenario_counts[old_state] -= 1
                node._scenario_counts[state] += 1

    def counts(self):
        """Scenario counts of this subtree, as dict: state -> count."""
        counts = dict(self._scenario_counts)
        counts["total"] = sum(counts.values())
        counts["done"] = (counts[PASSED] + counts[FAILED] + counts[SKIPPED]
                          + counts[UNTESTED])
        return counts

    def compute_container_state(self):
        """Compute the state of a container node from its scenarios."""
        counts = self.counts()
        if counts[RUNNING]:
            return RUNNING
        if counts[PENDING]:
            # -- PARTLY DONE: Some scenarios ran, others are still pending.
            return RUNNING if counts["done"] else PENDING
        if counts[FAILED]:
            return FAILED
        if counts[PASSED]:
            return PASSED
        if counts[UNTESTED]:
            return UNTESTED
        return SKIPPED if counts[SKIPPED] else PENDING

    def __repr__(self):
        return "<Node %s:%s %s>" % (self.kind, self.key, self.state)


class Update:
    """Describes what :meth:`StatusTree.apply()` has changed."""

    def __init__(self):
        self.added = []         # -- New nodes (without their descendants).
        self.rebuilt = []       # -- Nodes whose children were replaced.
        self.changed = []       # -- Nodes with a new state/name/duration.
        self._changed_ids = set()

    def mark_changed(self, node):
        # -- HINT: Many nodes change at once when a test run is stopped.
        if id(node) not in self._changed_ids:
            self._changed_ids.add(id(node))
            self.changed.append(node)

    def __bool__(self):
        return bool(self.added or self.rebuilt or self.changed)

    __nonzero__ = __bool__


class StatusTree:
    """Keeps the state of a test run as a tree of status nodes."""

    def __init__(self):
        self.root = Node(KIND_ROOT, "root")
        self.features = OrderedDict()   # -- filename -> feature node
        self._scenarios = {}            # -- (filename, key) -> scenario node
        self._rerun_features = set()    # -- Features with details to drop.
        self._ignored_scenarios = set() # -- Not part of this (re)run.
        self._is_rerun = False
        self._rerun_scenarios = set()   # -- id(scenario): Run again now.
        self._rerun_whole_features = set()  # -- Keys: All of it runs again.
        self.output = []                # -- Text without a feature.
        self.finished = False
        #: Where the test run is now: Running step or scenario (or None).
        #: HINT: Parallel test run -- the one that was started last.
        self.execution_point = None

    # -- EVENT PROCESSING:
    def apply(self, event):
        """Apply one event.

        :param event:  Event to apply (as dict).
        :return: Update object that describes the changed nodes.
        """
        update = Update()
        handler = getattr(self, "_on_%s" % event.get("type"), None)
        if handler is not None:
            handler(event, update)
        return update

    def _on_testrun_started(self, event, update):
        for filename in event.get("files", ()):
            self._ensure_feature(filename, update)
        for outline in event.get("features", ()):
            self._use_feature_outline(outline, update)

    def _on_feature_started(self, event, update):
        feature = self._use_feature_outline(event["feature"], update)
        self._drop_details_of_last_run(feature, update)
        self._set_state(feature, RUNNING, update)

    def _drop_details_of_last_run(self, feature, update):
        """RERUN: Drop the details of the last run of a feature (hook output,
        error), they are provided again by this run.

        HINT: Done once, by the first event of the feature in this run
        (parallel run: its output may arrive before it is started).
        """
        if feature.key not in self._rerun_features:
            return
        self._rerun_features.discard(feature.key)
        if self._has_details(feature):
            feature.children = [child for child in feature.children
                                if child.kind != KIND_DETAIL]
            update.rebuilt.append(feature)

    def _on_scenario_started(self, event, update):
        scenario = self._select_scenario(event, update)
        if scenario is None:
            return
        if scenario.is_final:
            # -- RERUN OF OTHER SCENARIOS: This one is not run again, it is
            # only shown as skipped. Keep its result (it may have failed).
            self._ignored_scenarios.add(id(scenario))
            return
        self._ignored_scenarios.discard(id(scenario))
        self._set_state(scenario, RUNNING, update)
        self._update_ancestors(scenario, update)
        self.execution_point = scenario

    def _on_step_started(self, event, update):
        scenario = self._select_scenario(event, update)
        if scenario is None or id(scenario) in self._ignored_scenarios:
            return
        step = self._select_step(scenario, event["line"])
        if step is not None:
            step.definition = event.get("definition")
            self._set_state(step, RUNNING, update)
            self.execution_point = step

    def _on_step_finished(self, event, update):
        scenario = self._select_scenario(event, update)
        if scenario is None or id(scenario) in self._ignored_scenarios:
            return
        step = self._select_step(scenario, event["line"])
        if step is None:
            return
        step.status = event["status"]
        step.duration = event.get("duration") or 0.0
        self._set_state(step, state_from_status(step.status), update)
        if not self._has_details(step, exclude=DETAIL_DATA):
            if event.get("error"):
                self._add_details(step, event["error"], update, DETAIL_ERROR)
            if event.get("output"):
                self._add_details(step, event["output"], update)
        if self.execution_point is step:
            self.execution_point = scenario

    def _on_scenario_finished(self, event, update):
        scenario = self._select_scenario(event, update)
        if scenario is None:
            return
        if id(scenario) in self._ignored_scenarios:
            self._ignored_scenarios.discard(id(scenario))
            return
        self._finish_scenario(scenario, event["status"],
                              event.get("duration"), update)
        if (event.get("output")
                and not self._has_details(scenario, exclude=DETAIL_DATA)):
            self._add_details(scenario, event["output"], update)
        self._update_ancestors(scenario, update)
        if self.execution_point in [scenario] + scenario.children:
            self.execution_point = None

    def _on_feature_finished(self, event, update):
        feature = self._ensure_feature(event["filename"], update)
        scenarios = dict((node.key, node) for node in feature.scenarios())
        for key, status in (event.get("statuses") or {}).items():
            scenario = scenarios.get(key)
            if scenario is not None and not scenario.is_final:
                self._finish_scenario(scenario, status, None, update)
        for scenario in scenarios.values():
            if not scenario.is_final:
                self._finish_scenario(scenario, "untested", None, update)

        feature.status = event.get("status")
        feature.duration = event.get("duration") or 0.0
        if event.get("output"):
            self._add_details(feature, event["output"], update)
        for node in feature.walk():
            if node.is_container:
                self._set_state(node, node.compute_container_state(), update)
        if feature.status is not None and not feature.scenarios():
            self._set_state(feature, state_from_status(feature.status), update)
        elif (feature.status is not None and feature.state != FAILED
              and state_from_status(feature.status) == FAILED):
            # -- CASE: Feature failed without a failed scenario (hook-error).
            self._set_state(feature, FAILED, update)
        update.mark_changed(feature)

    def _on_feature_result(self, event, update):
        feature = self._ensure_feature(event["filename"], update)
        status = event.get("status")
        if status is None:
            return
        statuses = event.get("statuses") or {}
        # -- HINT: Without the status of a scenario, only a skipped feature
        # tells something about it. Otherwise its own events will (they may
        # arrive later in a parallel run) or it stays "not run".
        default_status = status if status in SKIPPED_STATUSES else None
        changed = False
        for scenario in feature.scenarios():
            # -- HINT: UNTESTED means "not reported" if the test run has
            # ended already (then this is the first word about it).
            if scenario.is_final and scenario.state != UNTESTED:
                continue
            if self._is_rerun and not (
                    id(scenario) in self._rerun_scenarios
                    or feature.key in self._rerun_whole_features):
                # -- NOT PART OF THIS RERUN: behave reports it as skipped,
                # but this says nothing about it. It may have never run
                # (test run was stopped), this must not be forgotten.
                continue
            new_status = statuses.get(scenario.key, default_status)
            if new_status is None:
                continue
            if state_from_status(new_status) != scenario.state:
                self._finish_scenario(scenario, new_status, None, update)
                changed = True
        if feature.is_final and feature.scenarios() and not changed:
            return      # -- REPORTED: By its formatter events.
        feature.status = status
        for node in feature.walk():
            if node.is_container:
                self._set_state(node, node.compute_container_state(), update)
        if not feature.scenarios():
            # -- FEATURE WAS NEVER SHOWN: Its scenarios are not known.
            self._set_state(feature, state_from_status(status), update)
        elif (feature.state != FAILED
              and state_from_status(status) == FAILED):
            # -- CASE: Feature failed without a failed scenario (hook-error).
            self._set_state(feature, FAILED, update)
        update.mark_changed(feature)

    def _on_output(self, event, update):
        text = event.get("text") or u""
        if not text.strip():
            return
        filename = event.get("filename")
        if filename is None:
            self.output.append(text)
            return
        feature = self._ensure_feature(filename, update)
        self._drop_details_of_last_run(feature, update)
        self._add_details(feature, text, update)

    def _on_rerun_started(self, event, update):
        """Reset the features/scenarios that are run again (to: pending)."""
        self.finished = False
        self.execution_point = None
        self._ignored_scenarios.clear()
        self._is_rerun = True
        self._rerun_scenarios.clear()
        self._rerun_whole_features.clear()
        for filename in event.get("changed_features", ()):
            self._forget_feature_outline(filename, update)
            # -- HINT: Its scenarios are not known until it is parsed again.
            self._rerun_whole_features.add(normalize_filename(filename))
        for location in event.get("locations", ()):
            for scenario in self._select_scenarios_at(location):
                self._rerun_features.add(scenario.feature_file)
                self._rerun_scenarios.add(id(scenario))
                # -- HINT: Drop error/output details of the last run.
                # -- HINT: Drop details of the last run, but keep the data.
                scenario.children = [child for child in scenario.children
                                     if child.kind != KIND_DETAIL
                                     or child.keyword == DETAIL_DATA]
                for step in scenario.children:
                    if step.kind != KIND_STEP:
                        continue
                    step.children = [child for child in step.children
                                     if child.keyword == DETAIL_DATA]
                    step.duration = 0.0
                    step.status = None
                    step.definition = None
                    self._set_state(step, PENDING, update)
                scenario.duration = 0.0
                scenario.status = None
                self._set_state(scenario, PENDING, update)
                update.rebuilt.append(scenario)
                self._update_ancestors(scenario, update)

    def _forget_feature_outline(self, filename, update):
        """A feature file was changed: Its scenarios (and their lines) are
        unknown until the feature is parsed and started again.
        """
        feature = self.features.get(normalize_filename(filename))
        if feature is None:
            return
        for key in [key for key in self._scenarios if key[0] == feature.key]:
            del self._scenarios[key]
        feature.remove_children()
        feature.status = None
        feature.duration = 0.0
        self._set_state(feature, PENDING, update)
        update.rebuilt.append(feature)
        update.mark_changed(feature)

    def _select_scenarios_at(self, location):
        """Select the scenarios of a location: feature, rule, outline, ..."""
        for feature in self.features.values():
            for node in feature.walk():
                if node.key == location and node.kind != KIND_STEP:
                    if node.kind == KIND_FEATURE:
                        feature.children = [child for child in feature.children
                                            if child.kind != KIND_DETAIL]
                    return node.scenarios()
        return []

    def _on_testrun_finished(self, event, update):
        # pylint: disable=unused-argument
        self.finished = True
        self.execution_point = None
        for node in self.root.walk():
            if node.kind == KIND_SCENARIO and not node.is_final:
                self._finish_scenario(node, "untested", None, update)
        for node in self.root.walk():
            if node.kind in (KIND_FEATURE, KIND_RULE, KIND_OUTLINE):
                if not node.is_final:
                    state = node.compute_container_state()
                    if state in (PENDING, RUNNING):
                        state = UNTESTED
                    self._set_state(node, state, update)

    # -- SUMMARY:
    def counts(self):
        return self.root.counts()

    def feature_counts(self):
        counts = dict.fromkeys(ALL_STATES, 0)
        for feature in self.features.values():
            counts[feature.state] += 1
        counts["total"] = len(self.features)
        return counts

    def failed_nodes(self):
        """Failed scenarios (and failed features without failed scenario)."""
        nodes = []
        for feature in self.features.values():
            failed_scenarios = [node for node in feature.scenarios()
                                if node.state == FAILED]
            if failed_scenarios:
                nodes.extend(failed_scenarios)
            elif feature.state == FAILED:
                nodes.append(feature)
        return nodes

    # -- INTERNALS:
    @staticmethod
    def _set_state(node, state, update):
        if node.state != state:
            node.set_state(state)
            update.mark_changed(node)

    def _update_ancestors(self, node, update):
        for ancestor in node.ancestors():
            if ancestor.kind == KIND_ROOT:
                break
            self._set_state(ancestor, ancestor.compute_container_state(),
                            update)
            # -- ALWAYS: Its scenario counts have changed.
            update.mark_changed(ancestor)

    def _finish_scenario(self, scenario, status, duration, update):
        scenario.status = status
        if duration is not None:
            scenario.duration = duration
        state = state_from_status(status)
        for step in scenario.children:
            if step.kind == KIND_STEP and not step.is_final:
                # -- STEP WAS NOT RUN: After a failed step, skipped, ...
                step_state = SKIPPED if state == SKIPPED else UNTESTED
                self._set_state(step, step_state, update)
        self._set_state(scenario, state, update)

    def _ensure_feature(self, filename, update):
        filename = normalize_filename(filename)
        feature = self.features.get(filename)
        if feature is None:
            feature = Node(KIND_FEATURE, filename, name=filename)
            self.root.add_child(feature)
            self.features[filename] = feature
            update.added.append(feature)
        return feature

    def _use_feature_outline(self, outline, update):
        feature = self._ensure_feature(outline["key"], update)
        feature.keyword = outline["keyword"]
        feature.name = outline["name"]
        feature.line = outline["line"]
        feature.tags = list(outline.get("tags") or [])
        if not any(child.kind != KIND_DETAIL for child in feature.children):
            details = feature.children
            feature.children = []
            for child_outline in outline["children"]:
                self._add_outline(feature, child_outline, feature.key)
            feature.children.extend(details)
            if feature not in update.added:
                update.rebuilt.append(feature)
        update.mark_changed(feature)
        return feature

    def _add_outline(self, parent, outline, filename):
        node = parent.add_child(Node(outline["kind"], outline["key"],
                                     outline["keyword"], outline["name"],
                                     outline["line"]))
        node.tags = list(outline.get("tags") or [])
        if node.kind == KIND_SCENARIO:
            self._scenarios[(filename, node.key)] = node
        for line in outline.get("data") or []:
            # -- DATA FIRST: Table/text of a step, example row of a scenario.
            node.add_child(Node(KIND_DETAIL, None, keyword=DETAIL_DATA,
                                name=clean_text_line(line)))
        for child_outline in outline["children"]:
            self._add_outline(node, child_outline, filename)
        return node

    @staticmethod
    def _has_details(node, exclude=None):
        return any(child.kind == KIND_DETAIL and child.keyword != exclude
                   for child in node.children)

    @staticmethod
    def _add_details(node, text, update, category=DETAIL_OUTPUT):
        # -- HINT: A carriage-return starts a line again (progress output).
        for line in text.rstrip().replace(u"\r\n", u"\n").split(u"\n"):
            line = clean_text_line(line.split(u"\r")[-1])
            if not line.strip():
                continue
            detail = Node(KIND_DETAIL, None, keyword=category, name=line)
            detail.state = node.state
            node.add_child(detail)
            update.added.append(detail)

    def _select_scenario(self, event, update):
        feature = self._ensure_feature(event["filename"], update)
        return self._scenarios.get((feature.key, event["key"]))

    @staticmethod
    def _select_step(scenario, line):
        for step in scenario.children:
            if step.kind == KIND_STEP and step.key == line \
                    and not step.is_final:
                return step
        return None
