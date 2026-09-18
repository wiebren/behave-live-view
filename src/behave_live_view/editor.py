# -*- coding: UTF-8 -*-
"""
Open a source location (file and line) in an editor or IDE
(used by the "live" formatter).

The editor command is selected in this order:

1. Environment variable ``BEHAVE_EDITOR`` (or userdata ``live.editor``),
   for example: ``code -g {file}:{line}`` or just ``pycharm``.
2. The IDE whose terminal is used (VS Code, JetBrains IDEs), if its
   command-line launcher is installed.
3. Environment variables ``VISUAL`` and ``EDITOR``.

A command may use the placeholders ``{file}`` and ``{line}``. Without them,
the arguments are chosen by the name of the editor (well-known editors).
"""

import os
import shlex
import shutil
import subprocess


#: Editors with own window: Are started in the background.
#: All others need the terminal (the interactive view is suspended).
GUI_EDITORS = frozenset([
    "code", "code-insiders", "codium", "cursor", "windsurf", "zed", "subl",
    "sublime_text", "mate", "atom", "gedit", "kate", "notepad++", "open",
    "xdg-open", "gvim", "mvim", "emacsclient",
    # -- JETBRAINS IDEs:
    "idea", "pycharm", "charm", "webstorm", "phpstorm", "goland", "rubymine",
    "clion", "rider", "datagrip", "rustrover", "fleet",
])
JETBRAINS_EDITORS = frozenset([
    "idea", "pycharm", "charm", "webstorm", "phpstorm", "goland", "rubymine",
    "clion", "rider", "datagrip", "rustrover",
])
#: Editors that use: EDITOR FILE:LINE
FILE_COLON_LINE_EDITORS = frozenset([
    "zed", "subl", "sublime_text", "atom", "hx", "helix", "fleet",
])
#: Editors that use: EDITOR -g FILE:LINE
GOTO_OPTION_EDITORS = frozenset([
    "code", "code-insiders", "codium", "cursor", "windsurf",
])


class EditorError(Exception):
    """Raised if no editor can be used (message is shown to the user)."""


def split_command(command):
    """Split an editor command into its arguments.

    HINT: Windows -- backslashes of paths must be kept (non-POSIX mode),
    but this mode keeps the quotes of a quoted path, too.
    """
    if os.name != "nt":
        return shlex.split(command)
    command_args = shlex.split(command, posix=False)
    return [arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1]
            and arg[0] in "\"'" else arg for arg in command_args]


def select_editor_name(command_args):
    # -- HINT: Accept both path separators (on any platform).
    name = command_args[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".sh"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name


def select_jetbrains_candidates(environ):
    """JetBrains launchers to try: The IDE that is used comes first.

    HINT: macOS tells which application has started the terminal.
    """
    product = environ.get("__CFBundleIdentifier", "").lower()
    candidates = sorted(JETBRAINS_EDITORS)
    # -- HINT: Longest name first, "pycharm" before its alias "charm".
    preferred = sorted([name for name in candidates if name in product],
                       key=lambda name: (-len(name), name))
    if "intellij" in product:
        preferred.append("idea")
    return preferred + [name for name in candidates if name not in preferred]


def select_editor_command(environ=None, configured=None, which=shutil.which):
    """Select the editor command to use (as string).

    :param environ:     Environment variables (default: os.environ).
    :param configured:  Editor command from the configuration (or None).
    :raises EditorError: If no editor is known.
    """
    environ = os.environ if environ is None else environ
    command = configured or environ.get("BEHAVE_EDITOR")
    if command:
        return command

    # -- IDE TERMINAL: Prefer the IDE that the user is working in.
    candidates = []
    if environ.get("TERM_PROGRAM") == "vscode":
        candidates = ["code", "cursor", "codium", "code-insiders"]
    elif "jetbrains" in environ.get("TERMINAL_EMULATOR", "").lower():
        candidates = select_jetbrains_candidates(environ)
    for candidate in candidates:
        if which(candidate):
            return candidate

    command = environ.get("VISUAL") or environ.get("EDITOR")
    if command:
        return command
    raise EditorError(
        "No editor known: set BEHAVE_EDITOR, for example to "
        "'code -g {file}:{line}', 'pycharm' or 'vim'.")


def make_editor_args(command, filename, line=None):
    """Make the command-line to open a file at a line (as list).

    :param command:  Editor command, optionally with {file}/{line}.
    """
    line = int(line) if line else 1
    command_args = split_command(command)
    if not command_args:
        raise EditorError("Editor command is empty.")

    if "{file}" in command or "{line}" in command:
        return [arg.replace("{file}", filename).replace("{line}", str(line))
                for arg in command_args]

    name = select_editor_name(command_args)
    if name in GOTO_OPTION_EDITORS:
        return command_args + ["-g", "%s:%d" % (filename, line)]
    if name in FILE_COLON_LINE_EDITORS:
        return command_args + ["%s:%d" % (filename, line)]
    if name in JETBRAINS_EDITORS:
        return command_args + ["--line", str(line), filename]
    if name == "mate":
        return command_args + ["-l", str(line), filename]
    if name in ("open", "xdg-open", "gedit", "kate", "notepad++"):
        return command_args + [filename]    # -- NO SUPPORT: For a line.
    # -- MANY EDITORS: vim, nvim, vi, nano, emacs, emacsclient, gvim, ...
    return command_args + ["+%d" % line, filename]


def needs_terminal(command):
    """Check if the editor runs in the terminal (no own window)."""
    return select_editor_name(split_command(command)) not in GUI_EDITORS


def open_in_editor(filename, line=None, command=None, terminal=None,
                   run=subprocess.call, start=subprocess.Popen):
    """Open a file in the editor.

    :param terminal:  File object of the terminal, for an editor that needs
        one (stdout/stderr of this process may be redirected).
    :raises EditorError: If the editor cannot be started.
    """
    command = command or select_editor_command()
    filename = os.path.abspath(filename)
    if not os.path.exists(filename):
        raise EditorError("File not found: %s" % filename)

    editor_args = make_editor_args(command, filename, line)
    try:
        if needs_terminal(command):
            run(editor_args, stdout=terminal, stderr=terminal)
        else:
            start(editor_args, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  start_new_session=(os.name == "posix"))
    except OSError as e:
        raise EditorError("Cannot start editor '%s': %s"
                          % (editor_args[0], e))
