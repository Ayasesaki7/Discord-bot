"""Compatibility import for :mod:`tools.draw.models`."""

try:
    from ...tools.draw.models import *  # noqa: F401,F403
except ImportError:
    from tools.draw.models import *  # noqa: F401,F403
