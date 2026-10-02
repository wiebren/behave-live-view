# -*- coding: UTF-8 -*-
"""
"live" formatter: Maps the formatter calls of a test run to events
(see: :mod:`behave_live_view.model`) and sends them to a renderer.

* :class:`LiveFormatter`: Shows the events -- in the interactive view of
  the :class:`~behave_live_view.host.LiveHost` (if one is active; in this
  process or in the parent process of a parallel test run)
  or as plain status lines.
"""

from behave.formatter.base import Formatter
from behave_live_view import model
from behave_live_view.plain import PlainRenderer
from behave_live_view.remote import EventSender
from behave.model import Rule, ScenarioOutline


# -----------------------------------------------------------------------------
# OUTLINE BUILDING: behave model -> picklable outline dicts
# -----------------------------------------------------------------------------
def scenario_key(scenario):
    return str(scenario.location)


def make_output_text(text, error_text=None):
    """Compact a captured-output report: short section headers, no rulers.

    "CAPTURED STDOUT: step" -> "stdout:"
    "CAPTURED STDOUT: before_scenario" -> "before_scenario stdout:"
    """
    if error_text:
        # -- HINT: Error message is stored as captured output, too.
        text = text.replace(error_text.rstrip(), u"")

    lines = []
    last_is_header = False
    # -- HINT: Keep carriage-returns (progress output), see: model.
    for line in text.replace(u"\r\n", u"\n").split(u"\n"):
        if not line.strip() or line.strip() == u"----":
            continue    # -- RULER: Of the captured-output report.
        is_header = line.startswith(u"CAPTURED ")
        if is_header:
            if last_is_header:
                lines.pop()     # -- EMPTY SECTION: Header without content.
            section, _, name = line.partition(u":")
            section = section.replace(u"CAPTURED ", u"").lower()
            name = name.strip()
            if name and name not in (u"step", u"scenario"):
                section = u"%s %s" % (name, section)
            line = section + u":"
        lines.append(line)
        last_is_header = is_header
    if last_is_header:
        lines.pop()
    return u"\n".join(lines) or None


def make_step_output(step, error_text=None):
    """Select the captured output of a step (without its error message)."""
    captured = getattr(step, "captured", None)
    if not captured or not captured.has_output():
        return None
    return make_output_text(captured.make_report(), error_text)


def make_hooks_output(model_element):
    """Select the captured output of the hooks of a scenario/rule/feature.

    HINT: Output of the steps is shown with each step (excluded here).
    """
    captured = getattr(model_element, "captured", None)
    parts = [part for part in getattr(captured, "captures", ())
             if getattr(part, "name", None) not in ("step", "scenario")
             and part.has_output()]
    text = u"\n".join(part.make_report() for part in parts)
    return make_output_text(text)


def make_table_lines(table):
    """Format a table as text lines with aligned columns."""
    rows = [list(table.headings)] + [list(row.cells) for row in table.rows]
    widths = [max(len(u"%s" % row[index]) for row in rows)
              for index in range(len(table.headings))]
    return [u"| %s |" % u" | ".join((u"%s" % cell).ljust(width)
                                     for cell, width in zip(row, widths))
            for row in rows]


def make_step_data(step):
    """Describe the data of a step as text lines: Its table or text."""
    lines = []
    if getattr(step, "table", None):
        lines.extend(make_table_lines(step.table))
    if getattr(step, "text", None):
        lines.append(u'"""')
        lines.extend(step.text.splitlines())
        lines.append(u'"""')
    return lines


def make_scenario_data(scenario):
    """Describe the example row of a scenario from a scenario outline."""
    row = getattr(scenario, "_row", None)
    if row is None:
        return []
    values = [u"%s=%s" % (heading, cell)
              for heading, cell in zip(row.headings, row.cells)]
    return [u"Example: %s" % u", ".join(values)]


def make_step_outline(step):
    return model.make_outline(model.KIND_STEP, step.location.line,
                              step.keyword, step.name, step.location.line,
                              data=make_step_data(step))


def make_scenario_outline(scenario):
    steps = [make_step_outline(step) for step in scenario.all_steps]
    return model.make_outline(model.KIND_SCENARIO, scenario_key(scenario),
                              scenario.keyword, scenario.name,
                              scenario.location.line, steps,
                              tags=scenario.tags,
                              data=make_scenario_data(scenario))


def make_run_item_outline(run_item):
    if isinstance(run_item, Rule):
        children = [make_run_item_outline(item) for item in run_item.run_items]
        return model.make_outline(model.KIND_RULE, str(run_item.location),
                                  run_item.keyword, run_item.name,
                                  run_item.location.line, children,
                                  tags=run_item.tags)
    if isinstance(run_item, ScenarioOutline):
        children = [make_scenario_outline(scenario)
                    for scenario in run_item.scenarios]
        return model.make_outline(model.KIND_OUTLINE, str(run_item.location),
                                  run_item.keyword, run_item.name,
                                  run_item.location.line, children,
                                  tags=run_item.tags)
    return make_scenario_outline(run_item)


