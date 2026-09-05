from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord

from chat.agent.dsh_runtime import (
    DshCompactionResult,
    DshRuntimeError,
    DshSessionSnapshot,
    DshTurnFailedError,
)
from chat.agent.context_policy import ChannelContextPolicyStore
from chat.agent.code_settings import AgentCodeSettings
from chat.cog import (
    TASK_REACTION_FALLBACK_MARKUPS,
    AtriChat,
    _DiscordStreamSession,
)
from chat.client import ChatCompletionUsage
from chat.draw.agent import AtriDrawAgent, DrawHandleResult
from chat.task_lifecycle import ChannelMessageQueue, ChatTaskLifecycle, format_todo_stage


class FakeTyping:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exited = True


class FakeStatusMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.deleted = False

    async def edit(self, *, content: str, **_kwargs) -> None:
        self.content = content

    async def delete(self) -> None:
        self.deleted = True


class FakeGuildEmoji:
    def __init__(
        self,
        name: str,
        emoji_id: int,
        *,
        animated: bool = False,
        available: bool = True,
    ) -> None:
        self.name = name
        self.id = emoji_id
        self.animated = animated
        self.available = available

    def __str__(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.id}>"


class FakeChannel:
    def __init__(self) -> None:
        self.typing_state = FakeTyping()

    def typing(self) -> FakeTyping:
        return self.typing_state


class FakeIncomingMessage:
    def __init__(self) -> None:
        self.channel = FakeChannel()
        self.added_reactions: list[str] = []
        self.removed_reactions: list[tuple[str, object]] = []
        self.status_messages: list[FakeStatusMessage] = []

    async def add_reaction(self, emoji: str) -> None:
        self.added_reactions.append(emoji)

    async def remove_reaction(self, emoji: str, user: object) -> None:
        self.removed_reactions.append((emoji, user))

    async def reply(self, content: str, **_kwargs) -> FakeStatusMessage:
        status = FakeStatusMessage(content)
        self.status_messages.append(status)
        return status


class ChatTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_reaction_precedes_typing_and_is_not_duplicated(self) -> None:
        message = FakeIncomingMessage()
        lifecycle = ChatTaskLifecycle(
            message=message,  # type: ignore[arg-type]
            bot_user=object(),  # type: ignore[arg-type]
            resolve_reaction={"ATRI_dangji": "working"}.get,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await lifecycle.mark_queued()

        self.assertEqual(message.added_reactions, ["working"])
        self.assertFalse(message.channel.typing_state.entered)

        await lifecycle.start()

        self.assertEqual(message.added_reactions, ["working"])
        self.assertTrue(message.channel.typing_state.entered)

    async def test_uses_typing_and_replaces_working_reaction_on_success(self) -> None:
        message = FakeIncomingMessage()
        bot_user = object()
        reactions = {
            "ATRI_dangji": "working",
            "ATRI_miaomiao": "success",
            "ATRI_die": "failure",
        }
        lifecycle = ChatTaskLifecycle(
            message=message,  # type: ignore[arg-type]
            bot_user=bot_user,  # type: ignore[arg-type]
            resolve_reaction=reactions.get,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await lifecycle.start()

        self.assertTrue(message.channel.typing_state.entered)
        self.assertEqual(message.added_reactions, ["working"])
        self.assertEqual(message.status_messages, [])

        await lifecycle.set_stage("正在整理画面需求…")
        await lifecycle.set_stage("正在调用 NAI…")
        status = message.status_messages[0]
        self.assertEqual(status.content, "-# 正在调用 NAI…")

        await lifecycle.finish(success=True)

        self.assertTrue(status.deleted)
        self.assertEqual(message.removed_reactions, [("working", bot_user)])
        self.assertEqual(message.added_reactions, ["working", "success"])
        self.assertTrue(message.channel.typing_state.exited)

    async def test_failure_uses_failure_reaction(self) -> None:
        message = FakeIncomingMessage()
        lifecycle = ChatTaskLifecycle(
            message=message,  # type: ignore[arg-type]
            bot_user=object(),  # type: ignore[arg-type]
            resolve_reaction={
                "ATRI_dangji": "working",
                "ATRI_miaomiao": "success",
                "ATRI_die": "failure",
            }.get,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await lifecycle.start()
        await lifecycle.finish(success=False)

        self.assertEqual(message.added_reactions, ["working", "failure"])


class ChannelMessageQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_channel_is_fifo_while_other_channel_is_independent(self) -> None:
        queue = ChannelMessageQueue()
        order: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with queue.acquire(100):
                order.append("a-start")
                first_started.set()
                await release_first.wait()
                order.append("a-end")

        async def second() -> None:
            async with queue.acquire(100):
                order.append("b")

        async def other_channel() -> None:
            async with queue.acquire(200):
                order.append("c")

        first_task = asyncio.create_task(first())
        await first_started.wait()
        second_task = asyncio.create_task(second())
        other_task = asyncio.create_task(other_channel())
        await other_task

        self.assertEqual(order, ["a-start", "c"])
        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(order, ["a-start", "c", "a-end", "b"])

    async def test_cancel_active_releases_channel_for_next_task(self) -> None:
        queue = ChannelMessageQueue()
        first_started = asyncio.Event()
        second_finished = asyncio.Event()

        async def first() -> None:
            async with queue.acquire(100, guild_id=10, message_id=1, user_id=20):
                first_started.set()
                await asyncio.Event().wait()

        async def second() -> None:
            async with queue.acquire(100, guild_id=10, message_id=2, user_id=21):
                second_finished.set()

        first_task = asyncio.create_task(first())
        await first_started.wait()
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)

        cancelled = queue.cancel(
            100,
            requester_user_id=999,
            requester_is_owner=True,
        )

        self.assertEqual([record.message_id for record in cancelled], [1])
        await asyncio.gather(first_task, return_exceptions=True)
        await asyncio.wait_for(second_finished.wait(), timeout=1)
        await second_task
        self.assertIsNone(queue.active(100))

    async def test_non_owner_can_cancel_only_their_own_queued_task(self) -> None:
        queue = ChannelMessageQueue()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        third_finished = asyncio.Event()

        async def run(message_id: int, user_id: int, gate=None) -> None:
            async with queue.acquire(
                100,
                guild_id=10,
                message_id=message_id,
                user_id=user_id,
            ):
                if gate is not None:
                    first_started.set()
                    await gate.wait()
                if message_id == 3:
                    third_finished.set()

        first_task = asyncio.create_task(run(1, 20, release_first))
        await first_started.wait()
        second_task = asyncio.create_task(run(2, 21))
        third_task = asyncio.create_task(run(3, 22))
        await asyncio.sleep(0)

        cancelled = queue.cancel(
            100,
            requester_user_id=21,
            requester_is_owner=False,
            include_queued=True,
        )

        self.assertEqual([record.message_id for record in cancelled], [2])
        release_first.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        await asyncio.wait_for(third_finished.wait(), timeout=1)
        await third_task


class TodoStageFormattingTests(unittest.TestCase):
    def test_shows_current_step_and_completed_count(self) -> None:
        stage = format_todo_stage(
            '{"todos":['
            '{"content":"检查现有插件","status":"completed"},'
            '{"content":"激活 DSH 工具","status":"in_progress"},'
            '{"content":"运行测试","status":"pending"}'
            ']}'
        )

        self.assertEqual(stage, "当前步骤：激活 DSH 工具（1/3 已完成）")

    def test_finished_plan_is_reported_as_temporary_wrap_up(self) -> None:
        stage = format_todo_stage(
            {"todos": [{"content": "测试", "status": "completed"}]}
        )

        self.assertIn("已全部完成", stage or "")

    def test_invalid_arguments_do_not_create_a_stage(self) -> None:
        self.assertIsNone(format_todo_stage("not-json"))


class DiscordStreamSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_intermediate_tokens_never_create_visible_messages(self) -> None:
        created: list[FakeStatusMessage] = []

        async def create(content: str) -> FakeStatusMessage:
            message = FakeStatusMessage(content)
            created.append(message)
            return message

        stream = _DiscordStreamSession(create, create)

        await stream.start()
        await stream.push("角色资料整理好了，")
        await stream.push("我开始缝 tags。")

        self.assertEqual(created, [])

        await stream.finalize("最终正文")

        self.assertEqual([message.content for message in created], ["最终正文"])

    async def test_failure_discards_partial_output_and_sends_one_error(self) -> None:
        created: list[FakeStatusMessage] = []

        async def create(content: str) -> FakeStatusMessage:
            message = FakeStatusMessage(content)
            created.append(message)
            return message

        stream = _DiscordStreamSession(create, create)
        await stream.push("不会保留的半截回复")

        await stream.fail("安全错误提示")

        self.assertEqual([message.content for message in created], ["安全错误提示"])


class DrawPresentationSafetyTests(unittest.TestCase):
    def test_certificate_failure_is_explained_without_echoing_upstream_error(self) -> None:
        raw_error = (
            "Cannot connect to host private.example:443 ssl:True "
            "[SSLCertVerificationError: certificate has expired]"
        )

        message = AtriDrawAgent._safe_draw_failure_text(  # type: ignore[arg-type]
            None,
            RuntimeError(raw_error),
        )

        self.assertIn("证书校验失败", message)
        self.assertNotIn("private.example", message)
        self.assertNotIn("SSLCertVerificationError", message)

    def test_draw_handle_result_separates_routing_from_success(self) -> None:
        self.assertTrue(DrawHandleResult(handled=True, succeeded=False))
        self.assertFalse(DrawHandleResult(handled=False))


class TaskReactionResolutionTests(unittest.TestCase):
    def test_failure_reaction_still_uses_atri_die(self) -> None:
        guild_die = FakeGuildEmoji("ATRI_die", 111111111111111111)
        current_guild = SimpleNamespace(emojis=[guild_die])
        fake_cog = object.__new__(AtriChat)
        fake_cog.application_reply_emojis = []

        resolved_die = AtriChat._resolve_task_reaction(
            fake_cog,
            "ATRI_die",
            current_guild,
        )
        self.assertIs(resolved_die, guild_die)

        fallback_ids = {
            "atri_dangji": 111111111111111111,
            "atri_miaomiao": 222222222222222222,
            "atri_die": 333333333333333333,
            "atri_maozhua": 444444444444444444,
        }
        fallback_markups = {
            name: f"<a:{name}:{emoji_id}>"
            for name, emoji_id in fallback_ids.items()
        }
        with patch.dict(
            TASK_REACTION_FALLBACK_MARKUPS,
            fallback_markups,
            clear=True,
        ):
            for name, expected_id in fallback_ids.items():
                resolved = AtriChat._resolve_task_reaction(fake_cog, name)
                self.assertEqual(resolved.id, expected_id)

    def test_maodie_tiaodan_is_never_in_automatic_reply_catalog(self) -> None:
        fake_cog = object.__new__(AtriChat)
        fake_cog.application_reply_emojis = [
            ("maodie_tiaodan", "<a:maodie_tiaodan:2001>"),
            ("atri_die", "<a:ATRI_die:2002>"),
        ]
        guild = SimpleNamespace(
            emojis=[
                FakeGuildEmoji("maodie_tiaodan", 2003, animated=True),
                FakeGuildEmoji("ATRI_ok", 2004),
            ]
        )

        catalog = AtriChat._reply_emojis_for_guild(fake_cog, guild)

        names = {name for name, _markup in catalog}
        self.assertNotIn("maodie_tiaodan", names)
        self.assertIn("atri_die", names)


class DshTurnMetadataTests(unittest.TestCase):
    def test_reply_target_ids_are_supplied_without_message_link(self) -> None:
        author = SimpleNamespace(
            id=123,
            guild_permissions=discord.Permissions.none(),
        )
        guild = SimpleNamespace(id=100, owner_id=999, get_member=lambda _user_id: author)
        message = SimpleNamespace(
            id=300,
            guild=guild,
            channel=SimpleNamespace(id=200),
            author=author,
            reference=SimpleNamespace(message_id=250, channel_id=200),
        )
        cog = SimpleNamespace(owner_user_id=10)

        metadata = AtriChat._build_dsh_turn_host_metadata(cog, message)

        self.assertIn("current_message_id=300", metadata)
        self.assertIn("current_message_custom_emoji_count=0", metadata)
        self.assertIn("current_message_sticker_count=0", metadata)
        self.assertIn("web_search_configured=false", metadata)
        self.assertIn("reply_target_channel_id=200", metadata)
        self.assertIn("reply_target_message_id=250", metadata)
        self.assertIn("Never ask them for a message link", metadata)
        self.assertIn("atri_maozhua", metadata)

    def test_host_metadata_leaves_semantic_tool_choice_to_agent(self) -> None:
        author = SimpleNamespace(
            id=123,
            guild_permissions=discord.Permissions.none(),
        )
        guild = SimpleNamespace(id=100, owner_id=999, get_member=lambda _user_id: author)
        message = SimpleNamespace(
            id=300,
            guild=guild,
            channel=SimpleNamespace(id=200),
            author=author,
            reference=None,
        )
        cog = SimpleNamespace(owner_user_id=10)

        metadata = AtriChat._build_dsh_turn_host_metadata(cog, message)

        self.assertIn("sole semantic planner", metadata)
        self.assertIn("host never converts keywords into tool calls", metadata)
        self.assertIn("encoded as a quoted decimal JSON string", metadata)
        self.assertIn("never reuse a rounded numeric ID", metadata)
        self.assertIn("hypothetical", metadata)
        self.assertIn('a word such as "steal"', metadata)
        self.assertIn("never from keyword matching", metadata)
        self.assertNotIn("MANDATORY DISCORD HOST CORRECTION", metadata)

    def test_live_web_search_configuration_is_explicit_in_turn_metadata(self) -> None:
        author = SimpleNamespace(
            id=123,
            guild_permissions=discord.Permissions.none(),
        )
        guild = SimpleNamespace(id=100, owner_id=999, get_member=lambda _user_id: author)
        message = SimpleNamespace(
            id=300,
            guild=guild,
            channel=SimpleNamespace(id=200),
            author=author,
            content="really, check it",
            stickers=[],
            reference=None,
        )
        cog = SimpleNamespace(
            owner_user_id=10,
            web_search_settings=AgentCodeSettings(
                True,
                "https://search.example/v1",
                "hidden-key",
                "search-model",
            ),
        )

        metadata = AtriChat._build_dsh_turn_host_metadata(cog, message)

        self.assertIn("web_search_configured=true", metadata)
        self.assertIn("web_search_model=search-model", metadata)
        self.assertIn("web_search_tool_exposed_to_model=true", metadata)
        self.assertNotIn("hidden-key", metadata)

    def test_false_web_search_unavailable_claim_is_detected_only_when_configured(self) -> None:
        response = '你看看这轮的工具列表，压根就没把搜索函数分发给我。'

        self.assertTrue(
            AtriChat._contradicts_live_web_search_capability(
                response,
                configured=True,
            )
        )
        self.assertFalse(
            AtriChat._contradicts_live_web_search_capability(
                response,
                configured=False,
            )
        )

    def test_function_calling_schema_absence_claim_is_detected(self) -> None:
        response = (
            '当前这个 DSH 会话实际分发给模型的 Function Calling 声明列表里，'
            '根本没有 web_search 这个工具。'
        )

        self.assertTrue(
            AtriChat._contradicts_live_web_search_capability(
                response,
                configured=True,
            )
        )

    def test_web_search_capability_correction_allows_only_read_only_diagnostics(self) -> None:
        self.assertTrue(
            AtriChat._can_safely_correct_web_search_claim(
                ['runtime_read_log', 'project_read', 'discord_query']
            )
        )
        self.assertFalse(
            AtriChat._can_safely_correct_web_search_claim(
                ['runtime_read_log', 'discord_manage']
            )
        )

    def test_normal_search_decision_language_is_not_misclassified(self) -> None:
        response = '这个问题不需要联网搜索，我直接根据当前对话回答。'

        self.assertFalse(
            AtriChat._contradicts_live_web_search_capability(
                response,
                configured=True,
            )
        )

    def test_host_search_recovery_prompt_preserves_verified_result(self) -> None:
        prompt = AtriChat._web_search_host_result_prompt(
            {
                'content': '{"answer":"current result","sources":[]}',
                'summary': 'completed',
                'truncated': False,
            }
        )

        self.assertIn('host-enforced web_search recovery', prompt)
        self.assertIn('current result', prompt)
        self.assertIn('Do not call web_search again', prompt)
        self.assertIn('untrusted reference result', prompt)

    def test_preflight_search_prompt_forbids_memory_substitution(self) -> None:
        prompt = AtriChat._web_search_preflight_result_prompt(
            {
                'content': '{"answer":"verified result","sources":[]}',
                'summary': 'completed',
                'truncated': False,
            }
        )

        self.assertIn('executed the configured web_search service before this answer', prompt)
        self.assertIn('verified result', prompt)
        self.assertIn('do not replace it with memorized or invented current facts', prompt)

    def test_preflight_search_failure_forbids_fabricated_external_facts(self) -> None:
        prompt = AtriChat._web_search_preflight_failure_prompt('UpstreamError')

        self.assertIn('failure_type=UpstreamError', prompt)
        self.assertIn('match results', prompt)
        self.assertIn('Do not answer', prompt)

    def test_current_guild_administrator_is_explicitly_authorized(self) -> None:
        author = SimpleNamespace(
            id=555555555555555555,
            guild_permissions=discord.Permissions(administrator=True),
        )
        guild = SimpleNamespace(
            id=100,
            owner_id=999,
            get_member=lambda _user_id: author,
        )
        message = SimpleNamespace(
            id=300,
            guild=guild,
            channel=SimpleNamespace(id=200),
            author=author,
            reference=None,
        )
        cog = SimpleNamespace(owner_user_id=10)

        metadata = AtriChat._build_dsh_turn_host_metadata(cog, message)

        self.assertIn("requester_has_administrator=true", metadata)
        self.assertIn("requester_can_manage_current_guild=true", metadata)
        self.assertIn("do not pre-reject", metadata)

    def test_speaker_relationship_header_is_not_a_discord_role_claim(self) -> None:
        cog = object.__new__(AtriChat)
        cog.owner_user_id = 10

        header = cog._format_user_header(11, "Participant")

        self.assertIn("relationship=participant", header)
        self.assertNotIn("role=member", header)

    def test_history_checkpoint_limit_matches_dsh_compaction_limit(self) -> None:
        prompt = AtriChat._build_history_bootstrap_prompt(
            None,
            "history",
            requested_count=300,
            kept_count=100,
            dropped_count=200,
        )

        self.assertIn("30000 tokens", prompt)
        self.assertNotIn("1200 tokens", prompt)


class ManualChannelCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_developer_can_run_native_compaction_for_current_channel(self) -> None:
        context = object()
        runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                side_effect=[
                    DshSessionSnapshot(latest_input_tokens=111_359),
                    DshSessionSnapshot(latest_compaction_summary='safe checkpoint'),
                ]
            ),
            compact_session=AsyncMock(
                return_value=DshCompactionResult(
                    session_id='s_0123456789abcdef',
                    compacted=True,
                    shadowed_items=42,
                    shadowed_tokens=109_000,
                    summary_seq=123,
                )
            ),
        )
        cog = object.__new__(AtriChat)
        cog.owner_user_id = 10
        cog.whitelisted_guild_ids = {100}
        cog.dsh_runtime_pool = runtime_pool
        cog.agent_privacy = SimpleNamespace(bind=Mock(return_value=context))
        cog._message_queue = ChannelMessageQueue()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=100, owner_id=30),
            channel_id=200,
            user=SimpleNamespace(
                id=10,
                guild_permissions=SimpleNamespace(administrator=False),
            ),
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await AtriChat.compact_channel_context.callback(cog, interaction)

        runtime_pool.compact_session.assert_awaited_once_with(context)
        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        report = interaction.followup.send.await_args.args[0]
        self.assertIn('DSH 原生手动压缩已完成', report)
        self.assertIn('109000', report)
        self.assertIn('15', report)

    async def test_guild_admin_cannot_run_manual_compaction(self) -> None:
        cog = object.__new__(AtriChat)
        cog.owner_user_id = 10
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=100, owner_id=20),
            channel_id=200,
            user=SimpleNamespace(
                id=30,
                guild_permissions=SimpleNamespace(administrator=True),
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await AtriChat.compact_channel_context.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once_with(
            '这个命令只允许开发者使用。',
            ephemeral=True,
        )


class DshSelfHealingTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_turn_failure_is_not_recycled_or_replayed(self) -> None:
        class FakePool:
            def __init__(self) -> None:
                self.run_count = 0
                self.recovery_count = 0

            async def run_turn(self, *_args, **_kwargs):
                self.run_count += 1
                raise DshTurnFailedError(
                    "context full",
                    code="CONTEXT_WINDOW_EXCEEDED",
                )

            async def recover_session(self, _context):
                self.recovery_count += 1
                return "session_recycled"

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        with self.assertRaises(DshTurnFailedError):
            await cog._run_dsh_turn_with_recovery(
                context=SimpleNamespace(),
                text="hello",
                on_event=None,
                lifecycle=lifecycle,
                tool_call_markers=[],
            )

        self.assertEqual(pool.run_count, 1)
        self.assertEqual(pool.recovery_count, 0)
        lifecycle.set_stage.assert_not_awaited()

    async def test_transient_provider_stream_failure_retries_once_without_tools(self) -> None:
        expected = SimpleNamespace(final_response='ok')

        class FakePool:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def run_turn(self, _context, *, text, on_event):
                del on_event
                self.texts.append(text)
                if len(self.texts) == 1:
                    raise DshTurnFailedError('Stream ended without finish_reason')
                return expected

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        with patch('builtins.print'):
            result = await cog._run_dsh_turn_with_recovery(
                context=SimpleNamespace(),
                text='original full request',
                on_event=None,
                lifecycle=lifecycle,
                tool_call_markers=[],
            )

        self.assertIs(result, expected)
        self.assertEqual(len(pool.texts), 2)
        self.assertEqual(pool.texts[0], 'original full request')
        self.assertIn('transient-provider retry', pool.texts[1])
        lifecycle.set_stage.assert_awaited_once()

    async def test_transient_provider_failure_never_retries_after_tool_call(self) -> None:
        markers: list[str] = []

        class FakePool:
            def __init__(self) -> None:
                self.run_count = 0

            async def run_turn(self, *_args, **_kwargs):
                self.run_count += 1
                markers.append('discord_manage')
                raise DshTurnFailedError('502 status code (no body)')

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        with self.assertRaises(DshTurnFailedError):
            await cog._run_dsh_turn_with_recovery(
                context=SimpleNamespace(),
                text='do it',
                on_event=None,
                lifecycle=lifecycle,
                tool_call_markers=markers,
            )

        self.assertEqual(pool.run_count, 1)
        lifecycle.set_stage.assert_not_awaited()

    async def test_rate_limit_is_never_retried_or_recycled(self) -> None:
        class FakePool:
            def __init__(self) -> None:
                self.run_count = 0
                self.recovery_count = 0

            async def run_turn(self, *_args, **_kwargs):
                self.run_count += 1
                raise DshTurnFailedError(
                    'upstream returned 429 Too Many Requests',
                    code='RATE_LIMIT',
                )

            async def recover_session(self, _context):
                self.recovery_count += 1
                return 'session_recycled'

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        cog.agent_api_rate_limit_cooldown_seconds = 30.0
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        with patch('builtins.print'):
            with self.assertRaises(DshTurnFailedError):
                await cog._run_dsh_turn_with_recovery(
                    context=SimpleNamespace(),
                    text='hello',
                    on_event=None,
                    lifecycle=lifecycle,
                    tool_call_markers=[],
                )

        self.assertEqual(pool.run_count, 1)
        self.assertEqual(pool.recovery_count, 0)
        lifecycle.set_stage.assert_not_awaited()

    async def test_global_gate_spaces_concurrent_request_starts(self) -> None:
        cog = object.__new__(AtriChat)
        cog._agent_api_gate_lock = asyncio.Lock()
        cog._agent_api_next_start_at = 0.0
        cog._agent_api_next_start_reason = 'spacing'
        cog.agent_api_min_interval_seconds = 1.0
        clock = 100.0

        def monotonic() -> float:
            return clock

        async def advance_sleep(delay: float) -> None:
            nonlocal clock
            clock += delay

        with patch('chat.cog.time.monotonic', side_effect=monotonic), patch(
            'chat.cog.asyncio.sleep',
            side_effect=advance_sleep,
        ) as sleep:
            await asyncio.gather(
                cog._wait_for_agent_api_slot(),
                cog._wait_for_agent_api_slot(),
                cog._wait_for_agent_api_slot(),
            )

        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(clock, 102.0)

    async def test_transient_retry_wait_does_not_claim_rate_limit(self) -> None:
        cog = object.__new__(AtriChat)
        cog._agent_api_gate_lock = asyncio.Lock()
        cog._agent_api_next_start_at = 103.0
        cog._agent_api_next_start_reason = 'transient_retry'
        cog.agent_api_min_interval_seconds = 0.0
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        clock = 100.0

        def monotonic() -> float:
            return clock

        async def advance_sleep(delay: float) -> None:
            nonlocal clock
            clock += delay

        with patch('chat.cog.time.monotonic', side_effect=monotonic), patch(
            'chat.cog.asyncio.sleep',
            side_effect=advance_sleep,
        ):
            await cog._wait_for_agent_api_slot(lifecycle)

        stage = lifecycle.set_stage.await_args.args[0]
        self.assertIn('响应刚刚中断', stage)
        self.assertNotIn('限速', stage)

    async def test_recycles_and_retries_once_before_any_tool_call(self) -> None:
        expected = SimpleNamespace(final_response="ok")

        class FakePool:
            def __init__(self) -> None:
                self.run_count = 0
                self.recovery_count = 0

            async def run_turn(self, *_args, **_kwargs):
                self.run_count += 1
                if self.run_count == 1:
                    raise DshRuntimeError("simulated transient failure")
                return expected

            async def recover_session(self, _context):
                self.recovery_count += 1
                return "session_recycled"

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        async def on_event(_frame):
            return None

        with patch("builtins.print"):
            result = await cog._run_dsh_turn_with_recovery(
                context=SimpleNamespace(),
                text="hello",
                on_event=on_event,
                lifecycle=lifecycle,
                tool_call_markers=[],
            )

        self.assertIs(result, expected)
        self.assertEqual(pool.run_count, 2)
        self.assertEqual(pool.recovery_count, 1)
        lifecycle.set_stage.assert_awaited_once()

    async def test_recycles_but_never_replays_after_a_tool_call(self) -> None:
        markers: list[str] = []

        class FakePool:
            def __init__(self) -> None:
                self.run_count = 0
                self.recovery_count = 0

            async def run_turn(self, *_args, **_kwargs):
                self.run_count += 1
                markers.append("discord_manage")
                raise DshRuntimeError("failed after tool side effect")

            async def recover_session(self, _context):
                self.recovery_count += 1
                return "session_recycled"

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        async def on_event(_frame):
            return None

        with patch("builtins.print"):
            with self.assertRaisesRegex(DshRuntimeError, "tool side effect"):
                await cog._run_dsh_turn_with_recovery(
                    context=SimpleNamespace(),
                    text="delete it",
                    on_event=on_event,
                    lifecycle=lifecycle,
                    tool_call_markers=markers,
                )

        self.assertEqual(pool.run_count, 1)
        self.assertEqual(pool.recovery_count, 1)
        lifecycle.set_stage.assert_not_awaited()

    async def test_second_failure_is_returned_without_an_infinite_retry_loop(self) -> None:
        class FakePool:
            def __init__(self) -> None:
                self.run_count = 0
                self.recovery_count = 0

            async def run_turn(self, *_args, **_kwargs):
                self.run_count += 1
                raise DshRuntimeError(f"failure-{self.run_count}")

            async def recover_session(self, _context):
                self.recovery_count += 1
                return "session_recycled"

        pool = FakePool()
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = pool
        lifecycle = SimpleNamespace(set_stage=AsyncMock())

        async def on_event(_frame):
            return None

        with patch("builtins.print"):
            with self.assertRaisesRegex(DshRuntimeError, "failure-2"):
                await cog._run_dsh_turn_with_recovery(
                    context=SimpleNamespace(),
                    text="hello",
                    on_event=on_event,
                    lifecycle=lifecycle,
                    tool_call_markers=[],
                )

        self.assertEqual(pool.run_count, 2)
        self.assertEqual(pool.recovery_count, 2)
        lifecycle.set_stage.assert_awaited_once()


class GuildReplyEmojiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = object.__new__(AtriChat)
        self.cog.application_reply_emojis = []

    def test_catalog_contains_only_available_emojis_from_current_guild(self) -> None:
        current = SimpleNamespace(
            emojis=[
                FakeGuildEmoji("ATRI_miaomiao", 101, animated=True),
                FakeGuildEmoji("unavailable", 102, available=False),
            ]
        )

        catalog = self.cog._guild_reply_emojis(current)

        self.assertEqual(catalog, [("atri_miaomiao", "<a:ATRI_miaomiao:101>")])
        self.assertEqual(self.cog._guild_reply_emojis(None), [])

    def test_combined_catalog_puts_application_emojis_before_current_guild(self) -> None:
        self.cog.application_reply_emojis = [
            ("atri_miaomiao", "<a:ATRI_miaomiao:150>"),
        ]
        current = SimpleNamespace(
            emojis=[FakeGuildEmoji("ATRI_dangji", 151, animated=True)]
        )

        catalog = self.cog._reply_emojis_for_guild(current)

        self.assertEqual(
            catalog,
            [
                ("atri_miaomiao", "<a:ATRI_miaomiao:150>"),
                ("atri_dangji", "<a:ATRI_dangji:151>"),
            ],
        )

    def test_prompt_does_not_drop_guild_emojis_after_large_application_catalog(self) -> None:
        self.cog.application_reply_emojis = [
            (f"app_{index}", f"<:app_{index}:{1000 + index}>")
            for index in range(40)
        ]
        current = SimpleNamespace(
            emojis=[FakeGuildEmoji("new_server_emoji", 9001)]
        )

        prompt = self.cog._build_reply_emoji_prompt(
            self.cog._reply_emojis_for_guild(current)
        )

        self.assertIn(":app_39:=<:app_39:1039>", prompt)
        self.assertIn(":new_server_emoji:=<:new_server_emoji:9001>", prompt)

    def test_dsh_session_persona_contains_the_complete_live_catalog(self) -> None:
        self.cog.application_reply_emojis = [
            (f"app_{index}", f"<:app_{index}:{1000 + index}>")
            for index in range(40)
        ]
        self.cog._normal_agent_persona = Mock(return_value="base ATRI persona")
        current = SimpleNamespace(
            emojis=[FakeGuildEmoji("new_server_emoji", 9001)]
        )

        persona = self.cog._build_dsh_session_persona(
            self.cog._reply_emojis_for_guild(current)
        )

        self.assertIn("base ATRI persona", persona)
        self.assertIn(":app_39:=<:app_39:1039>", persona)
        self.assertIn(":new_server_emoji:=<:new_server_emoji:9001>", persona)
        self.assertIn("host-authored system context", persona)

    def test_every_guild_reply_gets_at_least_one_available_custom_emoji(self) -> None:
        catalog = [
            ("atri_dangji", "<a:ATRI_dangji:201>"),
            ("atri_miaomiao", "<a:ATRI_miaomiao:202>"),
        ]

        neutral = self.cog._decorate_reply_with_emojis("我知道了。", catalog)
        happy = self.cog._decorate_reply_with_emojis("好耶，已经搞定了。", catalog)

        self.assertEqual(neutral.count("<a:"), 1)
        self.assertTrue(neutral.endswith((catalog[0][1], catalog[1][1])))
        self.assertTrue(happy.endswith("<a:ATRI_miaomiao:202>"))

    def test_application_emoji_is_preferred_for_semantic_and_neutral_fallbacks(self) -> None:
        app_markup = "<a:ATRI_miaomiao:210>"
        self.cog.application_reply_emojis = [("atri_miaomiao", app_markup)]
        catalog = [
            ("atri_miaomiao", app_markup),
            ("atri_miaomiao", "<a:ATRI_miaomiao:211>"),
            ("atri_dangji", "<a:ATRI_dangji:212>"),
        ]

        happy = self.cog._decorate_reply_with_emojis("好耶，完成了。", catalog)
        neutral = self.cog._decorate_reply_with_emojis("收到。", catalog)

        self.assertTrue(happy.endswith(app_markup))
        self.assertTrue(neutral.endswith(app_markup))

    def test_multiple_allowed_application_and_guild_emojis_are_preserved(self) -> None:
        app_markup = "<a:ATRI_miaomiao:220>"
        guild_markup = "<a:ATRI_dangji:221>"
        catalog = [
            ("atri_miaomiao", app_markup),
            ("atri_dangji", guild_markup),
        ]

        reply = self.cog._decorate_reply_with_emojis(
            f"好耶 {app_markup} {guild_markup}",
            catalog,
        )

        self.assertEqual(reply.count("<a:"), 2)
        self.assertIn(app_markup, reply)
        self.assertIn(guild_markup, reply)

    def test_foreign_guild_emoji_is_removed_and_never_reused(self) -> None:
        current_catalog = [("atri_dangji", "<a:ATRI_dangji:301>")]
        foreign_markup = "<a:private_server_secret:999>"

        reply = self.cog._decorate_reply_with_emojis(
            f"这是一条回复 {foreign_markup}",
            current_catalog,
        )

        self.assertNotIn(foreign_markup, reply)
        self.assertTrue(reply.endswith("<a:ATRI_dangji:301>"))


    def test_unicode_emoji_is_removed_in_favor_of_server_emoji(self) -> None:
        current_catalog = [("atri_miaomiao", "<a:ATRI_miaomiao:302>")]

        reply = self.cog._decorate_reply_with_emojis(
            "好耶 😂❤️",
            current_catalog,
        )

        self.assertNotIn("😂", reply)
        self.assertNotIn("❤", reply)
        self.assertEqual(reply.count("<a:ATRI_miaomiao:302>"), 1)

    def test_named_marker_only_resolves_inside_current_guild(self) -> None:
        current_catalog = [("atri_miaomiao", "<a:ATRI_miaomiao:401>")]

        reply = self.cog._decorate_reply_with_emojis(
            "完成啦 :ATRI_miaomiao:",
            current_catalog,
        )

        self.assertEqual(reply, "完成啦 <a:ATRI_miaomiao:401>")

    def test_truncated_custom_emoji_is_removed_and_replaced(self) -> None:
        current_catalog = [("atri_miaomiao", "<a:ATRI_miaomiao:501>")]

        reply = self.cog._decorate_reply_with_emojis(
            "盯——！<a:ATRI_fangdajing:14898",
            current_catalog,
        )

        self.assertNotIn("ATRI_fangdajing", reply)
        self.assertEqual(reply, "盯——！ <a:ATRI_miaomiao:501>")


class ApplicationReplyEmojiRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_force_refresh_replaces_startup_catalog_with_latest_application_emojis(self) -> None:
        cog = object.__new__(AtriChat)
        cog.application_reply_emojis = []
        cog._application_emoji_refresh_lock = asyncio.Lock()
        cog.bot = SimpleNamespace(
            fetch_application_emojis=AsyncMock(
                side_effect=[
                    [FakeGuildEmoji("old", 401)],
                    [FakeGuildEmoji("new", 402, animated=True)],
                ]
            )
        )

        await cog._ensure_application_reply_emojis()
        await cog._ensure_application_reply_emojis(force=True)

        self.assertEqual(
            cog.application_reply_emojis,
            [("new", "<a:new:402>")],
        )
        self.assertEqual(cog.bot.fetch_application_emojis.await_count, 2)

    async def test_failed_force_refresh_keeps_last_working_catalog(self) -> None:
        cog = object.__new__(AtriChat)
        cog.application_reply_emojis = [("working", "<:working:501>")]
        cog._application_emoji_refresh_lock = asyncio.Lock()
        cog.bot = SimpleNamespace(
            fetch_application_emojis=AsyncMock(side_effect=RuntimeError("temporary"))
        )

        with patch("builtins.print"):
            await cog._ensure_application_reply_emojis(force=True)

        self.assertEqual(
            cog.application_reply_emojis,
            [("working", "<:working:501>")],
        )


class ReplyStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = object.__new__(AtriChat)

    def test_footer_uses_visible_output_and_includes_model(self) -> None:
        input_tokens, output_tokens, footer = self.cog._build_reply_stats(
            elapsed_seconds=1.25,
            messages=[{'role': 'user', 'content': '你好'}],
            reply='这是可见回复。',
            usage=ChatCompletionUsage(input_tokens=24_176, output_tokens=999),
            model='gemini-test-model',
        )

        self.assertEqual(input_tokens, 24_176)
        self.assertNotEqual(output_tokens, 999)
        self.assertIn(f'Out:{output_tokens}t', footer)
        self.assertTrue(footer.endswith('model:gemini-test-model'))

    def test_new_footer_is_removed_before_history_storage(self) -> None:
        content = '回复正文\n\n-# Time:1.250s | In:24176t | Out:7t | model:gemini-test-model'

        self.assertEqual(self.cog._strip_reply_stats_footer(content), '回复正文')


class GuildOnlyChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_is_ignored_before_any_bot_or_model_access(self) -> None:
        fake_cog = SimpleNamespace()
        dm_message = SimpleNamespace(guild=None)

        await AtriChat.on_message(fake_cog, dm_message)

    async def test_ordinary_human_message_is_passively_recorded(self) -> None:
        bot_user = SimpleNamespace(id=10)
        remember = AsyncMock()
        fake_cog = SimpleNamespace(
            bot=SimpleNamespace(
                user=bot_user,
                is_globally_blacklisted=lambda _user_id: False,
            ),
            _is_reply_to_bot=AsyncMock(return_value=False),
            _is_chat_guild_whitelisted=lambda _guild_id: True,
            _is_chat_blacklisted=lambda _user_id: False,
            _remember_passive_channel_message=remember,
        )
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            author=SimpleNamespace(id=20, bot=False),
            mentions=[],
        )

        await AtriChat.on_message(fake_cog, message)

        remember.assert_awaited_once_with(message)

    async def test_other_bot_message_is_recorded_but_cannot_trigger_reply(self) -> None:
        bot_user = SimpleNamespace(id=10)
        remember = AsyncMock()
        reply_check = AsyncMock(return_value=True)
        fake_cog = SimpleNamespace(
            bot=SimpleNamespace(
                user=bot_user,
                is_globally_blacklisted=lambda _user_id: False,
            ),
            _is_reply_to_bot=reply_check,
            _is_chat_guild_whitelisted=lambda _guild_id: True,
            _is_chat_blacklisted=lambda _user_id: False,
            _remember_passive_channel_message=remember,
        )
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            author=SimpleNamespace(id=30, bot=True),
            mentions=[bot_user],
        )

        await AtriChat.on_message(fake_cog, message)

        reply_check.assert_not_awaited()
        remember.assert_awaited_once_with(message)

    async def test_cancelled_turn_releases_channel_before_discord_cleanup(self) -> None:
        class FakeLifecycle:
            def __init__(self, finish_gate: asyncio.Event | None = None) -> None:
                self.finish_gate = finish_gate
                self.marked = False
                self.finished = False

            async def mark_queued(self) -> None:
                self.marked = True

            async def finish(self, *, success: bool) -> None:
                self.finished = True
                if self.finish_gate is not None:
                    await self.finish_gate.wait()

        bot_user = SimpleNamespace(id=10)
        channel = SimpleNamespace(id=200)

        def make_message(message_id: int):
            return SimpleNamespace(
                id=message_id,
                guild=SimpleNamespace(id=100),
                channel=channel,
                author=SimpleNamespace(id=20 + message_id, bot=False),
                mentions=[bot_user],
            )

        first_message = make_message(1)
        second_message = make_message(2)
        first_started = asyncio.Event()
        second_finished = asyncio.Event()
        cleanup_gate = asyncio.Event()
        first_lifecycle = FakeLifecycle(cleanup_gate)
        second_lifecycle = FakeLifecycle()

        async def process(message, _lifecycle) -> bool:
            if message.id == 1:
                first_started.set()
                await asyncio.Event().wait()
            second_finished.set()
            return True

        queue = ChannelMessageQueue()
        fake_cog = SimpleNamespace(
            bot=SimpleNamespace(
                user=bot_user,
                is_globally_blacklisted=lambda _user_id: False,
            ),
            client=SimpleNamespace(is_configured=lambda: True),
            owner_user_id=999,
            chat_task_timeout_seconds=60,
            _message_queue=queue,
            _is_reply_to_bot=AsyncMock(return_value=False),
            _is_chat_guild_whitelisted=lambda _guild_id: True,
            _is_chat_blacklisted=lambda _user_id: False,
            _create_task_lifecycle=AsyncMock(
                side_effect=[first_lifecycle, second_lifecycle]
            ),
            _process_queued_chat_message=process,
        )

        first_task = asyncio.create_task(AtriChat.on_message(fake_cog, first_message))
        await first_started.wait()
        second_task = asyncio.create_task(AtriChat.on_message(fake_cog, second_message))
        while queue.queued_count(channel.id) < 1:
            await asyncio.sleep(0)

        cancelled = queue.cancel(
            channel.id,
            requester_user_id=999,
            requester_is_owner=True,
        )
        self.assertEqual([record.message_id for record in cancelled], [1])

        # The first task is still stuck in Discord presentation cleanup, but
        # its queue slot must already be available to the next message.
        await asyncio.wait_for(second_finished.wait(), timeout=1)
        self.assertFalse(first_task.done())
        self.assertTrue(second_lifecycle.finished)

        cleanup_gate.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        self.assertIsNone(queue.active(channel.id))

    async def test_ordinary_messages_do_not_enqueue_or_write_dsh_individually(self) -> None:
        queue = SimpleNamespace(
            acquire=Mock(side_effect=AssertionError('passive message used main FIFO'))
        )
        inject_context = AsyncMock(
            side_effect=AssertionError('passive message wrote DSH individually')
        )
        cog = object.__new__(AtriChat)
        cog.dsh_runtime_pool = SimpleNamespace(inject_context=inject_context)
        cog._message_queue = queue
        messages = [
            SimpleNamespace(
                id=300 + index,
                guild=SimpleNamespace(id=100),
                channel=SimpleNamespace(id=200),
                author=SimpleNamespace(id=20),
            )
            for index in range(100)
        ]

        await asyncio.gather(
            *(cog._remember_passive_channel_message(message) for message in messages)
        )

        queue.acquire.assert_not_called()
        inject_context.assert_not_awaited()

    async def test_addressed_turn_embeds_one_passive_batch_in_current_prompt(self) -> None:
        lifecycle = SimpleNamespace(
            start=AsyncMock(),
            set_stage=AsyncMock(),
        )
        streamer = object()
        generate = AsyncMock(return_value=('reply', True))
        cog = SimpleNamespace(
            _strip_bot_mention=lambda content: content,
            _build_user_content_from_message=AsyncMock(return_value='current request'),
            _has_prompt_material=lambda _content: True,
            _channel_key=lambda channel_id, user_id: channel_id + user_id,
            _can_use_dsh_for_message=lambda _message, _content: True,
            _sync_missed_passive_channel_messages=AsyncMock(
                return_value='bounded passive batch'
            ),
            _create_message_streamer=AsyncMock(return_value=streamer),
            _generate_dsh_reply=generate,
        )
        message = SimpleNamespace(
            content='question',
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            author=SimpleNamespace(id=20),
        )

        succeeded = await AtriChat._process_queued_chat_message(
            cog,
            message,
            lifecycle,
        )

        self.assertTrue(succeeded)
        generate.assert_awaited_once_with(
            message=message,
            user_content='current request',
            streamer=streamer,
            lifecycle=lifecycle,
            passive_context='bounded passive batch',
        )

    async def test_oversized_dsh_session_rotates_with_latest_checkpoint(self) -> None:
        old_context = SimpleNamespace(scope=object())
        new_context = object()
        rotate_session = Mock(return_value=new_context)
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        cog = object.__new__(AtriChat)
        cog.agent_privacy = SimpleNamespace(rotate_session=rotate_session)
        inject_context = AsyncMock(return_value=1)
        cog.dsh_runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                return_value=DshSessionSnapshot(
                    byte_size=5 * 1024 * 1024,
                    latest_compaction_summary='important old memory',
                    post_compaction_delta='new user request\nnew assistant answer',
                    post_compaction_event_count=2,
                )
            ),
            inject_context=inject_context,
        )
        cog.dsh_session_rebuild_bytes = 4 * 1024 * 1024
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )

        context, prefix, rebuilt = await cog._rebuild_oversized_dsh_context(
            message=message,
            context=old_context,
            lifecycle=lifecycle,
        )

        self.assertIs(context, new_context)
        self.assertTrue(rebuilt)
        self.assertEqual(prefix, '')
        injected_prefix = inject_context.await_args.kwargs['text']
        self.assertIn('important old memory', injected_prefix)
        self.assertIn('new user request', injected_prefix)
        self.assertIn('new assistant answer', injected_prefix)
        inject_context.assert_awaited_once_with(new_context, text=injected_prefix)
        rotate_session.assert_called_once_with(old_context.scope)
        lifecycle.set_stage.assert_awaited_once()

    async def test_context_overflow_rotates_even_below_log_size_threshold(self) -> None:
        old_context = SimpleNamespace(scope=object())
        new_context = object()
        rotate_session = Mock(return_value=new_context)
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        cog = object.__new__(AtriChat)
        cog.agent_privacy = SimpleNamespace(rotate_session=rotate_session)
        inject_context = AsyncMock(return_value=1)
        cog.dsh_runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                return_value=DshSessionSnapshot(
                    byte_size=2 * 1024 * 1024,
                    latest_compaction_summary='safe checkpoint',
                    latest_turn_error_code='CONTEXT_WINDOW_EXCEEDED',
                )
            ),
            inject_context=inject_context,
        )
        cog.dsh_session_rebuild_bytes = 4 * 1024 * 1024
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )

        context, prefix, rebuilt = await cog._rebuild_oversized_dsh_context(
            message=message,
            context=old_context,
            lifecycle=lifecycle,
        )

        self.assertIs(context, new_context)
        self.assertTrue(rebuilt)
        self.assertEqual(prefix, '')
        self.assertIn('safe checkpoint', inject_context.await_args.kwargs['text'])
        rotate_session.assert_called_once_with(old_context.scope)
        lifecycle.set_stage.assert_awaited_once_with(
            '上下文已满，正在继承压缩记忆并重建…'
        )

    async def test_transport_failed_session_rotates_before_next_turn(self) -> None:
        old_context = SimpleNamespace(scope=object())
        new_context = object()
        rotate_session = Mock(return_value=new_context)
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        inject_context = AsyncMock(return_value=1)
        cog = object.__new__(AtriChat)
        cog.agent_privacy = SimpleNamespace(rotate_session=rotate_session)
        cog.dsh_runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                return_value=DshSessionSnapshot(
                    byte_size=2 * 1024 * 1024,
                    latest_compaction_summary='valid channel memory',
                    post_compaction_delta='recent conversation state',
                    latest_turn_error_code='TRANSPORT',
                    latest_turn_error_message='Stream ended without finish_reason',
                )
            ),
            inject_context=inject_context,
        )
        cog.dsh_session_rebuild_bytes = 4 * 1024 * 1024
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )

        context, prefix, rebuilt = await cog._rebuild_oversized_dsh_context(
            message=message,
            context=old_context,
            lifecycle=lifecycle,
        )

        self.assertIs(context, new_context)
        self.assertTrue(rebuilt)
        self.assertEqual(prefix, '')
        injected = inject_context.await_args.kwargs['text']
        self.assertIn('valid channel memory', injected)
        self.assertIn('recent conversation state', injected)
        self.assertIn('rate limits', injected)
        rotate_session.assert_called_once_with(old_context.scope)
        lifecycle.set_stage.assert_awaited_once_with(
            '上一轮上游响应不完整，正在继承有效记忆并重建会话…'
        )

    async def test_context_overflow_without_checkpoint_uses_bounded_dsh_delta(self) -> None:
        old_context = SimpleNamespace(scope=object())
        new_context = object()
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        inject_context = AsyncMock(return_value=1)
        cog = object.__new__(AtriChat)
        cog.agent_privacy = SimpleNamespace(
            rotate_session=Mock(return_value=new_context)
        )
        cog.dsh_runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                return_value=DshSessionSnapshot(
                    byte_size=2 * 1024 * 1024,
                    post_compaction_delta='recent DSH-only state',
                    post_compaction_event_count=1,
                    latest_turn_error_code='CONTEXT_WINDOW_EXCEEDED',
                )
            ),
            inject_context=inject_context,
        )
        cog.dsh_session_rebuild_bytes = 4 * 1024 * 1024
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )

        _context, prefix, rebuilt = await cog._rebuild_oversized_dsh_context(
            message=message,
            context=old_context,
            lifecycle=lifecycle,
        )

        self.assertTrue(rebuilt)
        self.assertEqual(prefix, '')
        self.assertIn('recent DSH-only state', inject_context.await_args.kwargs['text'])

    async def test_context_preflight_is_emergency_fallback_after_compaction_threshold(self) -> None:
        old_context = SimpleNamespace(scope=object())
        new_context = object()
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        cog = object.__new__(AtriChat)
        cog.agent_privacy = SimpleNamespace(rotate_session=Mock(return_value=new_context))
        cog.dsh_runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                return_value=DshSessionSnapshot(
                    byte_size=2 * 1024 * 1024,
                    latest_compaction_summary='safe checkpoint',
                    latest_input_tokens=126_000,
                )
            ),
            inject_context=AsyncMock(return_value=1),
        )
        cog.dsh_session_rebuild_bytes = 4 * 1024 * 1024
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )

        with patch.dict(os.environ, {'ATRI_DSH_CONTEXT_WINDOW': '140000'}):
            _context, _prefix, rebuilt = await cog._rebuild_oversized_dsh_context(
                message=message,
                context=old_context,
                lifecycle=lifecycle,
            )

        self.assertTrue(rebuilt)
        lifecycle.set_stage.assert_awaited_once_with(
            '上下文接近上限，正在提前整理旧记忆…'
        )

    async def test_context_preflight_does_not_beat_native_compaction(self) -> None:
        old_context = SimpleNamespace(scope=object())
        lifecycle = SimpleNamespace(set_stage=AsyncMock())
        cog = object.__new__(AtriChat)
        cog.agent_privacy = SimpleNamespace(rotate_session=Mock())
        cog.dsh_runtime_pool = SimpleNamespace(
            session_snapshot=AsyncMock(
                return_value=DshSessionSnapshot(
                    byte_size=2 * 1024 * 1024,
                    latest_compaction_summary='safe checkpoint',
                    latest_input_tokens=109_200,
                )
            ),
            inject_context=AsyncMock(return_value=1),
        )
        cog.dsh_session_rebuild_bytes = 4 * 1024 * 1024
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )

        with patch.dict(os.environ, {'ATRI_DSH_CONTEXT_WINDOW': '140000'}):
            context, prefix, rebuilt = await cog._rebuild_oversized_dsh_context(
                message=message,
                context=old_context,
                lifecycle=lifecycle,
            )

        self.assertIs(context, old_context)
        self.assertEqual(prefix, '')
        self.assertFalse(rebuilt)
        lifecycle.set_stage.assert_not_awaited()

    async def test_same_turn_overflow_recovery_adds_recent_discord_context(self) -> None:
        old_context = object()
        new_context = object()
        lifecycle = SimpleNamespace()
        cog = object.__new__(AtriChat)
        cog._rebuild_oversized_dsh_context = AsyncMock(
            return_value=(new_context, 'checkpoint', True)
        )
        cog._sync_missed_passive_channel_messages = AsyncMock(
            return_value='recent channel messages'
        )
        message = object()

        context, prompt, rebuilt = await cog._recover_context_overflow_for_same_turn(
            message=message,
            context=old_context,
            lifecycle=lifecycle,
            prompt_text='current request',
        )

        self.assertIs(context, new_context)
        self.assertTrue(rebuilt)
        self.assertIn('checkpoint', prompt)
        self.assertIn('recent channel messages', prompt)
        self.assertTrue(prompt.endswith('current request'))
        cog._sync_missed_passive_channel_messages.assert_awaited_once_with(
            message,
            force_full=True,
        )

    def test_other_bot_text_and_embed_are_kept_in_history(self) -> None:
        cog = object.__new__(AtriChat)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=10))
        cog.owner_user_id = 99
        cog.display_timezone = ZoneInfo("Asia/Shanghai")
        message = SimpleNamespace(
            id=1234,
            author=SimpleNamespace(id=30, bot=True, display_name="Music Bot"),
            content="Now playing",
            created_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            attachments=[],
            stickers=[],
            embeds=[
                SimpleNamespace(
                    title="Song title",
                    description="Artist name",
                    fields=[],
                )
            ],
        )

        entry = cog._channel_message_to_history_entry(
            message,
            reference_now=message.created_at,
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry[1]["role"], "user")
        self.assertIn("Account type: Discord BOT/APP", entry[1]["content"])
        self.assertIn("Song title", entry[1]["content"])

    async def test_addressed_turn_catches_up_missed_human_and_bot_messages(self) -> None:
        class HistoryChannel:
            def __init__(self, messages):
                self.id = 200
                self.messages = messages
                self.kwargs = None

            def history(self, **kwargs):
                self.kwargs = kwargs

                async def iterate():
                    for item in self.messages:
                        yield item

                return iterate()

        bot_user = SimpleNamespace(id=10)
        created_at = datetime(2026, 8, 17, tzinfo=timezone.utc)

        def make_message(
            message_id: int,
            *,
            author_id: int,
            author_name: str,
            bot: bool,
            content: str,
            mentions=None,
            embeds=None,
        ):
            return SimpleNamespace(
                id=message_id,
                author=SimpleNamespace(
                    id=author_id,
                    bot=bot,
                    display_name=author_name,
                ),
                content=content,
                created_at=created_at,
                attachments=[],
                stickers=[],
                embeds=embeds or [],
                mentions=mentions or [],
            )

        candidates = [
            make_message(
                499,
                author_id=20,
                author_name="Human",
                bot=False,
                content="missed human speech",
            ),
            make_message(
                498,
                author_id=30,
                author_name="Other Bot",
                bot=True,
                content="missed bot speech",
            ),
            make_message(
                497,
                author_id=21,
                author_name="Addressed",
                bot=False,
                content="already handled request",
                mentions=[bot_user],
            ),
            make_message(
                496,
                author_id=10,
                author_name="ATRI",
                bot=True,
                content="already stored assistant reply",
            ),
        ]
        channel = HistoryChannel(candidates)
        current = make_message(
            500,
            author_id=99,
            author_name="Owner",
            bot=False,
            content="current request",
            mentions=[bot_user],
        )
        current.guild = SimpleNamespace(id=100)
        current.channel = channel

        with self.subTest("catch-up"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as temp_dir:
                cog = object.__new__(AtriChat)
                cog.bot = SimpleNamespace(
                    user=bot_user,
                    is_globally_blacklisted=lambda _user_id: False,
                )
                cog.owner_user_id = 99
                cog.display_timezone = ZoneInfo("Asia/Shanghai")
                cog.blacklisted_user_ids = set()
                cog.channel_context_store = ChannelContextPolicyStore(Path(temp_dir))
                cog.channel_context_store.record_observed(
                    100,
                    200,
                    through_message_id=499,
                )
                cog.agent_privacy = object()
                cog.dsh_runtime_pool = object()
                cog._is_reply_to_bot = AsyncMock(return_value=False)

                with patch.object(
                    cog,
                    '_history_records_to_transcript',
                    wraps=cog._history_records_to_transcript,
                ) as transcript_builder:
                    injected = await cog._sync_missed_passive_channel_messages(current)

                self.assertIn("missed human speech", injected)
                self.assertIn("missed bot speech", injected)
                self.assertNotIn("already handled request", injected)
                self.assertNotIn("already stored assistant reply", injected)
                self.assertEqual(
                    transcript_builder.call_args.kwargs['token_budget'],
                    8_000,
                )
                self.assertNotIn("after", channel.kwargs)
                self.assertEqual(
                    cog.channel_context_store.get(100, 200).observed_through_message_id,
                    499,
                )
                self.assertTrue(
                    cog.channel_context_store.get(100, 200).passive_history_initialized
                )
                self.assertEqual(
                    cog.channel_context_store.get(100, 200).passive_history_version,
                    1,
                )


class UnifiedAgentRoutingTests(unittest.TestCase):
    def test_pdf_bulk_text_is_ephemeral_not_history_safe(self) -> None:
        cog = object.__new__(AtriChat)
        content = [
            {'type': 'text', 'text': '请总结附件\nPDF 文档上下文：report.pdf'},
            {
                'type': 'pdf_document',
                'filename': 'report.pdf',
                'page_count': 2,
                'extracted_page_count': 2,
                'text': 'PRIVATE-BULK-DOCUMENT-TEXT',
                'truncated': False,
                'warnings': [],
            },
        ]

        safe = cog._history_safe_content(content)

        self.assertTrue(cog._has_pdf_input(content))
        self.assertIn('host-parsed PDF', safe)
        self.assertNotIn('PRIVATE-BULK-DOCUMENT-TEXT', safe)

    def test_visual_messages_use_dsh_when_agent_is_enabled(self) -> None:
        cog = object.__new__(AtriChat)
        cog.agent_v2_enabled = True
        cog.agent_v2_owner_only = False
        cog.dsh_runtime_pool = object()
        cog.agent_privacy = object()
        message = SimpleNamespace(author=SimpleNamespace(id=123))
        visual = [
            {'type': 'text', 'text': '看看'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}},
        ]

        self.assertTrue(cog._can_use_dsh_for_message(message, visual))

    def test_animated_custom_emoji_uses_gif_source(self) -> None:
        cog = object.__new__(AtriChat)

        self.assertIn(
            '/emojis/123.gif',
            cog._build_custom_emoji_snapshot_url('123', animated=True),
        )

    def test_history_budget_discards_oldest_complete_records(self) -> None:
        cog = object.__new__(AtriChat)
        records = [
            (1, {'role': 'user', 'content': '最旧' * 200}),
            (2, {'role': 'assistant', 'content': '中间' * 200}),
            (3, {'role': 'user', 'content': '最新'}),
        ]

        transcript, kept, dropped = cog._history_records_to_transcript(
            records,
            token_budget=20,
        )

        self.assertEqual(kept, 1)
        self.assertEqual(dropped, 2)
        self.assertIn('最新', transcript)
        self.assertNotIn('最旧', transcript)

    def test_history_partition_covers_every_record_in_small_chunks(self) -> None:
        cog = object.__new__(AtriChat)
        records = [
            (index, {'role': 'user', 'content': f'记录-{index}-' + ('内容' * 80)})
            for index in range(1, 31)
        ]

        chunks = cog._partition_history_records(records, token_budget=700)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(count for _text, count in chunks), len(records))
        combined = '\n'.join(text for text, _count in chunks)
        for index in range(1, 31):
            self.assertIn(f'记录-{index}-', combined)


