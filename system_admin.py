from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from tools.cleanup import GeneratedArtifactCleaner, collect_cleanup_protection


DEFAULT_OWNER_DISCORD_ID = 0
PROC_DIR = Path('/proc')
SELF_STATUS_PATH = PROC_DIR / 'self' / 'status'
MEMINFO_PATH = PROC_DIR / 'meminfo'
UPTIME_PATH = PROC_DIR / 'uptime'
BOOT_ID_PATH = PROC_DIR / 'sys' / 'kernel' / 'random' / 'boot_id'


class FILETIME(ctypes.Structure):
    _fields_ = [
        ('dwLowDateTime', ctypes.c_ulong),
        ('dwHighDateTime', ctypes.c_ulong),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', ctypes.c_ulong),
        ('dwMemoryLoad', ctypes.c_ulong),
        ('ullTotalPhys', ctypes.c_ulonglong),
        ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong),
        ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong),
        ('ullAvailVirtual', ctypes.c_ulonglong),
        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('cb', ctypes.c_ulong),
        ('PageFaultCount', ctypes.c_ulong),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', ctypes.c_ulong),
        ('cntUsage', ctypes.c_ulong),
        ('th32ThreadID', ctypes.c_ulong),
        ('th32OwnerProcessID', ctypes.c_ulong),
        ('tpBasePri', ctypes.c_long),
        ('tpDeltaPri', ctypes.c_long),
        ('dwFlags', ctypes.c_ulong),
    ]


def _read_owner_discord_id() -> int:
    raw = os.getenv('ATRI_OWNER_DISCORD_ID', '').strip()
    if not raw:
        return DEFAULT_OWNER_DISCORD_ID
    try:
        owner_id = int(raw)
        return owner_id if owner_id > 0 else DEFAULT_OWNER_DISCORD_ID
    except ValueError:
        return DEFAULT_OWNER_DISCORD_ID


def _read_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, '').strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


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


def _filetime_to_int(filetime: FILETIME) -> int:
    return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime


def _read_windows_cpu_times() -> tuple[int, int] | None:
    if platform.system() != 'Windows':
        return None

    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None

    idle_value = _filetime_to_int(idle)
    kernel_value = _filetime_to_int(kernel)
    user_value = _filetime_to_int(user)
    total_value = kernel_value + user_value
    return total_value, idle_value


async def _sample_cpu_percent() -> float | None:
    if PROC_DIR.exists():
        def read_linux_cpu_times() -> tuple[int, int] | None:
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

        first = read_linux_cpu_times()
        if first is None:
            return None
        await asyncio.sleep(0.2)
        second = read_linux_cpu_times()
        if second is None:
            return None

        total_delta = second[0] - first[0]
        idle_delta = second[1] - first[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))

    first = _read_windows_cpu_times()
    if first is None:
        return None
    await asyncio.sleep(0.2)
    second = _read_windows_cpu_times()
    if second is None:
        return None

    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))


def _read_windows_memory_status() -> MEMORYSTATUSEX | None:
    if platform.system() != 'Windows':
        return None

    memory_status = MEMORYSTATUSEX()
    memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
        return None
    return memory_status


def _read_windows_process_memory() -> tuple[int | None, int | None]:
    if platform.system() != 'Windows':
        return None, None

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    process_handle = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(
        process_handle,
        ctypes.byref(counters),
        counters.cb,
    ):
        return None, None
    return counters.WorkingSetSize, counters.PeakWorkingSetSize


