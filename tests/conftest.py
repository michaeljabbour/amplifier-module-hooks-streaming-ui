"""
Pytest configuration for module tests.

Behavioral tests use inheritance from amplifier-core base classes.
See tests/test_behavioral.py for the inherited tests.

The amplifier-core pytest plugin provides fixtures automatically:
- module_path: Detected path to this module
- module_type: Detected type (provider, tool, hook, etc.)
- provider_module, tool_module, etc.: Mounted module instances
"""

import pytest

from amplifier_module_hooks_streaming_ui.live_footer import _reset_singleton


@pytest.fixture(autouse=True)
def _clean_footer_singleton():
    """Reset the singleton LiveFooter between every test."""
    _reset_singleton()
    yield
    _reset_singleton()
