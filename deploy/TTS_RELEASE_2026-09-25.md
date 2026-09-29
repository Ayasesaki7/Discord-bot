# ATRI 日语 TTS

已部署官方 API 方案，用户确认替代网页自动化，不安装浏览器/不导入 Cookie。
新增普通 Agent 工具 `send_voice`，固定音色 `5303afc44f544728b9a45000bf632c9e`，固定免费型号 `s2.1-pro-free`。
宿主用当前聊天模型翻译短句，然后把 MP3 从内存直接回复到当前频道；不进入语音频道，不动音乐队列。
服务器设置新增本服 TTS 开关，遵守现有聊天白名单/频道限制。Key 存于 root:root 0600 私有环境文件
`/etc/atri-bot/fish.env`，由 `40-fish-tts.conf` 注入服务；没有更改原 `.env` 或原 API 配置。

验证：

- 本地 636 项 Python 测试通过（Windows 下跳过 1 项 POSIX 权限测试）。
- 服务器隔离目录 615 项 Python、16 项 Node 测试通过。
- 指定 Key/音色/免费型号真实合成成功，44.1 kHz 单声道 MP3。
- 完整链路测试：中文测试句 → 当前模型的日语翻译 → Fish 语音 → ffmpeg 完整解码通过。
- 本轮测试未向任何实际 Discord 频道发送测试附件；Discord 文件发送由 mock 交互测试覆盖。
- 服务重启后 PID 500864，active/running，NRestarts=0。旧频道 DSH 进程随服务重启退出，新进程重新加载工具。

回滚代码/测试记录在 `/root/atri-tts-20260925`；首次版本不修改记忆库 schema。
生成请求记录只保存 Discord ID、状态和时间，失败/不确定发送也禁止同一用户消息自动重试。
默认每用户 60 秒、全局至少 10 秒、并发 1；每服 100 次/滚动 24 小时，全局 300 次。
免费 Fish 合成有公平使用限制；翻译仍使用现有聊天 API，按该上游计费。不会回退到收费 TTS 型号。

参考官方资料（2026-09-25 核实）：

- https://docs.fish.audio/developer-guide/models-pricing/pricing-and-rate-limits
- https://docs.fish.audio/developer-guide/models-pricing/models-overview
- https://docs.fish.audio/features/text-to-speech

本服开关见 `/服务器设置`；使用说明见 `chat/TTS.md`。
