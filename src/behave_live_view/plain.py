# -*- coding: UTF-8 -*-
"""
Plain renderer of the "live" formatter: append-only status lines.

Used without a terminal (CI, pipes, output files) or without "textual".
One status line per finished feature; failed scenarios are expanded
down to their failed step and its error message.
"""

from behave_live_view import model

ICONS = {
    model.PASSED: u"✔",    # -- CHECK MARK
    model.FAILED: u"✘",    # -- BALLOT X
    model.SKIPPED: u"-",
    model.UNTESTED: u"○",  # -- WHITE CIRCLE
    model.PENDING: u"○",
    model.RUNNING: u"○",
}
ANSI_COLORS = {
    model.PASSED: u"\x1b[32m",
    model.FAILED: u"\x1b[31m",
    model.SKIPPED: u"\x1b[36m",
    model.UNTESTED: u"\x1b[90m",
}
ANSI_DIM = u"\x1b[90m"
ANSI_RESET = u"\x1b[0m"


ASCII_ICONS = {
    model.PASSED: u"+", model.FAILED: u"x", model.SKIPPED: u"-",
    model.UNTESTED: u"o", model.PENDING: u"o", model.RUNNING: u"o",
}
ASCII_REPLACEMENTS = {u"\u00b7": u"|", u"\u203a": u">", u"\u2192": u"->"}


