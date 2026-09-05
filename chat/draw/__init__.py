"""Compatibility import for the drawing tool moved to :mod:`tools.draw`."""

try:
    from ...tools.draw import AtriDrawAgent
except ImportError:  # Top-level compatibility for local tests and scripts.
    from tools.draw import AtriDrawAgent

__all__ = ["AtriDrawAgent"]