def make_feature_outline(feature):
    """Describe a feature (and what it contains) as picklable outline."""
    children = [make_run_item_outline(item) for item in feature.run_items]
    return model.make_outline(model.KIND_FEATURE,
                              model.normalize_filename(feature.filename),
                              feature.keyword, feature.name,
                              feature.location.line, children,
                              tags=feature.tags)


# -----------------------------------------------------------------------------
# FORMATTERS:
# -----------------------------------------------------------------------------
class LiveEventFormatter(Formatter):
    """Base class: Maps the formatter interface to events (abstract)."""
    # pylint: disable=abstract-method
    description = None

    def __init__(self, stream_opener, config):
        super(LiveEventFormatter, self).__init__(stream_opener, config)
        self.current_feature = None
        self.current_scenario = None
        self._steps = []        # -- Steps of the current scenario (to run).

    def emit(self, event):
        raise NotImplementedError()

    # -- INTERFACE FOR: Formatter
    def feature(self, feature):
        self._finish_current_feature()
        self.current_feature = feature
        self.emit({"type": "feature_started",
                   "feature": make_feature_outline(feature)})

    def rule(self, rule):
        self._finish_current_scenario()

    def scenario(self, scenario):
        self._finish_current_scenario()
        self.current_scenario = scenario
        self._steps = list(scenario.all_steps)
        for step in self._steps:
            # -- ENSURE: Captured output of a passed step is kept, too
            # (a step can be opened to look at its output).
            step.capture_sink.store_on_success = True
        self.emit({"type": "scenario_started",
                   "filename": self._current_filename(),
                   "key": scenario_key(scenario)})

    def match(self, match):
        # -- HINT: Called before a step is run (steps run in their order).
        if self.current_scenario is None or not self._steps:
            return
        # -- HINT: An undefined step has no step definition (NoMatch).
        definition = getattr(match, "location", None)
        self.emit({"type": "step_started",
                   "filename": self._current_filename(),
                   "key": scenario_key(self.current_scenario),
                   "line": self._steps[0].location.line,
                   "definition": str(definition) if definition else None})

    def result(self, step):
        if self.current_scenario is None:
            return
        if step in self._steps:
            self._steps.remove(step)
        error_text = None
        if model.state_from_status(step.status.name) == model.FAILED:
            error_text = step.error_message or step.status.name
        self.emit({"type": "step_finished",
                   "filename": self._current_filename(),
                   "key": scenario_key(self.current_scenario),
                   "line": step.location.line,
                   "status": step.status.name,
                   "duration": step.duration,
                   "error": error_text,
                   "output": make_step_output(step, error_text)})

    def eof(self):
        self._finish_current_feature()

    def close(self):
        self._finish_current_feature()
        self.close_stream()

    # -- INTERNALS:
    def _current_filename(self):
        return model.normalize_filename(self.current_feature.filename)

    def _finish_current_scenario(self):
        scenario = self.current_scenario
        if scenario is None:
            return
        self.current_scenario = None
        self.emit({"type": "scenario_finished",
                   "filename": self._current_filename(),
                   "key": scenario_key(scenario),
                   "status": scenario.status.name,
                   "duration": scenario.duration,
                   "output": make_hooks_output(scenario)})

    def _finish_current_feature(self):
        feature = self.current_feature
        if feature is None:
            return
        self._finish_current_scenario()
        # -- HINT: Scenarios that were skipped are not announced (in general).
        statuses = dict((scenario_key(scenario), scenario.status.name)
                        for scenario in feature.walk_scenarios())
        self.emit({"type": "feature_finished",
                   "filename": self._current_filename(),
                   "status": feature.status.name,
                   "duration": feature.duration,
                   "statuses": statuses,
                   "output": make_hooks_output(feature)})
        self.current_feature = None


class LiveFormatter(LiveEventFormatter):
    """Compact status lines that can be expanded/collapsed (interactive).

    Falls back to plain status lines without a terminal (or "textual").
    """
    name = "live"
    description = "Compact, navigable status lines (interactive live view)."

    def __init__(self, stream_opener, config):
        super(LiveFormatter, self).__init__(stream_opener, config)
        self.host = None
        self.sender = None
        self.renderer = None
        if self.stdout_mode:
            from behave_live_view.host import LiveHost
            self.host = LiveHost.current
            if self.host is None:
                # -- WORKER PROCESS: Of a parallel test run whose parent
                # process shows the interactive view.
                self.sender = EventSender.connect_for(config)
        if self.host is None and self.sender is None:
            self.renderer = PlainRenderer(self.open(), config)

    def emit(self, event):
        if self.host is not None:
            self.host.post_event(event)
        elif self.sender is not None:
            self.sender.send(event)
        else:
            self.renderer.process_event(event)

    def close(self):
        self._finish_current_feature()
        if self.sender is not None:
            # -- HINT: Only the work of this process has ended (like: one
            # feature). The host knows when the test run has ended.
            self.sender.close()
        else:
            self.emit({"type": "testrun_finished"})
        self.close_stream()
