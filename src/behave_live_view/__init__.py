# -*- coding: UTF-8 -*-
"""
Interactive live view of a `behave`_ test run: compact status lines for
features/rules/scenarios/steps (pending, running, passed, failed) that can
be expanded, filtered, opened in the editor and run again.

.. code-block:: ini

    # -- FILE: behave.ini
    [behave]
    runner = behave_live_view:LiveRunner

* :class:`LiveRunner`: Shows the interactive view (needs a terminal).
* :class:`LiveFormatter`: The "live" formatter. Without the interactive view,
  it writes plain, append-only status lines (CI, pipes, output files).

.. _behave: https://github.com/behave/behave
"""

from behave_live_view.formatter import LiveFormatter
from behave_live_view.runner import LiveRunner

__all__ = ["LiveFormatter", "LiveRunner"]
__version__ = "0.1.0"
