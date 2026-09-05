# ATRI Discord Bot

一个以 Discord 为运行入口的多功能 Bot，包含音乐播放、AI 聊天、DSH 持久会话、图片生成、Bilibili/抖音解析、运势、身份组领取、黑名单与管理工具。

> 本仓库只包含可公开的源码和示例配置。Token、Cookie、Discord 用户/服务器/频道/身份组 ID、聊天记录、日志和运行时数据库都应只保存在部署机器上。

## 环境要求

- Windows：Python 3.12；启动脚本可以通过 `winget` 自动安装。
- Linux：建议 Python 3.12，并安装 `python3-venv`。
- 音乐播放：需要能够在 `PATH` 中找到 FFmpeg。
- DSH Agent（可选）：Node.js 22.19.0 或更高版本。
- 一个 Discord Bot Token。请在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用和 Bot。

Discord Bot 至少需要 `bot` 和 `applications.commands` scopes。请按启用的功能授予发送消息、嵌入链接、读取消息历史、连接/在语音频道发言等权限；封禁和身份组管理权限只在使用对应模块时需要。

## 1. 下载项目

```bash
git clone https://github.com/Ayasesaki7/Discord-bot.git
cd Discord-bot
```

## 2. 创建本机配置

不要直接修改 `.env.example`。复制一份名为 `.env` 的本机配置：

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

Linux：

```bash
cp .env.example .env
chmod 600 .env
```

最小配置：

```dotenv
DISCORD_TOKEN=你的_Discord_Bot_Token
ATRI_OWNER_DISCORD_ID=你的_Discord_用户_ID
```

启用 AI 聊天时再填写兼容 OpenAI API 的连接信息：

```dotenv
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=你的_API_Key
OPENAI_MODEL=你的模型名称
ATRI_CHAT_GUILD_WHITELIST_IDS=允许聊天的服务器ID
```

多个服务器 ID 使用英文逗号分隔。其他配置项的用途和默认值见 [.env.example](.env.example)。QQ 音乐、Bilibili 和抖音 Cookie 建议分别放在 `config/credentials/` 下；该目录除说明文件外不会被 Git 跟踪。

## 3. 安装并启动

### Windows 快速部署

双击 `一键安装并启动.bat`，或者在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Install
```

以后启动不需要重复安装依赖：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

前台调试：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Foreground
```

停止和查看日志：

```powershell
.\stop.ps1
.\logs.ps1
```

### Linux 部署

先安装 Python、venv 和 FFmpeg。以 Debian/Ubuntu 为例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg
chmod +x start.sh stop.sh logs.sh
./start.sh --install
```

停止和查看日志：

```bash
./stop.sh
./logs.sh
```

如果启用 DSH，Linux 还必须在 `.env` 中指定仓库外的绝对会话目录，例如：

```dotenv
ATRI_AGENT_SESSION_ROOT=/var/lib/atri/agent-sessions
```

请预先创建该目录，并确保运行 Bot 的系统用户拥有读写权限。

## 4. 启用 DSH Agent（可选）

安装 Node.js 22.19.0 或更高版本，然后安装锁定的 DSH 依赖。

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\agent_runtime\dsh\install.ps1
```

Linux：

```bash
cd agent_runtime/dsh
npm ci
cd ../..
```

在 `.env` 中启用普通 Agent：

```dotenv
ATRI_AGENT_V2_ENABLED=true
ATRI_AGENT_V2_OWNER_ONLY=false
```

DSH 默认复用 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `OPENAI_MODEL`。独立的维护 Agent 可另外启用 `ATRI_AGENT_CODE_ENABLED`，并通过 Discord 的 `/开发agent设置` 配置专用 API。更详细的架构和安全边界见 [agent_runtime/dsh/README.md](agent_runtime/dsh/README.md)。

## 如何更换 Discord 主人 ID

这里需要填写的是你的 Discord **用户 ID**，不是用户名、服务器 ID，也不是 Bot 应用 ID。

1. 打开 Discord 的“用户设置 → 高级”，启用“开发者模式”。
2. 右键自己的头像或名字，选择“复制用户 ID”。
3. 打开部署机器上的 `.env`，修改：

   ```dotenv
   ATRI_OWNER_DISCORD_ID=复制到的纯数字用户ID
   ```

4. 如果 QQ 音乐 Cookie 到期提醒也要发送给新主人，同时修改：

   ```dotenv
   QQMUSIC_COOKIE_NOTIFY_USER_ID=同一个纯数字用户ID
   ```

5. 重启 Bot。所有者权限在启动时读取，不重启不会完整生效。

Windows：

```powershell
.\stop.ps1
.\start.ps1
```

Linux：

```bash
./stop.sh
./start.sh
```

## DSH 日志和隐私

- Windows 默认将 DSH 持久会话写到 `%LOCALAPPDATA%\ATRI\agent-sessions`，位于仓库外。
- `bot.log`、`bot.err.log`、`*.jsonl`、常见 DSH 会话目录、`.env`、本机密钥、Cookie、数据库和缓存均已写入 `.gitignore`。
- `agent_runtime/dsh/` 中会上传的是运行代码、配置模板和锁文件；不会上传 `node_modules` 或实际聊天会话。
- 保持 `ATRI_CHAT_LOG_SENSITIVE_CONTENT=false`，错误日志只记录必要的脱敏元数据。
- 不建议把 `ATRI_AGENT_SESSION_ROOT` 指向仓库内部；Linux 请使用 `/var/lib/...`、`/srv/...` 或用户数据目录。

提交前可以检查：

```bash
git status --short --ignored
git check-ignore .env bot.log
```

仓库提供 `.gitleaks.toml`，发布前可使用 Gitleaks 再做一次凭据扫描。即使文件已被忽略，也不要使用 `git add -f` 强制提交 Token、Cookie 或聊天记录。

## 常见问题

### Bot 启动后没有斜杠命令

确认邀请 Bot 时包含 `applications.commands` scope，并等待 Discord 完成全局命令同步。设置 `TEST_GUILD_ID` 可以将命令同步到测试服务器，便于开发时快速生效。

### 音乐无法播放

确认 FFmpeg 已安装并能通过 `ffmpeg -version` 找到，同时检查 Bot 是否拥有连接和在语音频道发言的权限。

### DSH 提示未安装或找不到 Node.js

确认 `node --version` 不低于 22.19.0，并重新执行 `agent_runtime/dsh/install.ps1` 或在该目录运行 `npm ci`。

## 安全提醒

如果 Token 曾经进入公开 Git 历史，仅从最新提交删除文件并不安全。应立即在对应平台撤销/轮换 Token，并使用干净历史重新发布。
