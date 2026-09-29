# 频道关键记忆上线记录 · 2026-09-25

## 已部署

- SQLite 保存可追溯的关键陈述，384 维本地多语言向量＋中文词面混合检索。
- 当前消息触发的 Agent 自行判断是否保存，不新增逐句云端提取请求，也不扫描旧聊天建库。
- 宿主绑定 guild/channel/author/source；跨频道读取、目标 ID 覆盖、跨成员覆写均拒绝。
- 新增 `/频道记忆`：查看、搜索、删除、清空、开关；清空需要确认，普通成员只删自己的条目。
- 源消息变化和管理操作使旧请求失效；BOT 流式编辑不影响真人的在途写入。
- 压缩/恢复不清空关键记忆；开发者 `/重置对话` 同时清空当前频道关键记忆。
- 向量异常降级词面检索，记忆库初始化异常不会阻止普通聊天加载。

规则、限制及部署配置见 `chat/KEY_MEMORY.md`。

## 验证

- Windows 本地：585 项 Python 测试、16 项 Node 测试通过。
- 服务器隔离环境：564 项 Python 测试、16 项 Node 测试通过。本地有 21 项额外已有测试。
- 记忆相关 Python 测试 31 项，覆盖宿主、数据库、权限、并发失效及子进程故障清理。
- 实际离线向量验证通过：384 维，语义检索“有什么忌口”召回“不吃辣”，其他频道/服务器为空。
  首次加载约 1.46 秒，热检索约 0.01 秒（单次样例，非并发性能保证）。测试数据为临时虚构内容，
  未写入生产记忆库。尚未把真实用户的模型自主记忆选择当作已验证保证。
- 服务重启后 READY 正常、24 个全局指令已同步、自动重启次数 0。

## 环境与回退

- fastembed 0.7.4 独立环境：`/opt/atri-runtime/memory-venv`；BOT 主 venv 不增加此依赖。
- 模型缓存：`/opt/atri-runtime/memory-models`；运行时禁用 Hugging Face 联网，不传递 API/Discord 凭据。
- 服务配置：`/etc/systemd/system/atri-bot.service.d/memory.conf`，未修改 `.env` 或其他服务。
- 数据：`/opt/atri-bot/chat/agent/data/key_memories.sqlite3`，atri 所有、0600，父目录 0700。
- 上线前已核验服务器原文件散列，确认没有覆盖同步后的新增修改。
- 本次服务器备份：`/root/atri-key-memory-20260925-02eb5afe`，含四个原文件的 source-before.tar、
  本次 payload.tar、完整回归结果及 embedding 依赖 lock。
- 本地备份：`G:/code/atri-sync-backups/channel-memory-20260925-02eb5afea5914ce39c561e4fffc59078`。
- 若回退：停服务，恢复备份中的四个原文件，停用本次 memory.conf，再 daemon-reload/start；
  新的记忆数据库保留，不以回退代码为由删除。新文件不再被旧入口导入，可以保留供再次上线。
- 旧会话及凭据未迁移、重置或清空。已有缺少 Bilibili Cookie 的启动警告不属于本次变更。
