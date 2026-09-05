"""Compatibility import for :mod:`tools.draw.search`."""

try:
    from ...tools.draw.search import *  # noqa: F401,F403
except ImportError:
    from tools.draw.search import *  # noqa: F401,F403
