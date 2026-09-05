# Protected service credentials

This directory stores the live QQ Music, Bilibili, and Douyin cookie files.
Everything except this README is Git-ignored.

The owner-only maintenance Agent may replace a credential through the dedicated
`credential_update` tool. That tool has no read operation, returns no credential
content, and requests an in-process hot reload after an atomic file update.
General project read, search, edit, and create tools cannot access this
directory. This README is for local developers, not Agent context.

`agent_code_api.json` contains the independent maintenance-model connection.
The owner-only Discord command `/开发agent设置` writes it through an
ephemeral panel. The Bot applies the new maintenance DSH runtime immediately;
the normal chat runtime and active conversations are not restarted.

`web_search_api.json` contains the separate search-capable model connection.
The owner-only Discord command `/联网搜索设置` writes it through an ephemeral
panel and pulls the model list from the configured `/models` endpoint. Changes
apply to new `web_search` tool calls immediately without restarting the Bot.
