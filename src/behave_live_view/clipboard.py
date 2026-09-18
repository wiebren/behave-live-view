# -*- coding: UTF-8 -*-
"""
Copy text to the system clipboard (used by the "live" formatter).

Uses the clipboard tool of the platform. If there is none, the interactive
view uses the terminal instead (escape sequence OSC 52), which is not
supported by every terminal.
"""

import os
import shutil
import subprocess
import sys


def select_clipboard_commands(platform=None, environ=None):
    """Select the clipboard tools to try on this platform (in this order)."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        return [["pbcopy"]]
    if platform.startswith("win") or platform == "cygwin":
        return [["clip"]]
    commands = []
    if environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-copy"])
    if environ.get("DISPLAY"):
        commands.append(["xclip", "-selection", "clipboard"])
        commands.append(["xsel", "--clipboard", "--input"])
    return commands


def copy_to_clipboard(text, commands=None, which=shutil.which,
                      run=subprocess.run):
    """Copy text to the system clipboard.

    :return: True on success; False if no clipboard tool could be used.
    """
    if commands is None:
        commands = select_clipboard_commands()
    for command in commands:
        if not which(command[0]):
            continue
        # -- HINT: clip.exe uses the console code page for bytes,
        # but it detects UTF-16 (with byte-order mark).
        encoding = "utf-16" if command[0] == "clip" else "utf-8"
        try:
            result = run(command, input=text.encode(encoding), timeout=5,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False
