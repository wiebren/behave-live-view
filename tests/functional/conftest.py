# -*- coding: UTF-8 -*-
"""
Test support for functional tests: A behave project in a working directory,
"behave" as subprocess -- without a terminal or in a pseudo-terminal.
"""

import os
import re
import select
import shlex
import subprocess
import sys
import textwrap
import time

import pytest


BEHAVE_INI_TEXT = u"""
    [behave]
    runner = behave_live_view:LiveRunner
"""

STEPS_TEXT = u"""
    import time
    from behave import step

    def note(text):
        # -- HINT: Tests look at this file to see what was executed.
        with open("calls.txt", "a") as f:
            f.write(text + "\\n")

    @step('a step passes')
    def step_passes(context):
        note("PASSES")
        print("OUTPUT of a passing step")

    @step('a step fails')
    def step_fails(context):
        note("FAILS")
        assert False, "XFAIL-STEP"

    @step('I wait {seconds:d} seconds')
    def step_wait(context, seconds):
        # -- HINT: An interrupt arrives between two (short) blocking calls.
        note("WAIT-STARTED")
        for _ in range(seconds * 10):
            time.sleep(0.1)
        note("WAIT-DONE")
"""

FEATURE_FILES = {
    "features/alice.feature": u"""
        Feature: Alice
          Scenario: A1
            Given a step passes
            When a step passes
          Scenario: A2
            Given a step passes
            When a step fails
        """,
    "features/bob.feature": u"""
        Feature: Bob
          Scenario: B1
            Given a step passes
        """,
}

ESCAPE_SEQUENCES = re.compile(
    r"\x1b\[[0-9;?<>=]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]|\x1b\][^\x07]*\x07")


class CommandResult(object):
    __slots__ = ("returncode", "output")

    def __init__(self, returncode, output):
        self.returncode = returncode
        self.output = output

    def __str__(self):
        return "returncode=%s\n%s" % (self.returncode, self.output)


class Workdir(object):
    """Working directory of one test with a small behave project."""

    def __init__(self, path):
        self.path = path

    def write_file(self, relpath, text):
        path = self.path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="UTF-8")
        return path

    def read_calls(self):
        path = self.path / "calls.txt"
        return path.read_text().split() if path.exists() else []

    def wait_for_calls(self, name, count, timeout=30.0):
        """Wait until a step was executed that often (see: note())."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.read_calls().count(name) >= count:
                return True
            time.sleep(0.1)
        return False


def make_behave_command(args):
    return [sys.executable, "-m", "behave"] + shlex.split(args)


def run_behave(workdir, args, timeout=120):
    """Run behave WITHOUT a terminal (stdout/stderr are pipes)."""
    process = subprocess.run(make_behave_command(args), cwd=str(workdir.path),
                             stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, timeout=timeout)
    return CommandResult(process.returncode, process.stdout + process.stderr)


def run_behave_in_terminal(workdir, args, script, timeout=60.0,
                           size=(40, 120)):
    """Run behave in a pseudo-terminal (the interactive view is shown).

    :param script: Steps as list of (condition, action) that are performed
        in their order: A condition is a text that the terminal must have
        shown or a function that returns true; an action is the text to type
        or a function to call.
    :return: CommandResult; its output is the text of the terminal without
        escape sequences (HINT: The view paints the screen many times).
    """
    # pylint: disable=too-many-locals
    import fcntl
    import pty
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:    # -- CHILD PROCESS: Its stdin/stdout/stderr are the pty.
        os.chdir(str(workdir.path))
        os.environ["TERM"] = "xterm-256color"
        command = make_behave_command(args)
        os.execv(command[0], command)

    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", size[0], size[1], 0, 0))
    output = b""
    script = list(script)
    status = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            try:
                data = os.read(fd, 65536)
            except OSError:     # -- CLOSED: Child process has ended (Linux).
                data = b""
            output += data
        if script:
            condition, action = script[0]
            fulfilled = (condition() if callable(condition)
                         else condition.encode("UTF-8") in output)
            if fulfilled:
                script.pop(0)
                time.sleep(0.5)     # -- ENSURE: View has processed it.
                if callable(action):
                    action()
                else:
                    os.write(fd, action.encode("UTF-8"))
        done_pid, done_status = os.waitpid(pid, os.WNOHANG)
        if done_pid:
            status = done_status
            break
    if status is None:
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        returncode = "TIMEOUT"
    else:
        returncode = os.waitstatus_to_exitcode(status)
    os.close(fd)
    text = ESCAPE_SEQUENCES.sub("", output.decode("UTF-8", "replace"))
    if script:
        text += "\nSCRIPT NOT COMPLETED: %d step(s) left" % len(script)
    return CommandResult(returncode, text)


needs_pseudo_terminal = pytest.mark.skipif(
    sys.platform == "win32", reason="REQUIRES: pseudo-terminal (pty)")


@pytest.fixture
def workdir(tmp_path):
    this_workdir = Workdir(tmp_path)
    this_workdir.write_file("behave.ini", BEHAVE_INI_TEXT)
    this_workdir.write_file("features/steps/steps.py", STEPS_TEXT)
    for relpath, text in FEATURE_FILES.items():
        this_workdir.write_file(relpath, text)
    return this_workdir
