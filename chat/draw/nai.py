"""Compatibility import for :mod:`tools.draw.nai`."""

try:
    from ...tools.draw.nai import *  # noqa: F401,F403
except ImportError:
    from tools.draw.nai import *  # noqa: F401,F403
