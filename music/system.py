from __future__ import annotations

import asyncio
import os
import platform
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


DEFAULT_OWNER_DISCORD_ID = 0
PROC_DIR = Path('/proc')
SELF_STATUS_PATH = PROC_DIR / 'self' / 'status'
MEMINFO_PATH = PROC_DIR / 'meminfo'
UPTIME_PATH = PROC_DIR / 'uptime'
BOOT_ID_PATH = PROC_DIR / 'sys' / 'kernel' / 'random' / 'boot_id'


def _read_owner_discord_id() -> int:
    raw = os.getenv('ATRI_OWNER_DISCORD_ID', '').strip()
    if not raw:
        return DEFAULT_OWNER_DISCORD_ID
    try:
        owner_id = int(raw)
        return owner_id if owner_id > 0 else DEFAULT_OWNER_DISCORD_ID
    except ValueError:
        return DEFAULT_OWNER_DISCORD_ID


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except OSError:
        return None


def _read_linux_status_map() -> dict[str, str]:
    raw = _read_text(SELF_STATUS_PATH)
    if not raw:
        return {}

    status: dict[str, str] = {}
    for line in raw.splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        status[key.strip()] = value.strip()
    return status


def _parse_kb_field(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split()
    if not parts:
        return None
    try:
        amount = int(parts[0])
    except ValueError:
        return None
    return amount * 1024


def _format_bytes(num_bytes: int | None) -> str:
    if num_bytes is None:
        return '未知'

    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(num_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1

    precision = 0 if unit_index <= 1 else 2
    return f'{value:.{precision}f} {units[unit_index]}'


def _format_duration(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f'{days}天')
    if hours:
        parts.append(f'{hours}小时')
    if minutes:
        parts.append(f'{minutes}分钟')
    if secs or not parts:
        parts.append(f'{secs}秒')
    return ' '.join(parts)


def _format_datetime(dt: datetime) -> str:
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def _format_percent(value: float | None) -> str:
    if value is None:
        return '未知'
    return f'{value:.1f}%'


def _read_meminfo() -> dict[str, int]:
    raw = _read_text(MEMINFO_PATH)
    if not raw:
        return {}

    meminfo: dict[str, int] = {}
    for line in raw.splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        parsed = _parse_kb_field(value)
        if parsed is not None:
            meminfo[key.strip()] = parsed
    return meminfo


def _read_system_uptime_seconds() -> float | None:
    raw = _read_text(UPTIME_PATH)
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (IndexError, ValueError):
        return None


async def _sample_cpu_percent() -> float | None:
    if not PROC_DIR.exists():
        return None

    def read_cpu_times() -> tuple[int, int] | None:
        raw = _read_text(PROC_DIR / 'stat')
        if not raw:
            return None
        first_line = raw.splitlines()[0]
        parts = first_line.split()
        if len(parts) < 5 or parts[0] != 'cpu':
            return None
        try:
            values = [int(part) for part in parts[1:]]
        except ValueError:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total, idle

    first = read_cpu_times()
    if first is None:
        return None
    await asyncio.sleep(0.2)
    second = read_cpu_times()
    if second is None:
        return None

    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))


@dataclass(slots=True)
class SystemSnapshot:
    operating_system: str
    system_uptime: str
    process_uptime: str
    process_id: int
    cpu_usage: str
    process_memory: str
    system_memory: str
    working_set: str
    thread_count: str
    python_version: str
    discord_py_version: str
    boot_marker: str


class SystemAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.owner_user_id = _read_owner_discord_id()
        self.started_at = datetime.now(timezone.utc)
        self.started_monotonic = time.monotonic()
        self._shutdown_task: asyncio.Task[None] | None = None

    def _is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_user_id

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if self._is_owner(interaction.user.id):
            return True

        if not interaction.response.is_done():
            await interaction.response.send_message(
                '这个命令只允许开发者使用。',
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                '这个命令只允许开发者使用。',
                ephemeral=True,
            )
        return False

    async def _build_snapshot(self) -> SystemSnapshot:
        status_map = _read_linux_status_map()
        meminfo = _read_meminfo()
        cpu_usage = await _sample_cpu_percent()

        vm_rss = _parse_kb_field(status_map.get('VmRSS'))
        vm_hwm = _parse_kb_field(status_map.get('VmHWM'))
        thread_count = status_map.get('Threads', str(os.cpu_count() or '未知'))

        total_memory = meminfo.get('MemTotal')
        available_memory = meminfo.get('MemAvailable')
        used_memory = None
        memory_percent = None
        if total_memory is not None and available_memory is not None and total_memory > 0:
            used_memory = total_memory - available_memory
            memory_percent = (used_memory / total_memory) * 100

        if used_memory is None or total_memory is None or memory_percent is None:
            system_memory = '未知'
        else:
            system_memory = (
                f'{_format_bytes(used_memory)} / {_format_bytes(total_memory)} '
                f'({_format_percent(memory_percent)})'
            )

        boot_marker = '未知'
        boot_id = _read_text(BOOT_ID_PATH)
        if boot_id:
            boot_marker = boot_id.strip()[:8]

        system_uptime_seconds = _read_system_uptime_seconds()
        return SystemSnapshot(
            operating_system=f'{platform.system()} {platform.release()} ({platform.machine()})',
            system_uptime=_format_duration(system_uptime_seconds or 0),
            process_uptime=_format_duration(time.monotonic() - self.started_monotonic),
            process_id=os.getpid(),
            cpu_usage=_format_percent(cpu_usage),
            process_memory=_format_bytes(vm_rss),
            system_memory=system_memory,
            working_set=_format_bytes(vm_hwm or vm_rss),
            thread_count=thread_count,
            python_version=platform.python_version(),
            discord_py_version=getattr(discord, '__version__', '未知'),
            boot_marker=boot_marker,
        )

    def _build_status_embed(self, snapshot: SystemSnapshot) -> discord.Embed:
        embed = discord.Embed(
            title='系统状态',
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name='操作系统', value=snapshot.operating_system, inline=True)
        embed.add_field(name='系统运行时间', value=snapshot.system_uptime, inline=True)
        embed.add_field(name='进程 ID', value=str(snapshot.process_id), inline=True)
        embed.add_field(name='Bot 运行时间', value=snapshot.process_uptime, inline=True)
        embed.add_field(name='CPU 使用率', value=snapshot.cpu_usage, inline=True)
        embed.add_field(name='进程内存', value=snapshot.process_memory, inline=True)
        embed.add_field(name='总内存使用', value=snapshot.system_memory, inline=True)
        embed.add_field(name='工作集峰值', value=snapshot.working_set, inline=True)
        embed.add_field(name='线程数', value=snapshot.thread_count, inline=True)
        embed.add_field(name='Python 版本', value=snapshot.python_version, inline=True)
        embed.add_field(name='discord.py', value=snapshot.discord_py_version, inline=True)
        embed.add_field(name='系统启动标识', value=snapshot.boot_marker, inline=True)
        embed.set_footer(text=f'Bot 启动时间: {_format_datetime(self.started_at)}')
        return embed

    async def _shutdown_after_delay(self, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self.bot.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f'[WARN] Failed to shut down bot cleanly: {exc}')

    @app_commands.command(name='status', description='查看当前 Bot 所在服务器的系统状态')
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = await self._build_snapshot()
        await interaction.followup.send(
            embed=self._build_status_embed(snapshot),
            ephemeral=True,
        )

    @app_commands.command(name='关闭程序', description='优雅关闭当前 Bot 进程')
    async def shutdown_bot(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return

        if self._shutdown_task is not None and not self._shutdown_task.done():
            await interaction.response.send_message(
                '关闭流程已经在进行了。',
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            '正在关闭 Bot，连接会在 1 秒内断开。',
            ephemeral=True,
        )
        self._shutdown_task = asyncio.create_task(self._shutdown_after_delay(1.0))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SystemAdmin(bot))
