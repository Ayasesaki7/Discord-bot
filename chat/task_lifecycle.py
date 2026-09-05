from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass
from contextlib import asynccontextmanager
from collections.abc import Callable
from typing import Any, AsyncIterator

import discord


ReactionResolver = Callable[[str], Any | None]
PRESENTATION_OPERATION_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class ChannelTaskRecord:
    channel_id: int
    guild_id: int | None
    message_id: int
    user_id: int
    task: asyncio.Task[Any]
    cancellable: bool = True
    active: bool = False
    cancelled_while_active: bool = False


class ChannelMessageQueue:
    """One fair FIFO execution slot per Discord channel."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._active: dict[int, ChannelTaskRecord] = {}
        self._queued: dict[int, list[ChannelTaskRecord]] = {}

    @asynccontextmanager
    async def acquire(
        self,
        channel_id: int,
        *,
        guild_id: int | None = None,
        message_id: int = 0,
        user_id: int = 0,
        cancellable: bool = True,
    ) -> AsyncIterator[None]:
        # asyncio.Lock wakes waiters in acquisition order. Discord channel IDs
        # are globally unique, so other channels keep independent execution.
        normalized_channel_id = int(channel_id)
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("channel queue acquisition requires an asyncio task")
        record = ChannelTaskRecord(
            channel_id=normalized_channel_id,
            guild_id=int(guild_id) if guild_id is not None else None,
            message_id=int(message_id),
            user_id=int(user_id),
            task=current_task,
            cancellable=bool(cancellable),
        )
        queue = self._queued.setdefault(normalized_channel_id, [])
        queue.append(record)
        lock = self._locks.setdefault(normalized_channel_id, asyncio.Lock())
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            if record in queue:
                queue.remove(record)
            if not queue:
                self._queued.pop(normalized_channel_id, None)
            record.active = True
            self._active[normalized_channel_id] = record
            yield
        finally:
            if record in queue:
                queue.remove(record)
            if not queue:
                self._queued.pop(normalized_channel_id, None)
            if self._active.get(normalized_channel_id) is record:
                self._active.pop(normalized_channel_id, None)
            record.active = False
            if acquired and lock.locked():
                lock.release()

    def cancel(
        self,
        channel_id: int,
        *,
        requester_user_id: int,
        requester_is_owner: bool,
        include_queued: bool = False,
    ) -> list[ChannelTaskRecord]:
        normalized_channel_id = int(channel_id)

        def allowed(record: ChannelTaskRecord) -> bool:
            return record.cancellable and (
                requester_is_owner or record.user_id == int(requester_user_id)
            )

        selected: list[ChannelTaskRecord] = []
        active = self._active.get(normalized_channel_id)
        if active is not None and allowed(active):
            active.cancelled_while_active = True
            selected.append(active)
        if include_queued:
            selected.extend(
                record
                for record in tuple(self._queued.get(normalized_channel_id, ()))
                if allowed(record)
            )
        seen: set[asyncio.Task[Any]] = set()
        unique: list[ChannelTaskRecord] = []
        for record in selected:
            if record.task in seen or record.task.done():
                continue
            seen.add(record.task)
            unique.append(record)
            record.task.cancel()
        return unique

    def active(self, channel_id: int) -> ChannelTaskRecord | None:
        return self._active.get(int(channel_id))

    def queued_count(self, channel_id: int) -> int:
        return len(self._queued.get(int(channel_id), ()))


def format_todo_stage(arguments: object) -> str | None:
    """Turn DSH todo_write arguments into one temporary Discord stage line."""

    payload = arguments
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    todos = payload.get("todos")
    if not isinstance(todos, list) or not todos:
        return None

    valid = [item for item in todos if isinstance(item, dict)]
    if not valid:
        return None
    completed = sum(1 for item in valid if item.get("status") == "completed")
    active = next(
        (item for item in valid if item.get("status") == "in_progress"),
        None,
    )
    if active is not None:
        content = " ".join(str(active.get("content") or "").split())[:120]
        if content:
            return f"当前步骤：{content}（{completed}/{len(valid)} 已完成）"
    if completed == len(valid):
        return f"任务步骤已全部完成（{completed}/{len(valid)}），正在整理结果…"
    return f"已列出 {len(valid)} 个任务步骤，正在安排执行顺序…"


class ChatTaskLifecycle:
    """Owns Discord-only presentation state for one incoming chat task.

    The model never controls reactions or destinations. Progress messages are
    explicitly temporary and final user-facing content is published elsewhere.
    """

    def __init__(
        self,
        *,
        message: discord.Message,
        bot_user: discord.ClientUser | discord.User | discord.Member | None,
        resolve_reaction: ReactionResolver,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self.message = message
        self.bot_user = bot_user
        self.resolve_reaction = resolve_reaction
        self.allowed_mentions = allowed_mentions
        self._typing: Any | None = None
        self._status_message: discord.Message | None = None
        self._stage = ""
        self._started = False
        self._finished = False
        self._working_reaction: Any | None = None
        self._working_reaction_added = False

    async def __aenter__(self) -> "ChatTaskLifecycle":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if not self._finished:
            await self.finish(success=exc is None)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._typing = self.message.channel.typing()
            await self._typing.__aenter__()
        except Exception:
            self._typing = None
        await self.mark_queued()

    async def mark_queued(self) -> None:
        """Acknowledge a queued message without starting Discord typing yet."""

        if self._finished or self._working_reaction_added:
            return
        self._working_reaction = self.resolve_reaction("ATRI_dangji")
        if self._working_reaction is None:
            print("[WARN] Task reaction is unavailable: name=ATRI_dangji")
        await self._safe_add_reaction(self._working_reaction)
        self._working_reaction_added = self._working_reaction is not None

    async def set_stage(self, text: str) -> None:
        if self._finished:
            return
        normalized = " ".join(str(text or "").split()).strip()
        if not normalized or normalized == self._stage:
            return
        self._stage = normalized[:180]
        content = f"-# {self._stage}"
        if self._status_message is None:
            try:
                self._status_message = await self.message.reply(
                    content,
                    mention_author=False,
                    allowed_mentions=self.allowed_mentions,
                )
            except Exception:
                self._status_message = None
            return
        try:
            await self._status_message.edit(
                content=content,
                allowed_mentions=self.allowed_mentions,
            )
        except Exception:
            self._status_message = None

    async def finish(self, *, success: bool) -> None:
        if self._finished:
            return
        self._finished = True
        await self._delete_status()
        await self._safe_remove_reaction(self._working_reaction)
        terminal_name = "ATRI_miaomiao" if success else "ATRI_die"
        terminal_reaction = self.resolve_reaction(terminal_name)
        if terminal_reaction is None:
            print(f"[WARN] Task reaction is unavailable: name={terminal_name}")
        await self._safe_add_reaction(terminal_reaction)
        if self._typing is not None:
            try:
                async with asyncio.timeout(PRESENTATION_OPERATION_TIMEOUT_SECONDS):
                    await self._typing.__aexit__(None, None, None)
            except Exception:
                pass
            self._typing = None

    async def _delete_status(self) -> None:
        status = self._status_message
        self._status_message = None
        if status is None:
            return
        try:
            async with asyncio.timeout(PRESENTATION_OPERATION_TIMEOUT_SECONDS):
                await status.delete()
        except Exception as exc:
            print(
                "[WARN] Failed to delete temporary task status: "
                f"error_type={exc.__class__.__name__}, "
                f"status={getattr(exc, 'status', None)}, code={getattr(exc, 'code', None)}"
            )

    async def _safe_add_reaction(self, emoji: Any | None) -> None:
        if emoji is None:
            return
        try:
            async with asyncio.timeout(PRESENTATION_OPERATION_TIMEOUT_SECONDS):
                await self.message.add_reaction(emoji)
        except Exception as exc:
            print(
                "[WARN] Failed to add task reaction: "
                f"emoji={getattr(emoji, 'name', str(emoji))}, "
                f"error_type={exc.__class__.__name__}, "
                f"status={getattr(exc, 'status', None)}, code={getattr(exc, 'code', None)}"
            )

    async def _safe_remove_reaction(self, emoji: Any | None) -> None:
        if emoji is None or self.bot_user is None:
            return
        try:
            async with asyncio.timeout(PRESENTATION_OPERATION_TIMEOUT_SECONDS):
                await self.message.remove_reaction(emoji, self.bot_user)
        except Exception as exc:
            print(
                "[WARN] Failed to remove task reaction: "
                f"emoji={getattr(emoji, 'name', str(emoji))}, "
                f"error_type={exc.__class__.__name__}, "
                f"status={getattr(exc, 'status', None)}, code={getattr(exc, 'code', None)}"
            )
