"""Compatibility import for :mod:`tools.fortune.cog`."""

try:
    from ..tools.fortune.cog import *  # noqa: F401,F403
except ImportError:
    from tools.fortune.cog import *  # noqa: F401,F403
