# Agent configuration zone

The maintenance Agent may create and edit declarative configuration here.
Keep credentials and private conversation data out of this directory.

`tool_guidance.md` is the hot-loaded extension capability supplement. When a
maintenance task adds or materially changes a callable tool, it must update
that file in the same task. The read-only core validates and appends it to
normal Agent turns; malformed updates fall back to the last valid content.