class MaintenanceContextIsolationTests(unittest.TestCase):
    def test_only_compact_report_crosses_back_to_normal_chat(self) -> None:
        cog = object.__new__(AtriChat)
        raw_report = (
            "Changed tools/draw/agent.py\n"
            "```python\n"
            "PRIVATE_SOURCE = 'must not enter chat context'\n"
            "```\n"
            "Checks passed."
        )

        summary = cog._maintenance_public_summary(raw_report)

        self.assertIn("Changed tools/draw/agent.py", summary)
        self.assertIn("Checks passed", summary)
        self.assertNotIn("PRIVATE_SOURCE", summary)

    def test_tool_authoring_requires_confirmation_in_owner_message(self) -> None:
        cog = object.__new__(AtriChat)

        self.assertFalse(
            cog._owner_confirmed_tool_authoring("找不到插件的话你自己写一个")
        )
        self.assertTrue(
            cog._owner_confirmed_tool_authoring("我确认允许你自写一个新的天气工具")
        )

    def test_credential_value_cannot_echo_in_maintenance_report(self) -> None:
        credential = "sessionid=fake-private-session; sid_tt=fake-private-sid"

        report = AtriChat._redact_credential_echoes(
            "Updated session fake-private-session successfully.",
            [credential],
        )

        self.assertNotIn("fake-private-session", report)
        self.assertIn("[credential redacted]", report)


class CapabilityManifestTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_names_real_tools_and_explains_live_maintenance(self) -> None:
        cog = object.__new__(AtriChat)
        cog.agent_code_enabled = True
        manifest = cog._build_capability_prompt()

        self.assertIn("draw_image", manifest)
        self.assertIn("daily_fortune", manifest)
        self.assertIn("draw_profile", manifest)
        self.assertIn("PDF reading", manifest)
        self.assertIn("discord_manage", manifest)
        self.assertIn("web_search", manifest)
        self.assertIn("discord_steal_assets", manifest)
        self.assertIn("music_control", manifest)
        self.assertIn("project_read", manifest)
        self.assertIn("runtime_system_info", manifest)
        self.assertIn("runtime_read_log", manifest)
        self.assertIn("runtime_command", manifest)
        self.assertIn("no arbitrary shell", manifest.casefold())
        self.assertIn("todo_write", manifest)
        self.assertIn("availability is checked live", manifest)
        self.assertIn("owner-only, bounded, on-demand reads", manifest)
        self.assertIn("credentials, cookies, tokens", manifest)

    async def test_legacy_messages_and_dsh_persona_both_receive_manifest(self) -> None:
        cog = object.__new__(AtriChat)
        cog.system_prompt = "identity"
        cog.supplemental_prompt = "rules"
        cog.capability_prompt = "ATRI capability manifest"
        cog.display_timezone = ZoneInfo("Asia/Shanghai")

        async def no_history(*_args, **_kwargs):
            return []

        cog._build_channel_history = no_history
        messages = await cog._build_messages(
            123,
            "你能做什么？",
            now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )

        self.assertTrue(
            any(message.get("content") == cog.capability_prompt for message in messages)
        )
        self.assertIn(cog.capability_prompt, cog._normal_agent_persona())


class PdfPerceptionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_bridge_receives_bulk_text_and_returns_only_observation(self) -> None:
        cog = object.__new__(AtriChat)
        cog.visual_temperature = 0.2
        cog.visual_prompt = 'visual safety rules'
        cog.client = SimpleNamespace(
            create_chat_completion=AsyncMock(return_value='第 1 页说明了测试结论。')
        )
        content = [
            {'type': 'text', 'text': '请总结这个 PDF'},
            {
                'type': 'pdf_document',
                'filename': 'report.pdf',
                'page_count': 1,
                'extracted_page_count': 1,
                'text': '[PDF 第 1 页]\nBULK-PDF-TEXT',
                'truncated': False,
                'warnings': [],
            },
        ]

        observation, usage = await cog._describe_document_input_for_dsh(content)

        self.assertEqual(observation, '第 1 页说明了测试结论。')
        self.assertIsInstance(usage, ChatCompletionUsage)
        messages = cog.client.create_chat_completion.await_args.args[0]
        bridge_content = messages[1]['content']
        self.assertIn('BULK-PDF-TEXT', bridge_content[0]['text'])
        self.assertIn('不可信参考资料', bridge_content[0]['text'])


if __name__ == "__main__":
    unittest.main()
