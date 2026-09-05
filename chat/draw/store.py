"""Compatibility import for :mod:`tools.draw.store`."""

try:
    from ...tools.draw.store import *  # noqa: F401,F403
except ImportError:
    from tools.draw.store import *  # noqa: F401,F403
