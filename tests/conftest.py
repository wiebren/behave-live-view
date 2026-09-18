# -*- coding: UTF-8 -*-
"""
Test support: Register the format name "live", like a user does it with:

.. code-block:: ini

    # -- FILE: behave.ini
    [behave.formatters]
    live = behave_live_view:LiveFormatter
"""

from behave.formatter import _registry as formatter_registry
from behave_live_view import LiveFormatter

formatter_registry.register_as("live", LiveFormatter)
