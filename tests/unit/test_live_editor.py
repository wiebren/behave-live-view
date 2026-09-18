# -*- coding: UTF-8 -*-
"""
Unit tests for :mod:`behave_live_view.editor`:
Open a source location in an editor/IDE.
"""

import os
import subprocess

import pytest

from behave_live_view import editor
from behave_live_view.editor import EditorError


class TestSelectEditorCommand:
    @staticmethod
    def which_none(name):
        return None

    def test_behave_editor_has_priority(self):
        environ = {"BEHAVE_EDITOR": "subl", "VISUAL": "vim",
                   "TERM_PROGRAM": "vscode"}
        assert editor.select_editor_command(environ) == "subl"

    def test_configured_editor_wins(self):
        environ = {"BEHAVE_EDITOR": "subl"}
        command = editor.select_editor_command(environ, configured="zed")
        assert command == "zed"

    def test_vscode_terminal_uses_its_launcher(self):
        environ = {"TERM_PROGRAM": "vscode", "EDITOR": "vim"}
        which = lambda name: "/bin/code" if name == "code" else None
        assert editor.select_editor_command(environ, which=which) == "code"

    def test_jetbrains_terminal_prefers_the_ide_that_is_used(self):
        environ = {"TERMINAL_EMULATOR": "JetBrains-JediTerm",
                   "__CFBundleIdentifier": "com.jetbrains.pycharm"}
        which = lambda name: "/bin/" + name     # -- ALL: Are installed.
        assert editor.select_editor_command(environ, which=which) == "pycharm"

    def test_jetbrains_terminal_uses_an_installed_launcher(self):
        environ = {"TERMINAL_EMULATOR": "JetBrains-JediTerm"}
        which = lambda name: "/bin/idea" if name == "idea" else None
        assert editor.select_editor_command(environ, which=which) == "idea"

    def test_ide_without_launcher_falls_back_to_editor(self):
        environ = {"TERM_PROGRAM": "vscode", "EDITOR": "nano"}
        command = editor.select_editor_command(environ, which=self.which_none)
        assert command == "nano"

    def test_visual_is_preferred_to_editor(self):
        environ = {"VISUAL": "nvim", "EDITOR": "nano"}
        assert editor.select_editor_command(environ) == "nvim"

    def test_no_editor_is_an_error_with_a_hint(self):
        with pytest.raises(EditorError, match="BEHAVE_EDITOR"):
            editor.select_editor_command({}, which=self.which_none)


class TestMakeEditorArgs:
    @pytest.mark.parametrize("command, expected", [
        ("code", ["code", "-g", "a.py:12"]),
        ("cursor --reuse-window", ["cursor", "--reuse-window", "-g", "a.py:12"]),
        ("pycharm", ["pycharm", "--line", "12", "a.py"]),
        ("/usr/local/bin/idea", ["/usr/local/bin/idea", "--line", "12", "a.py"]),
        ("subl", ["subl", "a.py:12"]),
        ("zed", ["zed", "a.py:12"]),
        ("mate -w", ["mate", "-w", "-l", "12", "a.py"]),
        ("vim", ["vim", "+12", "a.py"]),
        ("nvim -p", ["nvim", "-p", "+12", "a.py"]),
        ("nano", ["nano", "+12", "a.py"]),
        ("emacsclient -n", ["emacsclient", "-n", "+12", "a.py"]),
        ("open", ["open", "a.py"]),
        ("myedit --at {line} {file}", ["myedit", "--at", "12", "a.py"]),
        ("myedit {file}:{line}", ["myedit", "a.py:12"]),
    ])
    def test_well_known_editors(self, command, expected):
        assert editor.make_editor_args(command, "a.py", 12) == expected

    def test_unknown_line_opens_the_first_line(self):
        assert editor.make_editor_args("vim", "a.py", None) == \
               ["vim", "+1", "a.py"]

    def test_empty_command_is_an_error(self):
        with pytest.raises(EditorError):
            editor.make_editor_args("  ", "a.py", 1)

    @pytest.mark.parametrize("command, expected", [
        ("vim", True), ("nano", True), ("hx", True), ("emacs -nw", True),
        ("code -g {file}:{line}", False), ("pycharm", False),
        ("/opt/bin/subl -n", False), ("open", False),
    ])
    def test_needs_terminal(self, command, expected):
        assert editor.needs_terminal(command) is expected


class TestOpenInEditor:
    def test_gui_editor_is_started_in_the_background(self, tmp_path):
        source_file = tmp_path / "steps.py"
        source_file.write_text(u"# -- EMPTY\n")
        calls = []
        editor.open_in_editor(
            str(source_file), 7, command="code",
            start=lambda args, **kwargs: calls.append((args, kwargs)))
        args, kwargs = calls[0]
        assert args == ["code", "-g", "%s:7" % source_file]
        assert kwargs["stdout"] == subprocess.DEVNULL

    def test_terminal_editor_runs_with_the_terminal(self, tmp_path):
        source_file = tmp_path / "steps.py"
        source_file.write_text(u"# -- EMPTY\n")
        calls = []
        terminal = object()
        editor.open_in_editor(
            str(source_file), 7, command="vim", terminal=terminal,
            run=lambda args, **kwargs: calls.append((args, kwargs)))
        args, kwargs = calls[0]
        assert args == ["vim", "+7", str(source_file)]
        assert kwargs == {"stdout": terminal, "stderr": terminal}

    def test_relative_filename_is_made_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.feature").write_text(u"Feature: A\n")
        calls = []
        editor.open_in_editor("a.feature", 1, command="subl",
                              start=lambda args, **kw: calls.append(args))
        assert calls == [["subl", "%s:1" % os.path.abspath("a.feature")]]

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(EditorError, match="File not found"):
            editor.open_in_editor(str(tmp_path / "missing.py"), 1,
                                  command="vim")

    def test_editor_that_cannot_be_started_is_an_error(self, tmp_path):
        source_file = tmp_path / "steps.py"
        source_file.write_text(u"# -- EMPTY\n")

        def start(args, **kwargs):
            raise OSError("No such file or directory")

        with pytest.raises(EditorError, match="Cannot start editor 'code'"):
            editor.open_in_editor(str(source_file), 1, command="code",
                                  start=start)


class TestWindowsCommands:
    def test_quoted_path_loses_its_quotes(self, monkeypatch):
        # -- REGRESSION: The editor was never started (path with quotes)
        # and a GUI editor was classified as terminal editor.
        monkeypatch.setattr(editor.os, "name", "nt")
        command = '"C:\\Program Files\\Microsoft VS Code\\bin\\code.cmd" -r'
        assert editor.split_command(command) == [
            "C:\\Program Files\\Microsoft VS Code\\bin\\code.cmd", "-r"]
        assert editor.needs_terminal(command) is False
        assert editor.make_editor_args(command, "a.py", 3)[-2:] == \
               ["-g", "a.py:3"]
