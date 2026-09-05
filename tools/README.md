# Capability tools

- `draw/`: the live NovelAI drawing capability.
- `fortune/`: the live daily-fortune capability.
- `agent/`: staging area for new owner-reviewed Agent tools.
- `lux/`: external downloader binary data; not Agent-readable or writable.

Bot core, privacy boundaries, Discord routing, and the maintenance host remain
outside this directory and are read-only to the maintenance Agent.

Missing capabilities follow a plugin-first workflow. The maintenance Agent can
search npm, quarantine-download a candidate, and activate audited low-privilege
official DSH plugins. Writing a new executable tool is allowed only after the
owner explicitly confirms source authoring in that maintenance request.

The independent maintenance API is configured through the owner-only Discord
panel `/开发agent设置`; no local desktop editor is required.
