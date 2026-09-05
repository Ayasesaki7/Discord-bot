from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat.agent.privacy import (
    ConversationScope,
    CrossTenantAccessError,
    PrivacyBoundary,
    build_safe_debug_snapshot,
    build_sensitive_content_metadata,
    sensitive_content_logging_enabled,
)


SECRET = b"test-only-agent-session-secret-32-bytes-long"


class PrivacyBoundaryTests(unittest.TestCase):
    def make_boundary(self, root: Path) -> PrivacyBoundary:
        return PrivacyBoundary(secret=SECRET, session_root=root)

    def test_same_scope_has_stable_opaque_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            boundary = self.make_boundary(Path(temp_dir))
            scope = ConversationScope.from_discord_ids(
                guild_id=100,
                channel_id=200,
                user_id=300,
            )

            first = boundary.bind(scope)
            second = boundary.bind(scope)

            self.assertEqual(first.identity, second.identity)
            self.assertNotIn("100", first.identity.tenant_key)
            self.assertNotIn("200", first.identity.session_key)

    def test_different_guilds_never_share_tenant_or_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            boundary = self.make_boundary(Path(temp_dir))
            private = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )
            public = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=101,
                    channel_id=200,
                    user_id=300,
                )
            )

            self.assertNotEqual(private.identity.tenant_key, public.identity.tenant_key)
            self.assertNotEqual(private.identity.session_key, public.identity.session_key)
            with self.assertRaises(CrossTenantAccessError):
                boundary.require_same_session(private, public)

    def test_channels_in_one_guild_have_separate_conversation_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            boundary = self.make_boundary(Path(temp_dir))
            first = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )
            second = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=201,
                    user_id=301,
                )
            )

            self.assertEqual(first.identity.tenant_key, second.identity.tenant_key)
            self.assertNotEqual(first.identity.session_key, second.identity.session_key)
            with self.assertRaises(CrossTenantAccessError):
                boundary.require_same_session(first, second)

    def test_direct_messages_are_tenant_scoped_by_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            boundary = self.make_boundary(Path(temp_dir))
            first = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=None,
                    channel_id=900,
                    user_id=300,
                )
            )
            second = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=None,
                    channel_id=901,
                    user_id=301,
                )
            )

            self.assertNotEqual(first.identity.tenant_key, second.identity.tenant_key)

    def test_reply_target_is_bound_to_originating_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = self.make_boundary(Path(temp_dir)).bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )

            context.reply.require_destination(guild_id=100, channel_id=200)
            with self.assertRaises(CrossTenantAccessError):
                context.reply.require_destination(guild_id=101, channel_id=200)
            with self.assertRaises(CrossTenantAccessError):
                context.reply.require_destination(guild_id=100, channel_id=201)

    def test_session_paths_are_namespaced_below_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            boundary = self.make_boundary(root)
            context = boundary.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )

            session_path = boundary.session_path(context)

            session_path.relative_to(root.resolve())
            self.assertEqual(session_path.parent.name, context.identity.tenant_key)
            self.assertEqual(session_path.name, context.identity.session_key)

    def test_reset_rotates_only_the_selected_session_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            boundary = self.make_boundary(root)
            selected_scope = ConversationScope.from_discord_ids(
                guild_id=100,
                channel_id=200,
                user_id=300,
            )
            other_scope = ConversationScope.from_discord_ids(
                guild_id=100,
                channel_id=201,
                user_id=300,
            )
            old_selected = boundary.bind(selected_scope)
            old_other = boundary.bind(other_scope)

            new_selected = boundary.rotate_session(selected_scope)

            self.assertNotEqual(old_selected.identity.session_key, new_selected.identity.session_key)
            self.assertEqual(old_other.identity, boundary.bind(other_scope).identity)
            restarted = self.make_boundary(root)
            self.assertEqual(new_selected.identity, restarted.bind(selected_scope).identity)

    def test_revision_state_contains_no_raw_discord_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            boundary = self.make_boundary(root)
            scope = ConversationScope.from_discord_ids(
                guild_id=123456789012345678,
                channel_id=223456789012345678,
                user_id=323456789012345678,
            )

            boundary.rotate_session(scope)

            revision_text = (root / "_session-revisions.json").read_text(encoding="utf-8")
            self.assertNotIn(str(scope.guild_id), revision_text)
            self.assertNotIn(str(scope.channel_id), revision_text)
            self.assertNotIn(str(scope.user_id), revision_text)

    def test_from_env_creates_and_reuses_machine_local_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "LOCALAPPDATA": temp_dir,
                "ATRI_AGENT_SESSION_SECRET": "",
            }
            with patch.dict(os.environ, environment, clear=True):
                first = PrivacyBoundary.from_env()
                second = PrivacyBoundary.from_env()
                scope = ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )

            self.assertEqual(first.bind(scope).identity, second.bind(scope).identity)
            key_path = Path(temp_dir) / "ATRI" / "agent-session.key"
            self.assertTrue(key_path.is_file())
            self.assertEqual(len(key_path.read_text(encoding="ascii").strip()), 64)


class SensitiveLoggingTests(unittest.TestCase):
    def test_sensitive_logging_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sensitive_content_logging_enabled())

    def test_content_metadata_does_not_contain_plaintext(self) -> None:
        secret_message = "这是只应该存在于私密服务器里的内容"

        metadata = build_sensitive_content_metadata(secret_message)

        self.assertNotIn(secret_message, repr(metadata))
        self.assertEqual(metadata["characters"], len(secret_message))

    def test_debug_snapshot_redacts_payloads_and_excerpts(self) -> None:
        debug = {
            "model": "test-model",
            "message_count": 3,
            "last_event_excerpt": "private message",
            "attempts": [
                {
                    "response_status": 400,
                    "error_body": '{"private":"message"}',
                }
            ],
        }

        sanitized = build_safe_debug_snapshot(debug)

        self.assertEqual(sanitized["model"], "test-model")
        self.assertEqual(sanitized["last_event_excerpt"], "[redacted]")
        self.assertEqual(sanitized["attempts"][0]["error_body"], "[redacted]")
        self.assertNotIn("private message", repr(sanitized))


if __name__ == "__main__":
    unittest.main()
