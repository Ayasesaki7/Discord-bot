"""Compatibility import for :mod:`tools.draw.agent`."""

try:
    from ...tools.draw.agent import *  # noqa: F401,F403
except ImportError:
    from tools.draw.agent import *  # noqa: F401,F403