def supports_unicode(stream):
    """Check if a stream can encode the symbols that are used here."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True     # -- TEXT STREAM: Like io.StringIO.
    try:
        u"".join(list(ICONS.values()) + list(ASCII_REPLACEMENTS)).encode(
            encoding)
    except (UnicodeError, LookupError):
        return False
    return True


class SafeWriter:
    """Writes text to a stream that may not be able to encode all of it
    (like a console or a pipe with an ASCII or Latin-1 encoding).
    """

    def __init__(self, stream):
        self.stream = stream
        self.unicode = supports_unicode(stream)
        self.icons = ICONS if self.unicode else ASCII_ICONS

    def write(self, text):
        if not self.unicode:
            for symbol, replacement in ASCII_REPLACEMENTS.items():
                text = text.replace(symbol, replacement)
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            text = text.encode(encoding, "replace").decode(encoding)
        self.stream.write(text)

    def flush(self):
        self.stream.flush()


def format_duration(seconds):
    seconds = seconds or 0.0
    if seconds < 60:
        return u"%.2fs" % seconds
    minutes, seconds = divmod(int(seconds), 60)
    return u"%dm %02ds" % (minutes, seconds)


def write_failures(status_tree, stream, colored=False):
    """Write a compact summary of the failures: what failed, where and why.

    :return: Number of failures that were written.
    """
    failed_nodes = status_tree.failed_nodes()
    if not failed_nodes:
        return 0

    stream = SafeWriter(stream)
    icon = stream.icons[model.FAILED]
    red, dim, reset = u"", u"", u""
    if colored:
        red, dim, reset = ANSI_COLORS[model.FAILED], ANSI_DIM, ANSI_RESET
    stream.write(u"Failures:\n")
    for node in failed_nodes:
        feature = node if node.kind == model.KIND_FEATURE else None
        for ancestor in node.ancestors():
            if ancestor.kind == model.KIND_FEATURE:
                feature = ancestor
        title = u"%s: %s" % (node.keyword, node.name)
        if feature is not None and feature is not node:
            title = u"%s \u203a %s" % (feature.name, title)
        if node.status and node.status != "failed":
            title += u"  [%s]" % node.status    # -- LIKE: hook_error
        stream.write(u"%s%s%s %s  %s%s%s\n" % (
            red, icon, reset, title, dim,
            model.format_location(node.source_location), reset))
        failed_steps = [step for step in node.children
                        if step.kind == model.KIND_STEP
                        and step.state == model.FAILED]
        if not failed_steps:
            # -- NO FAILED STEP: Failed by a hook or a cleanup, the reason
            # is in the output of the hooks (details of this node).
            for detail in node.children:
                if detail.kind == model.KIND_DETAIL \
                        and detail.keyword != model.DETAIL_DATA:
                    stream.write(u"    %s%s%s\n" % (red, detail.name, reset))
        for step in failed_steps:
            location = model.format_location(step.source_location)
            if step.definition:
                location += u" \u2192 %s" % step.definition
            stream.write(u"    %s%s%s %s %s  %s%s%s\n" % (
                red, icon, reset, step.keyword, step.name,
                dim, location, reset))
            for detail in step.children:
                if detail.keyword == model.DETAIL_ERROR:
                    stream.write(u"      %s%s%s\n" % (red, detail.name, reset))
    stream.write(u"\n")
    stream.flush()
    return len(failed_nodes)


class PlainRenderer:
    """Writes status lines for finished features to a stream."""

    def __init__(self, stream, config=None):
        self.stream = SafeWriter(stream)
        self.icons = self.stream.icons
        has_colored_mode = getattr(config, "has_colored_mode", None)
        self.colored = bool(has_colored_mode and has_colored_mode(stream))
        self.status_tree = model.StatusTree()

    def process_event(self, event):
        self.status_tree.apply(event)
        event_type = event.get("type")
        if event_type == "feature_finished":
            filename = model.normalize_filename(event["filename"])
            self.write_feature(self.status_tree.features[filename])
        elif event_type == "output" and (event.get("text") or u"").strip():
            if event.get("filename") is None:
                self.stream.write(event["text"].rstrip() + u"\n")
                self.stream.flush()
        elif event_type == "testrun_finished":
            self.write_untested_features()

    # -- OUTPUT:
    def write_feature(self, feature):
        counts = feature.counts()
        parts = [u"%d/%d" % (counts[model.PASSED], counts["total"])]
        if counts[model.FAILED]:
            parts.append(u"%d failed" % counts[model.FAILED])
        if counts[model.SKIPPED]:
            parts.append(u"%d skipped" % counts[model.SKIPPED])
        if counts[model.UNTESTED]:
            parts.append(u"%d untested" % counts[model.UNTESTED])
        text = u"%s: %s  %s  %s" % (feature.keyword or u"Feature",
                                    feature.name, u" · ".join(parts),
                                    format_duration(feature.duration))
        self.write_line(feature.state, text, 0, dimmed=feature.key)
        if feature.state == model.FAILED:
            for child in feature.children:
                self.write_failed_node(child, 1)
        self.stream.flush()

    def write_failed_node(self, node, level):
        if node.kind == model.KIND_DETAIL:
            self.write_text(node.name, level, node.state)
            return
        if node.state != model.FAILED:
            return
        text = u"%s %s" % (node.keyword, node.name)
        if node.kind != model.KIND_STEP:
            text = u"%s: %s" % (node.keyword, node.name)
        self.write_line(node.state, text, level)
        for child in node.children:
            self.write_failed_node(child, level + 1)

    def write_untested_features(self):
        for feature in self.status_tree.features.values():
            if feature.state == model.UNTESTED and feature.status is None:
                self.write_line(feature.state, feature.name, 0)
        self.stream.flush()

    def write_line(self, state, text, level, dimmed=None):
        icon = self.icons.get(state, u" ")
        if self.colored:
            icon = u"%s%s%s" % (ANSI_COLORS.get(state, u""), icon, ANSI_RESET)
            if dimmed:
                dimmed = u"%s%s%s" % (ANSI_DIM, dimmed, ANSI_RESET)
        line = u"%s%s %s" % (u"  " * level, icon, text)
        if dimmed:
            line += u"  " + dimmed
        self.stream.write(line + u"\n")

    def write_text(self, text, level, state=None):
        if self.colored and state == model.FAILED:
            text = u"%s%s%s" % (ANSI_COLORS[model.FAILED], text, ANSI_RESET)
        self.stream.write(u"%s%s\n" % (u"  " * level, text))
