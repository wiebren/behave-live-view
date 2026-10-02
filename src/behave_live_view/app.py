# -*- coding: UTF-8 -*-
"""
Interactive status view of a test run (used by: "live" formatter).

This module contains the terminal UI (based on: :mod:`textual`).
It shows a :class:`~behave_live_view.model.StatusTree` as a compact,
collapsible tree of status lines::

    behave  ⠙ running  12/40 scenarios · 1 failed · 00:12
    ✔ Feature: Alice  3/3  0.42s  features/alice.feature
    ⠙ Feature: Bob  1/4  features/bob.feature

The caller feeds EVENTS (see: :mod:`behave_live_view.model`) into
:meth:`LiveApp.handle_event()`. Because the test runner normally runs in
another thread, the caller must use :meth:`textual.app.App.call_from_thread`
for that (and for :meth:`LiveApp.testrun_done()`).

.. note::

    This module imports :mod:`textual` at module level.
    The caller should import this module lazily (and handle ImportError).
"""

from __future__ import absolute_import
import time

from rich.cells import cell_len
from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Static, Tree

from behave_live_view.model import (
    CONTAINER_KINDS, DETAIL_DATA, DETAIL_ERROR, FAILED, KIND_DETAIL,
    KIND_FEATURE, KIND_SCENARIO, KIND_STEP, PASSED, PENDING, RUNNING,
    SKIPPED, UNTESTED, VIEW_MODES, StatusTree, format_location,
    make_text_report, select_visible_nodes
)


# -----------------------------------------------------------------------------
# CONSTANTS
# -----------------------------------------------------------------------------
SPINNER_FRAMES = u"⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_INTERVAL = 0.08     # -- Seconds: Braille spinner animation step.
FLUSH_INTERVAL = 0.05       # -- Seconds: Coalesce label updates (batch).
HEADER_INTERVAL = 0.25      # -- Seconds: Header clock (while: test run).

STATE_ICON = {
    PENDING:  (u"○", "dim"),
    PASSED:   (u"✔", "green"),
    FAILED:   (u"✘", "red"),
    SKIPPED:  (u"⊘", "dim"),
    UNTESTED: (u"○", "dim"),
}
ACCENT_STYLE = "cyan"
COUNT_STYLE = "dim"
LOCATION_ARROW = u" → "     # -- Separator: source location, step definition
LOCATION_STYLE = "none"     # -- Source location: A little bit brighter.
DATA_STYLE = "cyan"         # -- Step table/text, example row (detail lines).
ELLIPSIS = u"…"
FILTER_DELAY = 0.15         # -- Seconds: Filter while typing (debounce).
NO_MATCH_TEXT = u"No lines match the filter"

# -- RERUN: How many changed file names are shown (at most).
MAX_DIALOG_NAMES = 8
MAX_NOTIFY_NAMES = 5


# -----------------------------------------------------------------------------
# UTILITY FUNCTIONS
# -----------------------------------------------------------------------------
def format_duration(seconds):
    """Format a duration, like: "0.12s" or "1m 02s"."""
    seconds = seconds or 0.0
    if seconds >= 60.0:
        minutes, seconds = divmod(int(round(seconds)), 60)
        return u"%dm %02ds" % (minutes, seconds)
    return u"%.2fs" % seconds


def format_elapsed(seconds):
    """Format an elapsed time (clock-like), like: "00:12" or "1:02:03"."""
    minutes, seconds = divmod(int(seconds or 0), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return u"%d:%02d:%02d" % (hours, minutes, seconds)
    return u"%02d:%02d" % (minutes, seconds)


def format_names(names, max_count):
    """Shorten a list of file names, like: [a, b, "… and 3 more"]."""
    names = list(names)
    if len(names) <= max_count:
        return names
    return names[:max_count] + [u"… and %d more" % (len(names) - max_count)]


def join_names(names, max_count=MAX_NOTIFY_NAMES):
    """Shorten and join file names (for a notification)."""
    return u", ".join(format_names(names, max_count))


def cut_from_left(text, width):
    """Keep the end of a text: At most width cells wide (not: characters).

    HINT: East-Asian characters need two cells in a terminal.
    """
    if width <= 0:
        return u""
    if cell_len(text) <= width:
        return text
    kept = []
    size = 0
    for char in reversed(text):
        size += cell_len(char)
        if size > width:
            break
        kept.append(char)
    return u"".join(reversed(kept))


def format_locations(node, width=None):
    """Locations of a node (for the location bar), as plain text.

    Example: "features/alice.feature:12 → features/steps/alice_steps.py:48"

    :param node:  Status-tree node (or None).
    :param width:  Maximum width in CELLS; longer text is truncated on the
        LEFT (its end, the file:line of the step definition, is important).
    :return: Location(s) of this node (as text; may be empty).
    """
    if node is None:
        return u""
    locations = [location for location in (node.source_location,
                                           node.definition_location)
                 if location is not None]
    text = LOCATION_ARROW.join(format_location(loc) for loc in locations)
    if width and cell_len(text) > width:
        # -- TRUNCATE ON THE LEFT: Keep the end of the text.
        text = ELLIPSIS + cut_from_left(text, width - cell_len(ELLIPSIS))
    return text


def make_location_text(node, width=None):
    """Build the (rich) text of the location bar (see: format_locations())."""
    text = Text(no_wrap=True, overflow="ellipsis", style="dim")
    source, arrow, definition = format_locations(node, width).partition(
        LOCATION_ARROW)
    text.append(source, style=LOCATION_STYLE)
    if arrow:
        text.append(arrow)
        text.append(definition)
    return text


def select_icon(node, spinner_frame):
    """Select the status icon (and its style) for a status-tree node."""
    if node.state == RUNNING:
        return spinner_frame, ACCENT_STYLE
    return STATE_ICON.get(node.state, STATE_ICON[PENDING])


def make_label(node, spinner_frame=SPINNER_FRAMES[0]):
    """Build the (rich) text of one status line.

    :param node:  Status-tree node to render (as :class:`Node`).
    :param spinner_frame:  Current spinner character (for running nodes).
    :return: Label of this status line (as :class:`rich.text.Text`).
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    if node.kind == KIND_DETAIL:
        # -- DETAIL LINE: Error message or captured output (without icon).
        # HINT: Its category is stored in the keyword of the detail node.
        is_error = (node.keyword == DETAIL_ERROR or
                    (not node.keyword and node.state == FAILED))
        if node.keyword == DETAIL_DATA:
            # -- DATA: Step table/text, example row (neutral, not an error).
            style = "dim %s" % DATA_STYLE
        else:
            style = "red" if is_error else "dim"
        text.append(node.name, style=style)
        return text

    icon, icon_style = select_icon(node, spinner_frame)
    text.append(icon, style=icon_style)
    text.append(u" ")
    name_style = "bold" if node.state == FAILED else None
    if node.kind == KIND_FEATURE and node.name == node.key:
        # -- FEATURE (without its outline): Only its filename is known.
        text.append(node.key, style="dim")
        return text

    if node.kind == KIND_STEP:
        text.append(u"%s " % node.keyword if node.keyword else u"")
        text.append(node.name, style=name_style)
    else:
        text.append(u"%s: " % node.keyword if node.keyword else u"")
        text.append(node.name, style=name_style)

    if node.kind in CONTAINER_KINDS:
        counts = node.counts()
        text.append(u"  %d/%d" % (counts["done"], counts["total"]),
                    style=COUNT_STYLE)
        if counts[FAILED]:
            text.append(u" · %d failed" % counts[FAILED], style="red")
    if node.is_final and node.duration:
        text.append(u"  %s" % format_duration(node.duration), style="dim")
    if node.kind == KIND_FEATURE:
        text.append(u"  %s" % node.key, style="dim")
    return text


# -----------------------------------------------------------------------------
# MODAL DIALOG: Reload changed source files?
# -----------------------------------------------------------------------------
class ReloadDialog(ModalScreen):
    """Asks if changed Python modules should be reloaded before a rerun.

    ANSWERS (dismiss-value): "reload", "old" or "cancel".
    """
    DEFAULT_CSS = """
    ReloadDialog {
        align: center middle;
        background: transparent;
    }
    ReloadDialog > #reload-dialog {
        width: auto;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round ansi_cyan;
        background: $surface;
    }
    ReloadDialog Static {
        width: auto;
        height: auto;
        background: transparent;
    }
    ReloadDialog > #reload-dialog > #reload-choices {
        margin-top: 1;
    }
    """
    BINDINGS = [
        Binding("y", "answer('reload')", "reload and run"),
        Binding("enter", "answer('reload')", show=False),
        Binding("n", "answer('old')", "run with the old code"),
        Binding("escape", "answer('cancel')", "cancel"),
        Binding("q", "answer('cancel')", show=False),
    ]
    TITLE_TEXT = u"Source files changed since they were loaded"
    CHOICES_TEXT = u"[y] reload and run   [n] run with the old code   " \
                   u"[esc] cancel"

    def __init__(self, filenames, **kwargs):
        super(ReloadDialog, self).__init__(**kwargs)
        self.filenames = format_names(filenames, MAX_DIALOG_NAMES)

    def compose(self):
        files = Text(u"\n".join(self.filenames), style="dim")
        yield Vertical(
            Static(Text(self.TITLE_TEXT, style="bold"), id="reload-title"),
            Static(files, id="reload-files"),
            Static(Text(self.CHOICES_TEXT, style=ACCENT_STYLE),
                   id="reload-choices"),
            id="reload-dialog")

    def action_answer(self, answer):
        """Answer this dialog (and: close it)."""
        self.dismiss(answer)


# -----------------------------------------------------------------------------
# MODAL DIALOG: Which keys can I use?
# -----------------------------------------------------------------------------
class HelpScreen(ModalScreen):
    """Shows all keys of this app (modal overlay)."""
    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
        background: transparent;
    }
    HelpScreen > #help-dialog {
        width: auto;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round ansi_cyan;
        background: $surface;
    }
    HelpScreen Static {
        width: auto;
        height: auto;
        background: transparent;
    }
    """
    BINDINGS = [
        Binding("question_mark", "close", "close", show=False),
        Binding("escape", "close", "close", show=False),
        Binding("q", "close", "close", show=False),
        Binding("enter", "close", "close", show=False),
    ]
    TITLE_TEXT = u"behave live view -- keys"
    KEY_GROUPS = [
        (u"Navigation", [
            (u"↑/↓, k/j", u"move up/down"),
            (u"→/←, l/h", u"expand / collapse (or: go to the parent)"),
            (u"enter, space", u"expand or collapse this line"),
            (u"e", u"expand all failures"),
            (u"c", u"collapse all"),
            (u"f", u"follow mode: cursor follows the running step"),
        ]),
        (u"Failures", [
            (u"n / N", u"go to the next / previous failure"),
        ]),
        (u"Filter and view", [
            (u"/", u"filter lines: text or @tag (enter: keep, esc: clear)"),
            (u"escape", u"clear the filter"),
            (u"v", u"view mode: all -> failed -> not passed"),
        ]),
        (u"Actions", [
            (u"o", u"open the feature file line in an editor"),
            (u"d", u"open the step definition in an editor"),
            (u"y", u"copy this line (as report) to the clipboard"),
            (u"Y", u"copy the location of this line"),
        ]),
        (u"Test run", [
            (u"r", u"run the selected feature/scenario again"),
            (u"R", u"run all (visible) failures again"),
            (u"q, ctrl+c", u"stop the test run (again: quit)"),
            (u"ctrl+q", u"quit now"),
            (u"?", u"show/hide this help"),
        ]),
    ]

    def compose(self):
        text = Text(no_wrap=True)
        for index, (title, keys) in enumerate(self.KEY_GROUPS):
            if index:
                text.append(u"\n")
            text.append(u"\n%s\n" % title, style="bold")
            for key, description in keys:
                text.append(u"  %-14s" % key, style=ACCENT_STYLE)
                text.append(u"%s\n" % description, style="dim")
        yield Vertical(
            Static(Text(self.TITLE_TEXT, style="bold"), id="help-title"),
            Static(text, id="help-keys"),
            Static(Text(u"[?] [esc] [q] close", style=ACCENT_STYLE),
                   id="help-close"),
            id="help-dialog")

    def action_close(self):
        self.dismiss(None)


