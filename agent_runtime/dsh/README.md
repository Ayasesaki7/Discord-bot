# ATRI DeepSeek Harness runtime

这个目录是 Discord 聊天模块的 DSH 核心，版本锁定在 `0.1.0-rc.6`。Windows 首次安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\agent_runtime\dsh\install.ps1
```

`.env` 中的开关：

```dotenv
ATRI_AGENT_V2_ENABLED=true
ATRI_AGENT_V2_OWNER_ONLY=false
ATRI_AGENT_CODE_ENABLED=true
ATRI_AGENT_CODE_BASE_URL=https://your-maintenance-api.example/v1
ATRI_AGENT_CODE_API_KEY=...
ATRI_AGENT_CODE_MODEL=your-maintenance-model
```

所有者可以在 Discord 使用 `/开发agent设置` 打开 ephemeral 面板，填写独立维护 API。Model 默认留空；提交后 Bot 会请求该上游的 `/models`，在面板中以可分页下拉列表选择，也可手动填 Model 作为不支持 `/models` 的兼容兜底。未启用时直接调用 `/改进自己` 也会自动弹出编辑 Modal。配置保存到被 Git 忽略的 `config/credentials/agent_code_api.json`；Bot 会等当前维护任务结束后原子替换维护 DSH 运行时，不重启普通聊天进程。面板配置优先于上面的历史 `.env` 变量。

- 普通 Agent：每个 Discord 服务器一个独立 DSH 进程，每个频道一个独立持久会话；内置 `draw_image` 和 `daily_fortune`，并从 `native-plugins.json` 加载已审计的 DSH 原生工具。
- 聊天白名单服务器中，没有 @/Reply ATRI 的普通频道发言也会按频道 FIFO 持久写入同频道上下文，包括其他 Discord BOT/APP 的文字与 Embed 文本。旁听写入不调用模型、不发送回复、不添加任务反应；ATRI 自己的回复由 DSH 原生 assistant 记录保存，不重复旁听。每次真正互动前，宿主还会按频道持久水位从 Discord 追赶最近漏记的普通发言，补齐 BOT 离线、重启或更新期间的缺口。这些记录与互动消息一起参与 DSH 原生窗口计量和自动压缩。
- 开发 Agent：所有者既可使用 `/改进自己`，也可在普通聊天中自然提出维护要求，由聊天 Agent 委派给独立维护 Agent。维护过程使用另一套 API、DSH 会话和存储，没有任意 Shell、跨频道发送、Web 或子 Agent 权限。它能反复读取非敏感项目源码，但宿主只允许写入 `config/agent`、`tools/agent`、`tools/draw` 与 `tools/fortune`，核心代码始终只读。
- 插件优先：维护 Agent 在缺少能力时先搜索 npm，下载到 `plugin_staging` 隔离区，校验 npm SHA-512 完整性并检查安装脚本。只有已审计的官方低权限 `@deepseek-ai/dsh-*` 包才能写入 `native-plugins.json`；Shell、文件系统、任务调度、Cordis 控制、Skill 和子 Agent 类型不会自动激活。
- 已启用 `@deepseek-ai/dsh-tool-todo`，ATRI 可通过 `todo_write` 维护复杂任务的待做、进行中和已完成层次。
- 普通聊天的系统提示词包含 ATRI 能力清单。被问到“你能做什么/有什么工具”时，它根据该清单与当轮实际可见的 DSH tool schema 回答，不会为此读取项目文件。实际 schema 始终优先，因此后续激活的原生工具也可被正确识别。
- 所有者可让 Agent 调用 `runtime_system_info`、`runtime_read_log` 与 `runtime_command` 诊断部署环境。Agent 必须先识别操作系统，再自行从宿主公布的只读探针中选择命令；日志只能读取固定的 `bot.log` / `bot.err.log` 尾部并经过凭据脱敏。运行时工具没有任意 Shell、参数、脚本求值、管道、重定向、环境变量转储、网络客户端、进程控制或任意路径能力，单次命令还受短超时和输出上限约束。
- 一个维护任务可以连续进行多次模型推理和多次工具调用；`maxParallelToolCalls: 1` 只禁止并行写入，不限制顺序调用次数。
- 维护 Agent 不会把整个仓库塞入提示词：先用 `project_list` 看有界目录，再用 `project_search` 定位符号，最后用 `project_read` 分段读取（单次最多 400 行）。每个维护任务使用新的代码会话，源码和工具结果不会进入普通聊天会话。
- 两套 DSH 都启用了自动上下文压缩：超大工具结果先做头尾剪枝，达到模型窗口压力阈值后再生成结构化检查点；维护任务的检查点仍只存在于独立代码会话和独立 API 中。
- 普通聊天把“上游模型能力”和“ATRI 记忆目标”分开：`ATRI_DSH_PROVIDER_CONTEXT_WINDOW` 默认 `1048576`，只供 pi-ai 判断真正的模型溢出；`ATRI_DSH_CONTEXT_WINDOW` 默认 `140000`，控制 ATRI 希望维持的会话规模。DSH 会动态换算压缩比例，仍在记忆目标的 `78%`（约 `109200` tokens）自动压缩，摘要最多生成 `30000` tokens。`/频道上下文` 可对当前服务器/频道热更新独立的楼层数、`8000～80000` tokens 软预算以及“压缩/丢弃最旧”策略，配置保存在 Git 忽略的 `chat/agent/data/channel_contexts.json`。
- 会话换代不只继承最后一份成功摘要：宿主还会从同一频道旧 JSONL 中提取该摘要之后的用户/助手记录和工具完成标记，过滤当轮宿主元数据并限制在 `40000` 字符内，再用 Discord 近期消息补漏。压缩 API 失败时不会出现“旧摘要到近期消息之间”的完全空档；旧 JSONL 始终保留且不会跨频道读取。
- `/导入频道上下文 楼层:300` 会在执行命令的当前频道读取最近 300 条可见消息并追加到该频道的 DSH 会话；超出软预算时按频道策略压缩旧记录或舍弃最旧记录。命令只允许所有者在聊天白名单服务器中使用，导入结果仅以 ephemeral 消息显示。
- 文字与视觉消息统一进入同一个 DSH 频道会话。附件、静态/动态贴纸、GIF/APNG/WebP 等动画图片以及静态/动态自定义表情仍复用 Discord 宿主的下载、格式归一化、压缩和多帧预览逻辑；视觉模型只生成观察笔记，图片 Base64 不写进 DSH 长期会话。
- `discord_visual_inspect` 可在需要时另行读取当前服务器成员头像、当前/指定消息附件、表情或贴纸。它是通用视觉工具，可用于描述、OCR、比较、风格/外观特征提取；只有用户确实要画图时，Agent 才把提取结果交给 `draw_image`。原始图像字节只进入当次视觉 API 请求。
- `discord_manage` 已覆盖服务器表情、应用表情和服务器贴纸的创建/修改/删除；创建素材可来自当前附件、指定消息附件、成员头像、已有表情/贴纸或公网素材 URL。直链图片/GIF 可直接使用，普通网页也会尝试读取 Open Graph/Twitter 图片元数据。外链仅下载到内存，且会拒绝本机/内网/保留地址、内网重定向、超时、非图像与超限文件。超过 Discord 限制时会在内存中自动逐级缩小、降色，动画还会抽帧并合并被跳过帧的时长；表情保留为 GIF，贴纸转为 APNG，上传完不会留下本地素材文件。这些写操作仅开放给所有者。删除、清理、踢出和封禁等破坏性操作会把 `atri_maozhua` 直接加到所有者当前的指令消息上，不另发确认消息；宿主同时校验该指令消息 ID、表情 ID 和点击者 Discord ID，只有配置的所有者本人在超时前点击才会执行，无需再发文字“确认删除”。Discord Reply 的目标频道/消息 ID 由宿主每轮直接注入，Agent 必须直接使用，不得再要求用户粘贴消息链接。
- 所有者可用 `/清理缓存` 先预览、再确认清理，也可让 Agent 先调用 `cleanup_preview` 后再执行 `cleanup_generated_files`，后者会进入猫爪 reaction 确认。清理器没有任意路径参数，只识别过期的 `music_cache` 音频产物、`bilibili_cache/bilibili_*`、`douyin_cache/douyin_*`临时任务目录以及 ATRI/F2 轮转日志。正在下载/播放的文件、当前 `bot.log`/`bot.err.log`、配置、凭据、数据库、Agent 会话与用户文件都不在删除范围。
- 普通聊天回复统一追加 `Time / In / Out` 小尾巴；视觉轮次会合计视觉桥接与 DSH 的 Token 用量。DSH 失败时不会再悄悄切换回另一套聊天记忆。
- 会话密钥：不配置 `ATRI_AGENT_SESSION_SECRET` 时，会自动生成到 `%LOCALAPPDATA%\ATRI\agent-session.key`，不会写入仓库。
- 会话数据：默认位于 `%LOCALAPPDATA%\ATRI\agent-sessions`。`/重置对话` 会轮换当前频道的持久会话 ID。
- JSON-RPC 入口使用项目内 `atri-sdk-jsonrpc-server` 恢复适配器：进程重启后，已存在的频道 sessionId 会调用 DSH 原生 `agents.resume()`；新频道才调用 `agents.create()`，避免官方 demo 对已有 JSONL 日志重复创建导致失忆或回退。
- DSH stdout 是逐行 JSON-RPC，Python 桥默认把单帧安全上限设为 8 MiB（`ATRI_DSH_STDIO_LIMIT_BYTES` 可在 256 KiB～32 MiB 间覆盖），避免 asyncio 默认 64 KiB 在长工具 schema/事件上触发 `LimitOverrunError`；真正超过安全上限时会受控回收该 DSH 进程，而不是留下未处理的 reader task 异常。

代码模式只会直接修改专用配置区和工具区，不会重启 Bot、提交 Git 或读取 `.env`、Cookie、Token、日志和运行数据。`tools/draw` 与 `tools/fortune` 是已接入能力；如果找不到可复用插件，创建新的 `.py` / `.js` 工具源码必须由所有者在当次维护请求中明确确认。修改后仍需检查 Git diff、测试并手动重启。

QQ 音乐、B 站和抖音的 Cookie 统一保存在 `config/credentials`。所有凭据文件均被 Git 忽略；维护 Agent 只能通过 `credential_update` 覆盖所有者在当次任务中提供的新值，无法读回旧值或通过通用项目工具搜索该目录。写入使用原子替换，随后请求对应 Cog 热加载。

聊天请求在每个 Discord 频道内严格 FIFO：后到的消息会先获得 `ATRI_dangji` 反应，等同频道前一条任务完整结束后再开始 typing 和模型调用。不同频道互不阻塞。

消息仍会发给 `.env` 中配置的模型服务；画图提示词会发给 NovelAI。若模型服务不承诺零保留，请不要把“本地隔离”理解成“第三方永远看不到消息”。
