"""Compatibility import for :mod:`tools.fortune.storage`."""

try:
    from ..tools.fortune.storage import *  # noqa: F401,F403
except ImportError:
    from tools.fortune.storage import *  # noqa: F401,F403
