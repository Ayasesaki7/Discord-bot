"""Compatibility import for :mod:`tools.fortune.service`."""

try:
    from ..tools.fortune.service import *  # noqa: F401,F403
except ImportError:
    from tools.fortune.service import *  # noqa: F401,F403