def _read_windows_thread_count() -> int | None:
    if platform.system() != 'Windows':
        return None

    TH32CS_SNAPTHREAD = 0x00000004
    snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return None

    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        current_pid = os.getpid()
        count = 0

        if not ctypes.windll.kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return None

        while True:
            if entry.th32OwnerProcessID == current_pid:
                count += 1
            if not ctypes.windll.kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        return count
    finally:
        ctypes.windll.kernel32.CloseHandle(snapshot)


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
        self._cleanup_lock = asyncio.Lock()
        self.cleanup_retention_hours = _read_bounded_int(
            'ATRI_CLEANUP_RETENTION_HOURS', 24, 1, 8760
        )
        self.cleanup_interval_seconds = _read_bounded_int(
            'ATRI_CLEANUP_INTERVAL_SECONDS', 21_600, 600, 604_800
        )
        self._cleanup_task = asyncio.create_task(
            self._periodic_generated_cleanup(),
            name='atri-generated-artifact-cleaner',
        )

    def cog_unload(self) -> None:
        self._cleanup_task.cancel()
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if (
            self._shutdown_task is not None
            and self._shutdown_task is not current_task
            and not self._shutdown_task.done()
        ):
            self._shutdown_task.cancel()

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
        thread_count = status_map.get('Threads')

        total_memory = meminfo.get('MemTotal')
        available_memory = meminfo.get('MemAvailable')
        used_memory = None
        memory_percent = None
        if total_memory is not None and available_memory is not None and total_memory > 0:
            used_memory = total_memory - available_memory
            memory_percent = (used_memory / total_memory) * 100

        system_uptime_seconds = _read_system_uptime_seconds()
        boot_marker = '未知'
        boot_id = _read_text(BOOT_ID_PATH)
        if boot_id:
            boot_marker = boot_id.strip()[:8]

        if platform.system() == 'Windows':
            memory_status = _read_windows_memory_status()
            process_memory, peak_working_set = _read_windows_process_memory()
            windows_thread_count = _read_windows_thread_count()
            if windows_thread_count is not None:
                thread_count = str(windows_thread_count)
            elif thread_count is None:
                thread_count = str(os.cpu_count() or '未知')

            if memory_status is not None and memory_status.ullTotalPhys > 0:
                total_memory = memory_status.ullTotalPhys
                available_memory = memory_status.ullAvailPhys
                used_memory = total_memory - available_memory
                memory_percent = (used_memory / total_memory) * 100

            if process_memory is not None:
                vm_rss = process_memory
            if peak_working_set is not None:
                vm_hwm = peak_working_set

            try:
                system_uptime_seconds = ctypes.windll.kernel32.GetTickCount64() / 1000
            except AttributeError:
                pass

            boot_marker = platform.node() or 'Windows'
        else:
            if thread_count is None:
                thread_count = str(os.cpu_count() or '未知')

        if used_memory is None or total_memory is None or memory_percent is None:
            system_memory = '未知'
        else:
            system_memory = (
                f'{_format_bytes(used_memory)} / {_format_bytes(total_memory)} '
                f'({_format_percent(memory_percent)})'
            )

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

    async def _periodic_generated_cleanup(self) -> None:
        try:
            await self.bot.wait_until_ready()
            # Let startup/download recovery settle before the first pass.
            await asyncio.sleep(min(300, self.cleanup_interval_seconds))
            cleaner = GeneratedArtifactCleaner(Path(__file__).resolve().parent)
            while not self.bot.is_closed():
                try:
                    async with self._cleanup_lock:
                        protection = collect_cleanup_protection(self.bot)
                        report = await asyncio.to_thread(
                            cleaner.clean,
                            minimum_age_hours=self.cleanup_retention_hours,
                            protection=protection,
                        )
                except Exception as exc:
                    print(
                        '[WARN] Protected generated-artifact cleanup pass failed: '
                        f'{exc.__class__.__name__}'
                    )
                else:
                    if report.deleted_count or report.failures:
                        print(
                            '[INFO] Protected generated-artifact cleanup: '
                            f'deleted={report.deleted_count}, freed={report.freed_bytes}, '
                            f'failures={len(report.failures)}'
                        )
                await asyncio.sleep(self.cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                '[WARN] Protected generated-artifact cleaner stopped unexpectedly: '
                f'{exc.__class__.__name__}'
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
            try:
                await asyncio.wait_for(self.bot.close(), timeout=15.0)
            except TimeoutError:
                print('[WARN] Graceful Bot close timed out; forcing process exit for supervisor restart')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f'[WARN] Failed to shut down bot cleanly: {exc}')
        finally:
            # The production service uses systemd Restart=always.  Closing only
            # the Discord client is not a reliable supervisor signal because a
            # stuck background task can keep the Python PID alive indefinitely.
            # SIGTERM guarantees that systemd observes process exit and starts
            # one clean replacement after RestartSec.
            os.kill(os.getpid(), signal.SIGTERM)

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

    @app_commands.command(name='清理缓存', description='预览或清理 ATRI 自己产生的过期缓存和轮转日志')
    @app_commands.rename(confirm='确认', minimum_age_hours='最小保留小时')
    @app_commands.describe(
        confirm='不勾选时只预览；勾选后才会执行删除',
        minimum_age_hours='只处理修改时间超过该时长的产物（1～8760）',
    )
    async def cleanup_generated_cache(
        self,
        interaction: discord.Interaction,
        confirm: bool = False,
        minimum_age_hours: app_commands.Range[int, 1, 8760] = 24,
    ) -> None:
        if not await self._ensure_owner(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        cleaner = GeneratedArtifactCleaner(Path(__file__).resolve().parent)
        protection = collect_cleanup_protection(self.bot)
        async with self._cleanup_lock:
            if confirm:
                report = await asyncio.to_thread(
                    cleaner.clean,
                    minimum_age_hours=int(minimum_age_hours),
                    protection=protection,
                )
            else:
                report = await asyncio.to_thread(
                    cleaner.preview,
                    minimum_age_hours=int(minimum_age_hours),
                    protection=protection,
                )

        payload = report.to_dict(include_items=12)
        candidate_count = int(payload['candidateCount'])
        candidate_bytes = int(payload['candidateBytes'])
        deleted_count = int(payload['deletedCount'])
        freed_bytes = int(payload['freedBytes'])
        title = '缓存清理完成' if confirm else '缓存清理预览'
        embed = discord.Embed(
            title=title,
            color=discord.Color.green() if confirm else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        if confirm:
            embed.description = (
                f'已删除 **{deleted_count}** 项，释放 **{_format_bytes(freed_bytes)}**。'
            )
        else:
            embed.description = (
                f'找到 **{candidate_count}** 项候选，共 **{_format_bytes(candidate_bytes)}**。\n'
                '当前只是预览；再次执行并把“确认”设为 `True` 才会删除。'
            )
        items = payload.get('items')
        if isinstance(items, list) and items:
            lines = [
                f"`{item['path']}` · {_format_bytes(int(item['bytes']))}"
                for item in items
                if isinstance(item, dict)
            ]
            if payload.get('truncated'):
                lines.append('…只显示前 12 项')
            embed.add_field(name='候选范围', value='\n'.join(lines)[:1024], inline=False)
        failures = payload.get('failures')
        if isinstance(failures, list) and failures:
            embed.add_field(
                name='未能处理',
                value='\n'.join(str(item) for item in failures[:8])[:1024],
                inline=False,
            )
        embed.set_footer(
            text=(
                '只处理 ATRI 白名单产物；活动下载、播放中音乐、当前日志、配置与凭据始终受保护。'
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
            '正在优雅退出 Bot；服务器上的 systemd 会在约 5 秒后自动拉起新进程。',
            ephemeral=True,
        )
        self._shutdown_task = asyncio.create_task(self._shutdown_after_delay(1.0))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SystemAdmin(bot))