# -----------------------------------------------------------------------------
# FILTER INPUT: One line to filter the status lines
# -----------------------------------------------------------------------------
class FilterInput(Input):
    """Input line for the filter text (see: LiveApp.action_open_filter())."""
    DEFAULT_CSS = """
    FilterInput {
        height: 1;
        border: none;
        padding: 0 1;
        background: transparent;
    }
    FilterInput:focus {
        border: none;
    }
    """
    BINDINGS = [
        Binding("escape", "cancel_filter", "cancel", show=False),
    ]

    def action_cancel_filter(self):
        # pylint: disable=protected-access
        self.app._close_filter(cancel=True)


# -----------------------------------------------------------------------------
# TEXTUAL APP
# -----------------------------------------------------------------------------
class LiveApp(App):
    """Shows the status tree of a test run while the test run is performed."""
    # pylint: disable=too-many-instance-attributes

    CSS = """
    Screen {
        background: transparent;
        layers: base;
    }
    #live-header {
        height: 1;
        background: transparent;
        padding: 0 1;
    }
    #live-tree {
        background: transparent;
        padding: 0 1;
        overflow-x: hidden;
        scrollbar-size-vertical: 1;
    }
    #live-location {
        height: 1;
        background: transparent;
        padding: 0 1;
    }
    #live-filter {
        display: none;
    }
    #live-filter.-active {
        display: block;
    }
    #live-location.-hidden {
        display: none;
    }
    Footer {
        background: transparent;
    }
    """
    BINDINGS = [
        # -- HINT: The footer shows a few keys only (see: "?" for all keys).
        Binding("q", "stop_or_quit", "quit", priority=True),
        Binding("ctrl+c", "stop_or_quit_now", "quit", show=False,
                priority=True),
        Binding("ctrl+q", "force_quit", "quit", show=False, priority=True),
        Binding("slash", "open_filter", "filter"),
        Binding("escape", "clear_filter", "clear filter", show=False),
        Binding("v", "next_view_mode", "view"),
        Binding("n", "next_failure", "next fail"),
        Binding("N", "previous_failure", "prev failure", show=False),
        Binding("f", "toggle_follow", "follow"),
        Binding("r", "rerun_selected", "rerun"),
        Binding("question_mark", "help", "help"),
        Binding("e", "expand_failures", "expand fails", show=False),
        Binding("c", "collapse_all", "collapse all", show=False),
        Binding("o", "open_source", "open", show=False),
        Binding("d", "open_definition", "definition", show=False),
        Binding("y", "copy_report", "copy", show=False),
        Binding("Y", "copy_location", "copy location", show=False),
        Binding("R", "rerun_failed", "rerun failed", show=False),
        # -- HINT: priority=True, because the Tree widget handles these keys
        # itself (left/right: horizontal scrolling of a ScrollView).
        # We need them here to stop the FOLLOW MODE (user navigation).
        Binding("right", "expand_node", "expand", priority=True),
        Binding("left", "collapse_node", "collapse", priority=True),
        Binding("l", "expand_node", show=False, priority=True),
        Binding("h", "collapse_node", show=False, priority=True),
        Binding("down", "cursor_down", show=False, priority=True),
        Binding("up", "cursor_up", show=False, priority=True),
        Binding("j", "cursor_down", show=False, priority=True),
        Binding("k", "cursor_up", show=False, priority=True),
        Binding("enter", "toggle_node", show=False, priority=True),
        Binding("space", "toggle_node", show=False, priority=True),
    ]
    TITLE = u"behave"
    ENABLE_COMMAND_PALETTE = False      # -- CLI LOOK: No "^p palette" hint.

    def __init__(self, status_tree=None, on_interrupt=None, on_rerun=None,
                 on_check_changes=None, on_open=None, on_copy=None,
                 parallel=False, **kwargs):
        kwargs.setdefault("ansi_color", True)
        super(LiveApp, self).__init__(**kwargs)
        self.status_tree = status_tree or StatusTree()
        self.on_interrupt = on_interrupt
        self.on_rerun = on_rerun
        self.on_check_changes = on_check_changes
        self.on_open = on_open
        self.on_copy = on_copy
        self.dialog_is_open = False     # -- MODAL: A dialog is shown.
        #: FILTER: Text (words, "@tag") and view mode (see: VIEW_MODES).
        self.filter_text = u""
        self.view_mode = list(VIEW_MODES)[0]
        self.captured_output = []   # -- List of (text, is_stderr) tuples.
        self.start_time = time.time()
        self.end_time = None
        self.testrun_failed = None
        self.is_done = False
        self.interrupting = False
        #: FOLLOW MODE: Cursor follows the execution point of the test run.
        self.follow = True
        #: PARALLEL TEST RUN: Many tests run at the same time (--jobs N).
        self.parallel = parallel
        # -- TREE MAPPING: id(model_node) -> TreeNode (materialized nodes).
        self._tree_nodes = {}
        self._materialized = set()  # -- id(model_node): Children exist.
        # -- PERSISTENT: Also for nodes that the filter hides now.
        self._expanded_ids = set()  # -- id(model_node): Expanded (ever).
        self._materialized_ever = set()   # -- id(model_node): Materialized.
        self._wanted_cursor_id = None     # -- Cursor line (if it comes back).
        self._app_cursor_node = None      # -- TreeNode: Cursor moved by app.
        self._blurred_by_app = False      # -- Terminal lost the focus.
        self._auto_expanded = set()  # -- id(model_node): Expanded once.
        self._running_nodes = {}    # -- id(model_node) -> model_node.
        # -- FOLLOW MODE: Which nodes were expanded by whom.
        self._followed = set()      # -- id(model_node): Expanded by follow.
        self._user_expanded = set()  # -- id(model_node): Expanded by user.
        self._app_toggles = {}      # -- (id(tree_node), what) -> count.
        self._last_point = None     # -- Last execution point (of model).
        # -- FILTER: Visible nodes as set of id(node) -- None: Everything.
        self._visible = None
        self._filter_timer = None       # -- Filter while typing (debounce).
        # -- PENDING WORK (coalesced, flushed by a timer):
        self._pending_rebuilt = []
        self._pending_added = []
        self._pending_changed = []
        self._pending_changed_ids = set()
        self._flush_timer = None
        self._spinner_timer = None
        self._header_timer = None
        self._spinner_index = 0
        self._failure_index = -1

    @property
    def is_exiting(self):
        """True, if this app is closing (or closed) its view."""
        # pylint: disable=protected-access
        return bool(self._exit) or not self.is_running

    # -- WIDGETS:
    def compose(self):
        yield Static(u"", id="live-header")
        yield Tree(u"root", id="live-tree")
        yield Static(u"", id="live-location")
        yield FilterInput(placeholder=u"filter: text or @tag",
                          id="live-filter")
        yield Footer()

    @property
    def tree_view(self):
        """The Tree widget that shows the status tree."""
        return self.query_one("#live-tree", Tree)

    def on_mount(self):
        """Setup the view (a subclass may add its own on_mount handler)."""
        if self._flush_timer is not None:
            return      # -- GUARD: Already mounted (called twice).
        # -- HINT: Capture print() output (from any thread) while we run;
        # the caller replays it on the real terminal after the app exits.
        self.begin_capture_print(self)
        self.theme = "ansi-dark"
        tree = self.tree_view
        tree.show_root = False
        tree.auto_expand = False    # -- HINT: "enter" toggles (see below).
        tree.guide_depth = 2
        tree.root.data = self.status_tree.root
        self._tree_nodes[id(self.status_tree.root)] = tree.root
        self._app_expand(tree.root)
        self._materialize(self.status_tree.root)
        tree.focus()
        self._flush_timer = self.set_interval(
            FLUSH_INTERVAL, self._flush_updates, pause=True)
        self._spinner_timer = self.set_interval(
            SPINNER_INTERVAL, self._animate_spinner, pause=True)
        self._header_timer = self.set_interval(
            HEADER_INTERVAL, self._tick_header, pause=True)
        self._flush_updates()

    def watch_app_focus(self, focus):
        """The terminal window has lost/got the focus (see: on_print)."""
        # HINT: Textual blurs the focused widget when the terminal loses
        # the focus. That (delayed) blur must not close the filter input.
        if not focus:
            self._blurred_by_app = True

    def on_key(self, event):
        """Keys belong to the filter input line while it is shown.

        SAFETY NET: If something has moved the focus away while the filter
        input is shown, its keys must not act as commands of this app.
        """
        if self.filter_is_shown and not self.filter_is_open:
            filter_input = self.filter_input
            if filter_input is not None:
                filter_input.focus()
                event.stop()

    def on_print(self, event):
        """Store text that was printed while this app is running.

        :param event:  Printed text (as: :class:`textual.events.Print`).
        """
        # -- HINT: events.Print is posted from the app thread, but print()
        # may come from any thread (post_message() is thread-safe).
        self.captured_output.append((event.text, event.stderr))

    # -------------------------------------------------------------------------
    # PUBLIC API (for the caller; must be used in the app thread)
    # -------------------------------------------------------------------------
    def handle_event(self, event):
        """Apply one test-run event to the status tree and refresh the view.

        .. note:: Must be called in the app thread (use: call_from_thread).
        """
        if self.is_exiting:
            return      # -- TOO LATE: This app is closing its view.
        try:
            update = self.status_tree.apply(event)
        except Exception as e:  # pylint: disable=broad-except
            # -- ROBUSTNESS: Never fail the test run due to an odd event.
            self.log("live: BAD EVENT %r (%s: %s)" % (event, e.__class__, e))
            return
        if event.get("type") == "rerun_started":
            self._testrun_restarted()
        if not update:
            return
        self._pending_rebuilt.extend(update.rebuilt)
        self._pending_added.extend(update.added)
        for node in update.changed:
            if id(node) not in self._pending_changed_ids:
                self._pending_changed_ids.add(id(node))
                self._pending_changed.append(node)
        if self._flush_timer is not None:
            self._flush_timer.resume()

    def testrun_done(self, failed):
        """Inform this app that the test run has ended.

        :param failed:  True/False (or None, if the test run has crashed).
        """
        if self.is_exiting:
            return      # -- TOO LATE: This app is closing its view.
        self.is_done = True
        self.testrun_failed = failed
        self.end_time = time.time()
        self.interrupting = False
        self._last_point = None
        self._flush_updates()
        if self.parallel:
            self._auto_expand_failures()    # -- KEPT STILL: Until now.
        self._ensure_cursor()
        self.refresh_bindings()     # -- SHOW: The rerun keys (footer).

    @property
    def keeps_tree_still(self):
        """Check if the tree must not be expanded automatically now: while
        a parallel test run runs, following its execution points and
        expanding its failures would let the lines jump all the time.
        The failures are expanded when the test run has ended.
        """
        return self.parallel and not self.is_done

    def _has_failed_cursor(self):
        """Check if the cursor is on a failed line (or inside of one)."""
        tree_node = self.tree_view.cursor_node
        model_node = tree_node.data if tree_node is not None else None
        # -- HINT: failed_nodes() walks the whole tree (use it once).
        failed_ids = set(id(node) for node in self.status_tree.failed_nodes())
        while model_node is not None:
            if id(model_node) in failed_ids:
                return True
            model_node = model_node.parent
        return False

    def _ensure_cursor(self):
        """Put the cursor on the first failure (in FOLLOW MODE) or
        at least on a line if it is on none.

        HINT: A fast test run ends before FOLLOW MODE has moved the cursor
        (keys that use the cursor line would do nothing, like: rerun).
        The first failure is preferred, otherwise the first line.
        """
        tree = self.tree_view
        failed_nodes = self.status_tree.failed_nodes()
        if failed_nodes and self.follow and not self._has_failed_cursor():
            # -- FOLLOW MODE ENDS HERE: Show what needs attention (the
            # cursor was not moved by the user, it is on the last test).
            self.action_next_failure()
            return
        if tree.cursor_node is not None or not tree.root.children:
            return
        if failed_nodes:
            self.action_next_failure()
        if tree.cursor_node is None:
            self._move_cursor_to(tree.root.children[0])

    def _testrun_restarted(self):
        """The test run starts again (event: rerun_started)."""
        self.is_done = False
        self.testrun_failed = None
        self.interrupting = False
        self.start_time = time.time()
        self.end_time = None
        self.follow = True
        self._last_point = None
        self._failure_index = -1
        # -- HINT: A changed feature file drops its (old) nodes.
        self._forget_dead_nodes()
        self.refresh_bindings()     # -- HIDE: The rerun keys (footer).

    # -------------------------------------------------------------------------
    # RENDERING
    # -------------------------------------------------------------------------
    @property
    def spinner_frame(self):
        return SPINNER_FRAMES[self._spinner_index % len(SPINNER_FRAMES)]

    def _flush_updates(self):
        """Apply all pending model updates to the tree widget (batched)."""
        rebuilt = self._pending_rebuilt
        added = self._pending_added
        changed = self._pending_changed
        self._pending_rebuilt = []
        self._pending_added = []
        self._pending_changed = []
        self._pending_changed_ids = set()

        for node in rebuilt:
            self._rebuild_children(node)
        for node in added:
            self._show_added_node(node)
        if self._visible is not None and (rebuilt or added or changed):
            # -- FILTER IS ACTIVE: Some lines may appear/disappear now.
            self._refresh_filter()
        for node in changed:
            if node.state == RUNNING:
                self._running_nodes[id(node)] = node
            else:
                self._running_nodes.pop(id(node), None)
            self._refresh_label(node)
            if (node.kind == KIND_SCENARIO and node.state == FAILED
                    and id(node) not in self._auto_expanded
                    and not self.keeps_tree_still):
                # -- AUTO-EXPAND (once): Show why this scenario has failed.
                self._auto_expand_failure(node)
            elif node.is_final:
                self._collapse_if_followed(node)

        self._follow_execution_point()
        self._update_header()
        if self._is_cursor_node_in(changed):
            # -- CASE: The step under the cursor has its definition now, ...
            self._update_location_bar()
        self._sync_timers_state()

    def _is_cursor_node_in(self, nodes):
        """Check if the node under the cursor is one of these (model) nodes."""
        tree_node = self.tree_view.cursor_node
        if tree_node is None or tree_node.data is None:
            return False
        cursor_id = id(tree_node.data)
        return any(id(node) == cursor_id for node in nodes)

    # -------------------------------------------------------------------------
    # FILTER: Which status lines are shown (text filter and view mode)
    # -------------------------------------------------------------------------
    @property
    def filter_is_active(self):
        return bool(self.filter_text.strip()) or bool(self.view_states)

    @property
    def view_states(self):
        """Scenario states of the current view mode (or None: all)."""
        return VIEW_MODES.get(self.view_mode)

    def _select_visible(self):
        """Compute the visible nodes of the current filter (or None)."""
        return select_visible_nodes(self.status_tree.root,
                                    self.filter_text, self.view_states)

    def _refresh_filter(self, force=False):
        """Re-apply the current filter (its result may have changed)."""
        visible = self._select_visible()
        if not force and visible == self._visible:
            return False
        self._visible = visible
        self._rebuild_tree()
        return True

    def _is_visible(self, node):
        """Check if a (model) node is shown with the current filter."""
        if self._visible is None:
            return True
        if node.kind in (KIND_STEP, KIND_DETAIL):
            return True     # -- NEVER FILTERED: Shown with their scenario.
        return id(node) in self._visible

    def _is_visible_line(self, node):
        """Check if a line (and all its ancestors) is shown."""
        if self._visible is None:
            return True
        for this_node in [node] + list(node.ancestors()):
            if not self._is_visible(this_node):
                return False
        return True

    def _rebuild_tree(self):
        """Rebuild the TreeNodes (the filter shows other lines now).

        HINT: Keeps the expanded nodes, the materialized nodes and -- if
        possible -- the line under the cursor. This also holds for nodes
        that a filter hides now (their state is remembered, see:
        self._expanded_ids, self._materialized_ever).
        """
        tree = self.tree_view
        # -- MERGE: Only the nodes that are shown now can have changed.
        for node_id, tree_node in self._tree_nodes.items():
            if tree_node.is_expanded:
                self._expanded_ids.add(node_id)
            else:
                self._expanded_ids.discard(node_id)
        self._materialized_ever |= self._materialized
        cursor_node = tree.cursor_node
        cursor_id = None
        if cursor_node is not None and cursor_node.data is not None:
            cursor_id = id(cursor_node.data)
        cursor_line = tree.cursor_line

        root = self.status_tree.root
        tree.clear()        # -- HINT: Keeps the root node (and its data).
        self._tree_nodes = {id(root): tree.root}
        self._materialized = set()
        self._app_toggles = {}
        self._app_expand(tree.root)
        self._restore_tree(root, self._expanded_ids, self._materialized_ever)
        self._restore_cursor(cursor_id, cursor_line)
        self._auto_expand_failures()
        self._forget_dead_nodes()
        self._update_location_bar()

    def _forget_dead_nodes(self):
        """Drop the ids of (model) nodes that are not in the tree any more.

        HINT: Otherwise a new node could reuse the id() of a dropped one.
        """
        alive = set(id(node) for node in self.status_tree.root.walk())
        for known_ids in (self._expanded_ids, self._materialized_ever,
                          self._auto_expanded, self._followed,
                          self._user_expanded):
            known_ids &= alive
        # -- HINT: A dropped node would keep the spinner timer running.
        for node_id in [node_id for node_id in self._running_nodes
                        if node_id not in alive]:
            self._running_nodes.pop(node_id, None)
        if self._wanted_cursor_id not in alive:
            self._wanted_cursor_id = None

    def _restore_tree(self, model_node, expanded, materialized):
        """Create the TreeNodes below a node again (see: _rebuild_tree())."""
        if id(model_node) not in materialized:
            return
        self._materialize(model_node)
        for child in model_node.children:
            tree_node = self._tree_nodes.get(id(child))
            if tree_node is None:
                continue        # -- FILTERED: Not shown now.
            if id(child) in expanded:
                self._app_expand(tree_node)
            self._restore_tree(child, expanded, materialized)

    def _restore_cursor(self, cursor_id, cursor_line):
        """Put the cursor back on its line (or near it).

        HINT: If the line of the cursor is filtered out, its node is
        remembered -- the cursor returns when the line is shown again.
        """
        tree = self.tree_view
        # pylint: disable=pointless-statement, protected-access
        tree._tree_lines

        def select_line(node_id):
            tree_node = self._tree_nodes.get(node_id)
            if tree_node is not None and tree.get_node_at_line(
                    tree_node.line) is tree_node:
                return tree_node
            return None

        # -- CASE: The line that the cursor wants is shown again.
        tree_node = select_line(self._wanted_cursor_id)
        if tree_node is not None:
            self._wanted_cursor_id = None
            self._app_cursor_node = tree_node
            tree.move_cursor(tree_node)
            return
        tree_node = select_line(cursor_id)
        if tree_node is not None:
            # -- HINT: Keep the wanted line (it may be shown again).
            self._app_cursor_node = tree_node
            tree.move_cursor(tree_node)
            return
        # -- CURSOR LINE IS GONE: Remember it, use the nearest line (or none).
        self._wanted_cursor_id = self._wanted_cursor_id or cursor_id
        last_line = tree.last_line
        if last_line < 0 or cursor_line < 0:
            self._app_cursor_node = None
            tree.cursor_line = -1
            return
        line = min(cursor_line, last_line)
        self._app_cursor_node = tree.get_node_at_line(line)
        tree.cursor_line = line

    # -- FILTER ACTIONS:
    @property
    def filter_input(self):
        """The filter input line (or None, if it is not mounted)."""
        try:
            return self.query_one("#live-filter", FilterInput)
        except Exception:  # pylint: disable=broad-except
            return None

    @property
    def filter_is_shown(self):
        """The filter input line is shown (instead of the location bar)."""
        filter_input = self.filter_input
        return filter_input is not None and filter_input.has_class("-active")

    @property
    def filter_is_open(self):
        """The filter input line is used (it has the focus).

        HINT: Derived from the focus -- a mouse click on the tree (or
        anything else that moves the focus) must not leave a stale flag
        that disables all keys of this app (see: check_action()).
        """
        return self.filter_is_shown and self.focused is self.filter_input

    def action_open_filter(self):
        """Open the filter input line (key: "/")."""
        filter_input = self.filter_input
        if filter_input is None:
            return
        filter_input.add_class("-active")
        self.query_one("#live-location", Static).add_class("-hidden")
        filter_input.value = self.filter_text
        filter_input.cursor_position = len(filter_input.value)
        filter_input.focus()
        self.refresh_bindings()

    def _close_filter(self, cancel=False, refocus=True):
        """Close the filter input line (back to the tree).

        :param cancel:  True: Clear the filter (key: escape).
        :param refocus: True: Move the focus back to the tree.
        """
        if not self.filter_is_shown:
            return
        self._stop_filter_timer()
        filter_input = self.filter_input
        filter_input.remove_class("-active")
        self.query_one("#live-location", Static).remove_class("-hidden")
        if cancel:
            filter_input.value = u""
            self._use_filter_text(u"")
        if refocus:
            self.tree_view.focus()
        self.refresh_bindings()
        self._update_header()
        self._update_location_bar()

    def on_descendant_blur(self, event):
        """Keep the filter when its input line loses the focus (mouse)."""
        filter_input = self.filter_input
        if filter_input is None or event.widget is not filter_input:
            return
        if self._blurred_by_app or not self.app_focus:
            # -- TERMINAL LOST THE FOCUS: Textual blurs everything and puts
            # the focus back on this input later; keep it open and shown.
            # HINT: The AppFocus may arrive before this (delayed) blur.
            self._blurred_by_app = False
            return
        if self.filter_is_shown:
            # -- LIKE "enter": Keep the filter text and close the input.
            self._use_filter_text(filter_input.value)
            self._close_filter(refocus=False)

    def _use_filter_text(self, text):
        """Use this filter text (and show the lines that match)."""
        if text == self.filter_text:
            return
        self.filter_text = text
        self._refresh_filter(force=True)
        self._update_header()

    def on_input_changed(self, event):
        """Filter while typing (debounced)."""
        if event.input.id != "live-filter":
            return
        self._stop_filter_timer()
        self._filter_timer = self.set_timer(FILTER_DELAY,
                                            self._apply_filter_input)

    def _stop_filter_timer(self):
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None

    def _apply_filter_input(self):
        """Use the text of the filter input line (debounced, see above)."""
        self._filter_timer = None
        filter_input = self.filter_input
        if filter_input is None or not self.filter_is_shown:
            return      # -- CLOSED MEANWHILE: Keep its filter (or none).
        self._use_filter_text(filter_input.value)
        self._update_location_bar()

    def on_input_submitted(self, event):
        """Keep the filter and go back to the tree (key: enter)."""
        if event.input.id != "live-filter":
            return
        self._use_filter_text(event.input.value)
        self._close_filter()

    def action_clear_filter(self):
        """Clear the filter (key: escape, in the tree)."""
        if not self.filter_text:
            return      # -- HINT: Do nothing (no filter is active).
        self._use_filter_text(u"")
        self._update_location_bar()

    def action_next_view_mode(self):
        """Show the next view mode: all -> failed -> not passed (key: v)."""
        modes = list(VIEW_MODES)
        index = (modes.index(self.view_mode) + 1) % len(modes)
        self.view_mode = modes[index]
        self._refresh_filter(force=True)
        self._update_header()
        self._update_location_bar()

    # -------------------------------------------------------------------------
    # FOLLOW MODE: Cursor follows the execution point of the test run
    # -------------------------------------------------------------------------
    def _follow_execution_point(self):
        """Move the cursor to the running step/scenario (if: follow mode)."""
        if not self.follow or self.is_done or self.keeps_tree_still:
            return
        point = self.status_tree.execution_point
        if point is None or point is self._last_point:
            return
        self._last_point = point
        if not self._is_visible_line(point):
            return      # -- FILTERED: This line is not shown.
        expand_node = (point.kind != KIND_STEP)
        tree_node = self._reveal(point, expand_node=expand_node, by_app=True)
        if tree_node is not None:
            self._move_cursor_to(tree_node)

    def _collapse_if_followed(self, node):
        """Collapse a finished node again (if: follow mode expanded it)."""
        if not self.follow or node.state == FAILED:
            return
        if node.kind == KIND_SCENARIO and node.state not in (PASSED, SKIPPED):
            return
        if id(node) in self._user_expanded or id(node) not in self._followed:
            return      # -- NEVER: Collapse what the user has expanded.
        tree_node = self._tree_nodes.get(id(node))
        if tree_node is not None and tree_node.is_expanded:
            self._app_collapse(tree_node)
        self._followed.discard(id(node))

    def _stop_follow(self):
        """Stop the follow mode (the user navigates on his own now)."""
        if self.follow:
            self.follow = False
            self._update_header()

    def _sync_timers_state(self):
        """Pause/resume the animation and the flush timer (as needed)."""
        if self._spinner_timer is not None:
            if self._running_nodes and not self.is_done:
                self._spinner_timer.resume()
            else:
                self._spinner_timer.pause()
        if self._header_timer is not None:
            # -- HINT: The header (spinner, clock) must not freeze when no
            # node is running (hooks, gaps between features, start-up).
            if self.is_done:
                self._header_timer.pause()
            else:
                self._header_timer.resume()
        if (self._flush_timer is not None and not self._pending_changed
                and not self._pending_added and not self._pending_rebuilt):
            self._flush_timer.pause()

    def _tick_header(self):
        """Keep the header alive (spinner, elapsed time) while it runs."""
        if not self._running_nodes:
            # -- HINT: Otherwise the spinner timer animates it (faster).
            self._spinner_index += 1
        self._update_header()

    def _animate_spinner(self):
        """Animate the spinner of all running nodes (one app-level timer)."""
        self._spinner_index += 1
        for node in list(self._running_nodes.values()):
            if node.state != RUNNING:
                self._running_nodes.pop(id(node), None)
                continue
            self._refresh_label(node)
        self._update_header()
        self._sync_timers_state()

    def _update_header(self):
        """Update the one-line header (status and counts of the test run)."""
        try:
            header = self.query_one("#live-header", Static)
        except Exception:  # pylint: disable=broad-except
            return      # -- CASE: Not mounted (yet).
        header.update(self._make_header())

    def _update_location_bar(self):
        """Update the location bar: Where does this line come from?"""
        try:
            location_bar = self.query_one("#live-location", Static)
        except Exception:  # pylint: disable=broad-except
            return      # -- CASE: Not mounted (yet).
        tree = self.tree_view
        width = location_bar.size.width or (self.size.width - 2)
        if self.filter_is_active and tree.last_line < 0:
            # -- EMPTY VIEW: Nothing matches the filter.
            location_bar.update(Text(NO_MATCH_TEXT, style="dim"))
            return
        tree_node = tree.cursor_node
        model_node = tree_node.data if tree_node is not None else None
        location_bar.update(make_location_text(model_node, width))

    def _make_header(self):
        counts = self.status_tree.counts()
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(u"behave", style="bold")
        text.append(u"  ")
        if self.is_done:
            # -- HINT: A partial rerun may have passed, but other scenarios
            # may still be failed (or not run at all).
            failed = (self.testrun_failed
                      or bool(self.status_tree.failed_nodes()))
            incomplete = (counts[UNTESTED] + counts[PENDING]
                          + counts[RUNNING])
            if self.testrun_failed is None:
                text.append(u"%s crashed" % STATE_ICON[FAILED][0], style="red")
            elif failed:
                text.append(u"%s failed" % STATE_ICON[FAILED][0], style="red")
            elif incomplete:
                text.append(u"%s incomplete" % STATE_ICON[UNTESTED][0],
                            style="dim")
            else:
                text.append(u"%s passed" % STATE_ICON[PASSED][0],
                            style="green")
            text.append(u"  ")
            text.append(u"%d passed" % counts[PASSED], style="dim")
            for state, style in ((FAILED, "red"), (SKIPPED, "dim"),
                                 (UNTESTED, "dim")):
                if counts[state]:
                    text.append(u" · ", style="dim")
                    text.append(u"%d %s" % (counts[state], state), style=style)
            elapsed = (self.end_time or time.time()) - self.start_time
        else:
            text.append(self.spinner_frame, style=ACCENT_STYLE)
            if self.interrupting:
                text.append(u" stopping…", style=ACCENT_STYLE)
            else:
                text.append(u" running", style=ACCENT_STYLE)
            text.append(u"  ")
            text.append(u"%d/%d scenarios" % (counts["done"], counts["total"]),
                        style="dim")
            if counts[FAILED]:
                text.append(u" · ", style="dim")
                text.append(u"%d failed" % counts[FAILED], style="red")
            elapsed = time.time() - self.start_time
        text.append(u" · ", style="dim")
        text.append(format_elapsed(elapsed), style="dim")
        if not self.is_done:
            # -- FOLLOW MODE: Only meaningful while the test run is active.
            text.append(u" · ", style="dim")
            text.append(u"follow" if self.follow else u"follow off",
                        style="dim")
        if self.view_states:
            text.append(u" · ", style="dim")
            text.append(u"view: %s" % self.view_mode, style=ACCENT_STYLE)
        if self.filter_text.strip():
            text.append(u" · ", style="dim")
            text.append(u"filter: %s" % self.filter_text.strip(),
                        style=ACCENT_STYLE)
        return text

    def _refresh_label(self, model_node):
        """Update the label of one node (if it is shown in the tree)."""
        tree_node = self._tree_nodes.get(id(model_node))
        if tree_node is None:
            return      # -- NOT MATERIALIZED (yet): Nothing to refresh.
        tree_node.set_label(make_label(model_node, self.spinner_frame))
        if model_node.kind == KIND_STEP and model_node.children:
            # -- CASE: Step has error/output details now (expandable).
            tree_node.allow_expand = True

    # -------------------------------------------------------------------------
    # TREE (materialization: TreeNodes are created lazily, when needed)
    # -------------------------------------------------------------------------
    def _materialize(self, model_node):
        """Create the TreeNodes for the children of this (model) node."""
        if id(model_node) in self._materialized:
            return
        tree_node = self._tree_nodes.get(id(model_node))
        if tree_node is None:
            return
        self._materialized.add(id(model_node))
        self._materialized_ever.add(id(model_node))
        for child in model_node.children:
            if self._is_visible(child):
                self._add_tree_node(tree_node, child)

    def _add_tree_node(self, parent_tree_node, model_node):
        """Create one TreeNode for a (model) node (and remember it)."""
        if id(model_node) in self._tree_nodes:
            return self._tree_nodes[id(model_node)]
        allow_expand = True
        if model_node.kind == KIND_DETAIL:
            allow_expand = False
        elif model_node.kind == KIND_STEP:
            allow_expand = bool(model_node.children)
        tree_node = parent_tree_node.add(
            make_label(model_node, self.spinner_frame), data=model_node,
            allow_expand=allow_expand)
        self._tree_nodes[id(model_node)] = tree_node
        if model_node.state == RUNNING:
            self._running_nodes[id(model_node)] = model_node
        return tree_node

    def _show_added_node(self, model_node):
        """Show a new (model) node, if its parent is already materialized."""
        parent = model_node.parent
        if parent is None or id(parent) not in self._materialized:
            return      # -- LAZY: Created when the parent is expanded.
        parent_tree_node = self._tree_nodes.get(id(parent))
        if parent_tree_node is None or not self._is_visible(model_node):
            return
        self._add_tree_node(parent_tree_node, model_node)
        if parent.kind == KIND_STEP:
            parent_tree_node.allow_expand = True

    def _rebuild_children(self, model_node):
        """Recreate the children of a node (its children have changed).

        HINT: Only some children may be gone (a rerun drops the details of
        the last run). Therefore: The expanded/materialized state of the
        children that are still there is kept (they are the same nodes).
        """
        tree_node = self._tree_nodes.get(id(model_node))
        if tree_node is None:
            return
        was_expanded = tree_node.is_expanded
        alive_ids = set(id(node) for node in model_node.walk())
        self._forget_children(tree_node, alive_ids)
        tree_node.remove_children()
        self._materialized.discard(id(model_node))
        # -- RERUN: This node may fail again (auto-expand it again).
        self._auto_expanded.discard(id(model_node))
        if was_expanded:
            # -- HINT: Expands the children again that were expanded.
            self._restore_tree(model_node, self._expanded_ids,
                               self._materialized_ever)

    def _forget_children(self, tree_node, alive_ids=None):
        """Forget the TreeNodes below this TreeNode (they are recreated).

        :param alive_ids:  Ids of (model) nodes that still exist: Their
            expanded/materialized state is kept (only their TreeNode is
            recreated). Everything else is forgotten.
        """
        alive_ids = alive_ids or set()
        todo = list(tree_node.children)
        while todo:
            this_tree_node = todo.pop()
            todo.extend(this_tree_node.children)
            for what in ("expand", "collapse"):
                self._app_toggles.pop((id(this_tree_node), what), None)
            model_node = this_tree_node.data
            if model_node is None:
                continue
            node_id = id(model_node)
            self._tree_nodes.pop(node_id, None)
            self._materialized.discard(node_id)
            if node_id in alive_ids:
                continue    # -- STILL THERE: Keep its state (see above).
            self._materialized_ever.discard(node_id)
            self._expanded_ids.discard(node_id)
            self._running_nodes.pop(node_id, None)
            self._followed.discard(node_id)
            self._user_expanded.discard(node_id)
            self._auto_expanded.discard(node_id)

    def _reveal(self, model_node, expand_node=False, by_app=False):
        """Make a (model) node visible: materialize/expand its ancestors.

        :param model_node:  Node that should become visible.
        :param expand_node:  Expand this node itself, too.
        :param by_app:  True, if this is done by the app (not by the user).
        :return: TreeNode of this node (or None, if it is unknown).
        """
        ancestors = [node for node in model_node.ancestors()]
        for ancestor in reversed(ancestors):
            self._materialize(ancestor)
            tree_node = self._tree_nodes.get(id(ancestor))
            if tree_node is not None:
                self._expand(tree_node, by_app)
        tree_node = self._tree_nodes.get(id(model_node))
        if tree_node is not None and expand_node:
            self._materialize(model_node)
            self._expand(tree_node, by_app)
        return tree_node

    def _auto_expand_failure(self, scenario):
        """Expand a failed scenario and its failed steps (once).

        HINT: Only if it is shown -- a scenario that fails while it is
        filtered out is expanded when it is shown again (not: never).
        """
        tree_node = self._reveal(scenario, expand_node=True, by_app=True)
        if tree_node is None:
            return False        # -- FILTERED: Try it again (when shown).
        self._auto_expanded.add(id(scenario))
        self._expand_failed_steps(scenario)
        return True

    def _auto_expand_failures(self):
        """Expand the failed scenarios that are shown now (once each)."""
        if self.keeps_tree_still:
            return
        for node in self.visible_failed_nodes():
            if (node.kind == KIND_SCENARIO
                    and id(node) not in self._auto_expanded):
                self._auto_expand_failure(node)

    def _expand_failed_steps(self, scenario):
        """Expand the failed steps of a scenario (show: error details)."""
        for child in scenario.children:
            if child.state == FAILED and child.children:
                self._materialize(child)
                tree_node = self._tree_nodes.get(id(child))
                if tree_node is not None:
                    tree_node.allow_expand = True
                    self._expand(tree_node, by_app=True)

    # -- EXPAND/COLLAPSE: Remember who did it (app or user).
    def _expand(self, tree_node, by_app):
        if by_app:
            return self._app_expand(tree_node, followed=True)
        tree_node.expand()      # -- USER: See on_tree_node_expanded().
        return True

    def _app_expand(self, tree_node, followed=False):
        """Expand a TreeNode (by the app, not by the user)."""
        if tree_node.is_expanded:
            return False
        self._remember_app_toggle(tree_node, "expand")
        tree_node.expand()
        model_node = tree_node.data
        if model_node is not None:
            self._expanded_ids.add(id(model_node))
        if (followed and model_node is not None
                and id(model_node) not in self._user_expanded):
            self._followed.add(id(model_node))
        return True

    def _app_collapse(self, tree_node):
        """Collapse a TreeNode (by the app, not by the user)."""
        self._remember_app_toggle(tree_node, "collapse")
        if tree_node.data is not None:
            self._expanded_ids.discard(id(tree_node.data))
        tree_node.collapse()

    def _remember_app_toggle(self, tree_node, what):
        key = (id(tree_node), what)
        self._app_toggles[key] = self._app_toggles.get(key, 0) + 1

    def _take_app_toggle(self, tree_node, what):
        """Check if this expand/collapse was made by the app (not the user)."""
        key = (id(tree_node), what)
        count = self._app_toggles.get(key, 0)
        if not count:
            return False
        self._app_toggles[key] = count - 1
        return True

    # -------------------------------------------------------------------------
    # TREE EVENTS
    # -------------------------------------------------------------------------
    def _is_stale_event(self, event):
        """Check if this event belongs to a TreeNode that was replaced.

        HINT: A rebuild creates new TreeNodes; the expand/collapse events
        of the old ones may arrive afterwards (they are posted, not sent).
        They are not made by the user (and their TreeNode is gone).
        """
        model_node = event.node.data
        return (model_node is not None
                and self._tree_nodes.get(id(model_node)) is not event.node)

    def on_tree_node_expanded(self, event):
        """Create the children of a node when it is expanded (lazily)."""
        if self._is_stale_event(event):
            return      # -- STALE: This TreeNode was replaced by a rebuild.
        model_node = event.node.data
        if model_node is not None:
            self._materialize(model_node)
        if model_node is not None:
            self._expanded_ids.add(id(model_node))
        if self._take_app_toggle(event.node, "expand"):
            return      # -- BY APP: Follow mode or auto-expand on failure.
        # -- BY USER: Never collapse this node automatically (and: no follow).
        if model_node is not None:
            self._user_expanded.add(id(model_node))
            self._followed.discard(id(model_node))
        self._stop_follow()

    def on_tree_node_collapsed(self, event):
        """Remember that the user has collapsed a node."""
        if self._is_stale_event(event):
            return      # -- STALE: This TreeNode was replaced by a rebuild.
        model_node = event.node.data
        if model_node is not None:
            self._expanded_ids.discard(id(model_node))
        if self._take_app_toggle(event.node, "collapse"):
            return      # -- BY APP: Follow mode (finished scenario/feature).
        if model_node is not None:
            self._user_expanded.discard(id(model_node))
            self._followed.discard(id(model_node))
        self._stop_follow()

    def on_tree_node_highlighted(self, event):
        """Show the locations of the node under the cursor.

        HINT: Any cursor move that this app did not make itself is made by
        the user (keys of the Tree widget: home/end/page-up/..., mouse).
        """
        if event.node is not self._app_cursor_node:
            # -- BY USER: Forget the line that the cursor wanted.
            # HINT: The Tree adjusts its cursor line when lines above it
            # are added/removed -- that is the SAME node (not a user move).
            self._wanted_cursor_id = None
        self._app_cursor_node = event.node  # -- CURSOR IS ON THIS NODE NOW.
        self._update_location_bar()

    def on_tree_node_selected(self, event):
        """Toggle a node when it is selected (with the mouse)."""
        self._stop_follow()
        tree_node = event.node
        if tree_node.allow_expand:
            if not tree_node.is_expanded and tree_node.data is not None:
                self._materialize(tree_node.data)
            tree_node.toggle()

    def on_mouse_scroll_down(self, event):
        # pylint: disable=unused-argument
        self._stop_follow()

    def on_mouse_scroll_up(self, event):
        # pylint: disable=unused-argument
        self._stop_follow()

    # -------------------------------------------------------------------------
    # ACTIONS (key bindings)
    # -------------------------------------------------------------------------
    def action_stop_or_quit(self):
        """Stop the test run (first press) or quit this app (second press)."""
        if self.is_done or self.interrupting:
            # -- SECOND PRESS (or: test run has ended): Quit now.
            self.exit()
            return
        self.interrupting = True
        self._update_header()
        if self.on_interrupt is not None:
            try:
                self.on_interrupt()
            except Exception as e:  # pylint: disable=broad-except
                self.log("live: on_interrupt() FAILED: %s" % e)

    def action_stop_or_quit_now(self):
        """Same as "q" -- but it works while the filter input is used."""
        self.action_stop_or_quit()

    def action_force_quit(self):
        self.exit()

    def action_help(self):
        """Show all keys of this app (key: "?")."""
        if self.dialog_is_open:
            return
        self.dialog_is_open = True
        self.refresh_bindings()
        self.push_screen(HelpScreen(), self._on_dialog_closed)

    def _on_dialog_closed(self, result):
        """Called when a modal dialog was closed (see: HelpScreen)."""
        # pylint: disable=unused-argument
        self.dialog_is_open = False
        self.refresh_bindings()

    def action_toggle_follow(self):
        """Toggle the follow mode (and: jump to the execution point)."""
        self.follow = not self.follow
        if self.follow:
            self._last_point = None
            self._follow_execution_point()
        self._update_header()

    def action_cursor_down(self):
        self._stop_follow()
        self.tree_view.action_cursor_down()

    def action_cursor_up(self):
        self._stop_follow()
        self.tree_view.action_cursor_up()

    def action_toggle_node(self):
        """Expand/collapse the current node (with: enter, space)."""
        self._stop_follow()
        tree_node = self.tree_view.cursor_node
        if tree_node is None or not tree_node.allow_expand:
            return
        if not tree_node.is_expanded and tree_node.data is not None:
            self._materialize(tree_node.data)
        tree_node.toggle()

    def action_expand_node(self):
        """Expand the current node (or: step into its first child)."""
        self._stop_follow()
        tree_node = self.tree_view.cursor_node
        if tree_node is None:
            return
        if tree_node.allow_expand and not tree_node.is_expanded:
            if tree_node.data is not None:
                self._materialize(tree_node.data)
            tree_node.expand()
        elif tree_node.children:
            self._move_cursor_to(tree_node.children[0])

    def action_collapse_node(self):
        """Collapse the current node (or: move to its parent node)."""
        self._stop_follow()
        tree_node = self.tree_view.cursor_node
        if tree_node is None:
            return
        if tree_node.allow_expand and tree_node.is_expanded:
            tree_node.collapse()
        elif tree_node.parent is not None and not tree_node.parent.is_root:
            self._move_cursor_to(tree_node.parent)

    def action_collapse_all(self):
        """Collapse everything: only the feature lines remain."""
        self._stop_follow()
        tree = self.tree_view
        tree.root.collapse_all()
        # -- ALSO: The nodes that a filter hides now (they have no TreeNode).
        self._expanded_ids.clear()
        self._app_expand(tree.root)
        self._user_expanded.clear()
        self._followed.clear()

    def visible_failed_nodes(self):
        """Failed nodes that the current filter shows (as list)."""
        failed_nodes = self.status_tree.failed_nodes()
        if self._visible is None:
            return failed_nodes
        return [node for node in failed_nodes if id(node) in self._visible]

    def action_expand_failures(self):
        """Expand all failed scenarios (and their failed steps)."""
        self._stop_follow()
        for model_node in self.visible_failed_nodes():
            self._reveal(model_node, expand_node=True)
            self._expand_failed_steps(model_node)

    def action_next_failure(self):
        self._goto_failure(1)

    def action_previous_failure(self):
        self._goto_failure(-1)

    def _goto_failure(self, step):
        """Move the cursor to the next/previous failure (cycles around)."""
        self._stop_follow()
        failed_nodes = self.visible_failed_nodes()
        if not failed_nodes:
            return
        index = (self._failure_index + step) % len(failed_nodes)
        self._failure_index = index
        tree_node = self._reveal(failed_nodes[index])
        if tree_node is not None:
            self._move_cursor_to(tree_node)

    # -- RERUN: Only after the test run has ended (see: check_action()).
    def check_action(self, action, parameters):
        """Enable/disable the rerun keys (dynamic bindings, see: Footer)."""
        # pylint: disable=unused-argument
        if action in ("force_quit", "stop_or_quit_now"):
            return True     # -- ALWAYS: ctrl+q and ctrl+c work.
        if self.dialog_is_open or self.filter_is_shown:
            # -- MODAL DIALOG or FILTER INPUT: These keys belong to them.
            # HINT: App bindings with priority=True are checked even when
            # a modal screen is shown (therefore: disable them here).
            # HINT: Single-letter keys must be TEXT in the filter input.
            # HINT: None -- disabled, but still shown in the footer.
            return None
        if action in ("open_source", "open_definition"):
            # -- HINT: Works while the test run is active, too.
            return self.on_open is not None
        if action in ("rerun_failed", "rerun_selected"):
            return bool(self.is_done and self.on_rerun is not None)
        if action == "toggle_follow":
            # -- HINT: Follow mode is useless after the test run.
            return not self.is_done
        return True

    def action_rerun_failed(self):
        """Run all failed scenarios again (with a filter: the shown ones)."""
        failed_nodes = self.visible_failed_nodes()
        locations = [node.location for node in failed_nodes if node.location]
        if not locations:
            self._notify(u"No failures to rerun.")
            return
        if self.filter_is_active:
            self._notify(u"Rerunning %d visible failure%s"
                         % (len(locations), u"" if len(locations) == 1
                            else u"s"))
        self._request_rerun(locations)

    def action_rerun_selected(self):
        """Run the feature/scenario under the cursor again."""
        tree_node = self.tree_view.cursor_node
        model_node = tree_node.data if tree_node is not None else None
        location = model_node.location if model_node is not None else None
        if not location:
            self._notify(u"Nothing selected to rerun.")
            return
        self._request_rerun([location])

    def _request_rerun(self, locations):
        """Ask the caller to run these locations again.

        Checks first if source files were changed since they were loaded:
        changed Python modules may be reloaded (the user decides).

        HINT: The caller resets the status tree by sending: rerun_started.
        """
        if not (self.is_done and self.on_rerun is not None):
            return
        changes = self._select_changes(locations)
        warned = changes.get("warn") or []
        features = changes.get("features") or []
        reloadable = changes.get("reload") or []
        if warned:
            self._notify(u"Changed, but cannot be reloaded "
                         u"(restart behave): %s" % join_names(warned),
                         severity="warning", timeout=10.0)
        if features:
            self._notify(u"Feature file changed, running all of it: %s"
                         % join_names(features))
        if reloadable:
            self._ask_reload(locations, reloadable)
            return
        self._start_rerun(locations, reload=False)

    def _select_changes(self, locations):
        """Ask the caller which source files were changed (as dict)."""
        if self.on_check_changes is None:
            return {}
        try:
            changes = self.on_check_changes(locations)
        except Exception as e:  # pylint: disable=broad-except
            # -- ROBUSTNESS: Rerun without reloading anything.
            self.log("live: on_check_changes() FAILED: %s" % e)
            return {}
        if not isinstance(changes, dict):
            return {}
        return changes

    def _ask_reload(self, locations, filenames):
        """Show the modal dialog: Reload the changed source files?"""
        def on_answer(answer):
            self._on_dialog_closed(answer)
            if answer == "reload":
                self._start_rerun(locations, reload=True)
            elif answer == "old":
                self._start_rerun(locations, reload=False)
            # -- OTHERWISE: Canceled (no rerun).

        self.dialog_is_open = True
        self.refresh_bindings()
        self.push_screen(ReloadDialog(filenames), on_answer)

    def _start_rerun(self, locations, reload=False):
        """Ask the caller to run these locations again (now).

        :param reload:  True, if changed source modules should be reloaded.
        """
        try:
            accepted = self.on_rerun(locations, reload=reload)
        except Exception as e:  # pylint: disable=broad-except
            self.log("live: on_rerun() FAILED: %s" % e)
            return
        if not accepted:
            self._notify(u"Cannot rerun now.")

    # -- OPEN IN EDITOR: Only if the caller provides on_open().
    def action_open_source(self):
        """Open the feature file of the current line (in an editor)."""
        location = self._select_location("source_location")
        if location is None:
            self._notify(u"Nothing to open.")
            return
        self._open_location(location)

    def action_open_definition(self):
        """Open the step definition of the current line (in an editor)."""
        location = self._select_location("definition_location")
        if location is None:
            self._notify(u"No step definition known for this line "
                         u"(it is known when the step has run).")
            return
        self._open_location(location)

    def _select_location(self, location_name):
        """Select a location of the node under the cursor (or None)."""
        tree_node = self.tree_view.cursor_node
        model_node = tree_node.data if tree_node is not None else None
        if model_node is None:
            return None
        return getattr(model_node, location_name, None)

    def _open_location(self, location):
        """Ask the caller to open this location in an editor.

        HINT: on_open() is called in the app thread and it may block for
        a long time (a terminal editor uses :meth:`App.suspend()`).
        Therefore: Do not use a worker/thread here and repaint afterwards.
        """
        filename, line = location
        message = None
        try:
            message = self.on_open(filename, line)
        except Exception as e:  # pylint: disable=broad-except
            self.log("live: on_open() FAILED: %s" % e)
            message = u"Cannot open %s: %s" % (filename, e)
        # -- AFTER THE EDITOR: The terminal was used by another program.
        self.refresh()
        if message:
            self._notify(message, severity="warning", timeout=8.0)

    # -- COPY TO CLIPBOARD:
    def action_copy_report(self):
        """Copy a text report of the current line to the clipboard."""
        model_node = self._select_cursor_node()
        if model_node is None:
            self._notify(u"Nothing to copy.")
            return
        lines = make_text_report(model_node)
        if not lines:
            self._notify(u"Nothing to copy.")
            return
        self._copy_text(u"\n".join(lines),
                        u"Copied: %d line%s" % (len(lines),
                                                u"" if len(lines) == 1
                                                else u"s"))

    def action_copy_location(self):
        """Copy the location of the current line to the clipboard."""
        model_node = self._select_cursor_node()
        location = model_node.source_location if model_node else None
        if location is None:
            self._notify(u"Nothing to copy.")
            return
        text = format_location(location)
        definition = model_node.definition_location
        if definition is not None:
            text += u" -> %s" % format_location(definition)
        self._copy_text(text, u"Copied: %s" % text)

    def _select_cursor_node(self):
        """The (model) node under the cursor (or None)."""
        tree_node = self.tree_view.cursor_node
        return tree_node.data if tree_node is not None else None

    def _copy_text(self, text, message):
        """Copy text to the clipboard (with the caller, or with textual)."""
        copied = False
        if self.on_copy is not None:
            try:
                copied = bool(self.on_copy(text))
            except Exception as e:  # pylint: disable=broad-except
                self.log("live: on_copy() FAILED: %s" % e)
        if not copied:
            # -- FALLBACK: Use the terminal (OSC 52 escape sequence).
            try:
                self.copy_to_clipboard(text)
            except Exception as e:  # pylint: disable=broad-except
                self.log("live: copy_to_clipboard() FAILED: %s" % e)
                self._notify(u"Cannot copy to the clipboard.",
                             severity="warning")
                return
        self._notify(message)

    def _notify(self, message, severity="information", timeout=3.0):
        """Show a message (HINT: Its text may contain any characters)."""
        try:
            # -- MARKUP: OFF -- file names/errors may contain "[...]".
            self.notify(message, severity=severity, timeout=timeout,
                        markup=False)
        except Exception:  # pylint: disable=broad-except
            pass        # -- ROBUSTNESS: Notifications are optional.

    def _move_cursor_to(self, tree_node):
        """Move the tree cursor to this TreeNode (and scroll to it)."""
        self._wanted_cursor_id = None
        tree = self.tree_view
        # -- HINT: Force a rebuild, otherwise TreeNode.line may be outdated.
        # pylint: disable=pointless-statement, protected-access
        tree._tree_lines
        self._app_cursor_node = tree_node   # -- NOT: A cursor move by user.
        tree.move_cursor(tree_node)
        self._update_location_bar()
