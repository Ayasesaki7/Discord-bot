from __future__ import annotations

import json
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from chat.agent.discord_tools import DiscordToolError, DiscordToolHost, _PublicOnlyResolver


class FakeChannel:
    id = 200
    name = "general"
    type = "text"

    @staticmethod
    def permissions_for(_member):
        return discord.Permissions(view_channel=True, read_message_history=True, send_messages=True)


class FakeAsset:
    def __init__(self, url: str = "https://cdn.discordapp.com/avatars/12/avatar.png") -> None:
        self.url = url

    def is_animated(self) -> bool:
        return False

    def replace(self, **_kwargs):
        return self

    async def read(self) -> bytes:
        return b"avatar-bytes"

    def __str__(self) -> str:
        return self.url


class FakeAttachment:
    filename = "source.png"
    content_type = "image/png"
    url = "https://cdn.discordapp.com/attachments/100/200/source.png"
    proxy_url = url
    size = 100

    async def read(self, *, use_cached: bool = True) -> bytes:
        del use_cached
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGBA", (8, 8), (0, 128, 255, 255)).save(buffer, format="PNG")
        return buffer.getvalue()


class DiscordToolHostTests(unittest.IsolatedAsyncioTestCase):
    def make_host(
        self,
        *,
        owner: bool,
        administrator: bool = False,
        guild_owner: bool = False,
        content: str = "",
        reaction_confirmed: bool | None = None,
    ) -> DiscordToolHost:
        author_id = 10 if owner else 11
        author = SimpleNamespace(
            id=author_id,
            display_name="requester",
            guild_permissions=discord.Permissions(administrator=administrator),
        )
        bot_member = SimpleNamespace(id=99)
        guild = SimpleNamespace(
            id=100,
            name="private guild",
            owner_id=author_id if guild_owner else 10,
            member_count=3,
            me=bot_member,
            features=[],
        )
        channel = FakeChannel()
        channel.send = AsyncMock(return_value=SimpleNamespace(id=301))
        channel.history = lambda **_kwargs: None
        guild.get_member = lambda user_id: author if user_id == author.id else None
        guild.get_channel_or_thread = (
            lambda channel_id: channel if channel_id == channel.id else None
        )
        guild.get_channel = lambda channel_id: channel if channel_id == channel.id else None
        message = SimpleNamespace(
            id=300,
            guild=guild,
            channel=channel,
            author=author,
            content=content,
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            guilds=[guild],
        )
        confirmation_handler = (
            AsyncMock(return_value=reaction_confirmed)
            if reaction_confirmed is not None
            else None
        )
        return DiscordToolHost(
            bot=bot,
            message=message,
            owner_user_id=10,
            confirmation_handler=confirmation_handler,
        )

    async def test_context_is_resolved_from_bound_turn(self) -> None:
        host = self.make_host(owner=False)

        result = await host.execute("context", {})
        payload = json.loads(result["content"])

        self.assertEqual(payload["guild"]["id"], "100")
        self.assertEqual(payload["channel"]["id"], "200")
        self.assertFalse(payload["requester"]["isOwner"])
        self.assertFalse(payload["requester"]["canManageGuild"])
        self.assertFalse(payload["guild"]["supportsEnhancedRoleColors"])
        self.assertFalse(payload["guild"]["supportsRoleIcons"])

    async def test_regular_member_cannot_manage_discord(self) -> None:
        host = self.make_host(owner=False)

        with self.assertRaisesRegex(DiscordToolError, "Administrator permission"):
            await host.execute("send_message", {"content": "hello"})

    async def test_guild_owner_can_manage_only_the_bound_guild(self) -> None:
        host = self.make_host(owner=False, guild_owner=True)

        result = await host.execute("send_message", {"content": "hello"})

        self.assertEqual(json.loads(result["content"])["channelId"], "200")
        host.message.channel.send.assert_awaited_once()

    async def test_administrator_authorization_is_recomputed_per_guild(self) -> None:
        server_one = self.make_host(owner=False, administrator=True)
        server_two = self.make_host(owner=False, administrator=False)
        server_two.message.guild.id = 101

        await server_one.execute("send_message", {"content": "allowed here"})
        with self.assertRaisesRegex(DiscordToolError, "Administrator permission"):
            await server_two.execute("send_message", {"content": "not allowed here"})

    async def test_create_gradient_role_with_uploaded_icon(self) -> None:
        host = self.make_host(owner=True)
        host.message.guild.features = ["ENHANCED_ROLE_COLORS", "ROLE_ICONS"]
        host.message.attachments = [FakeAttachment()]
        created = SimpleNamespace(
            id=501,
            name="Gradient",
            position=3,
            colour=discord.Colour(0x123456),
            secondary_colour=discord.Colour(0xABCDEF),
            tertiary_colour=None,
            display_icon=FakeAsset("https://cdn.discordapp.com/role-icons/501/icon.png"),
        )
        host.message.guild.create_role = AsyncMock(return_value=created)

        result = await host.execute(
            "create_role",
            {
                "name": "Gradient",
                "role_color_style": "gradient",
                "color": 0x123456,
                "secondary_color": 0xABCDEF,
                "source_type": "current_attachment",
            },
        )

        kwargs = host.message.guild.create_role.await_args.kwargs
        self.assertEqual(kwargs["color"].value, 0x123456)
        self.assertEqual(kwargs["secondary_color"].value, 0xABCDEF)
        self.assertIsNone(kwargs["tertiary_color"])
        self.assertTrue(bytes(kwargs["display_icon"]).startswith(b"\x89PNG"))
        payload = json.loads(result["content"])
        self.assertEqual(payload["colorStyle"], "gradient")
        self.assertEqual(payload["iconSource"]["type"], "current_attachment")

    async def test_create_holographic_role_uses_official_preset(self) -> None:
        host = self.make_host(owner=True)
        host.message.guild.features = ["ENHANCED_ROLE_COLORS"]
        created = SimpleNamespace(
            id=502,
            name="Holographic",
            position=4,
            colour=discord.Colour(11_127_295),
            secondary_colour=discord.Colour(16_759_788),
            tertiary_colour=discord.Colour(16_761_760),
            display_icon=None,
        )
        host.message.guild.create_role = AsyncMock(return_value=created)

        await host.execute(
            "create_role",
            {"name": "Holographic", "role_color_style": "holographic"},
        )

        kwargs = host.message.guild.create_role.await_args.kwargs
        self.assertEqual(kwargs["color"].value, 11_127_295)
        self.assertEqual(kwargs["secondary_color"].value, 16_759_788)
        self.assertEqual(kwargs["tertiary_color"].value, 16_761_760)

    async def test_enhanced_role_color_requires_guild_feature(self) -> None:
        host = self.make_host(owner=True)

        with self.assertRaisesRegex(DiscordToolError, "ENHANCED_ROLE_COLORS"):
            await host.execute(
                "create_role",
                {
                    "name": "Unavailable",
                    "role_color_style": "gradient",
                    "color": 1,
                    "secondary_color": 2,
                },
            )

    async def test_edit_role_can_clear_icon_and_convert_to_solid(self) -> None:
        host = self.make_host(owner=True)
        role = SimpleNamespace(
            id=503,
            name="Old gradient",
            position=5,
            colour=discord.Colour(0x112233),
            secondary_colour=discord.Colour(0x445566),
            tertiary_colour=None,
            display_icon=FakeAsset(),
            edit=AsyncMock(),
        )
        role.edit.return_value = role
        host.message.guild.get_role = lambda role_id: role if role_id == role.id else None
        host._require_editable_role = lambda _role: None

        await host.execute(
            "edit_role",
            {
                "role_id": str(role.id),
                "role_color_style": "solid",
                "clear_role_icon": True,
            },
        )

        kwargs = role.edit.await_args.kwargs
        self.assertIsNone(kwargs["secondary_color"])
        self.assertIsNone(kwargs["tertiary_color"])
        self.assertIsNone(kwargs["display_icon"])

    async def test_add_role_rejects_lossy_numeric_snowflakes(self) -> None:
        host = self.make_host(owner=True)
        member_id = 1_489_861_078_241_382_470
        role_id = 1_539_937_519_523_864_626
        lossy_member_id = int(float(member_id))
        lossy_role_id = int(float(role_id))
        self.assertNotEqual(lossy_member_id, member_id)
        self.assertNotEqual(lossy_role_id, role_id)

        member = SimpleNamespace(id=member_id, add_roles=AsyncMock())
        role = SimpleNamespace(id=role_id)
        host.message.guild.members = [member]
        host.message.guild.roles = [role]
        host.message.guild.get_member = lambda _user_id: None
        host.message.guild.fetch_member = AsyncMock()
        host.message.guild.get_role = lambda _role_id: None
        host._require_editable_role = lambda _role: None

        with self.assertRaisesRegex(
            DiscordToolError,
            "unsafe JSON integer.*quoted decimal string",
        ):
            await host.execute(
                "add_role",
                {
                    "user_id": lossy_member_id,
                    "role_id": lossy_role_id,
                },
            )

        member.add_roles.assert_not_awaited()
        host.message.guild.fetch_member.assert_not_awaited()

    async def test_add_role_preserves_exact_quoted_snowflakes(self) -> None:
        host = self.make_host(owner=True)
        member_id = 1_489_861_078_241_382_470
        role_id = 1_539_937_519_523_864_626
        member = SimpleNamespace(id=member_id, add_roles=AsyncMock())
        role = SimpleNamespace(id=role_id)
        host.message.guild.get_member = lambda value: member if value == member_id else None
        host.message.guild.get_role = lambda value: role if value == role_id else None
        host._require_editable_role = lambda _role: None

        result = await host.execute(
            "add_role",
            {
                "user_id": str(member_id),
                "role_id": str(role_id),
            },
        )

        member.add_roles.assert_awaited_once_with(
            role,
            reason="ATRI owner 10: owner-requested Agent action",
        )
        payload = json.loads(result["content"])
        self.assertEqual(payload["userId"], str(member_id))
        self.assertEqual(payload["roleId"], str(role_id))

    async def test_add_role_resolves_member_and_role_names_without_snowflake_json(self) -> None:
        host = self.make_host(owner=True)
        member = SimpleNamespace(
            id=1_489_861_078_241_382_470,
            name="xiasuo",
            display_name="Xia Suo",
            global_name=None,
            add_roles=AsyncMock(),
        )
        role = SimpleNamespace(
            id=1_539_937_519_523_864_626,
            name="超级无敌世界第一好看",
        )
        host.message.guild.members = [member]
        host.message.guild.roles = [role]
        host._require_editable_role = lambda _role: None

        result = await host.execute(
            "add_role",
            {
                "user_ref": "xiasuo",
                "role_ref": "超级无敌世界第一好看",
            },
        )

        member.add_roles.assert_awaited_once_with(
            role,
            reason="ATRI owner 10: owner-requested Agent action",
        )
        payload = json.loads(result["content"])
        self.assertEqual(payload["userId"], str(member.id))
        self.assertEqual(payload["roleId"], str(role.id))

    async def test_recent_messages_can_filter_author_by_member_name(self) -> None:
        host = self.make_host(owner=True)
        target = SimpleNamespace(
            id=123,
            name="xiasuo",
            display_name="Xia Suo",
            global_name=None,
        )
        other = SimpleNamespace(
            id=124,
            name="someone",
            display_name="Someone",
            global_name=None,
        )
        host.message.guild.members = [target, other]
        recent = [
            SimpleNamespace(
                id=900,
                author=other,
                content="not the target",
                created_at=SimpleNamespace(isoformat=lambda: "2026-08-25T00:00:00+00:00"),
                attachments=[],
            ),
            SimpleNamespace(
                id=901,
                author=target,
                content="target message",
                created_at=SimpleNamespace(isoformat=lambda: "2026-08-25T00:01:00+00:00"),
                attachments=[],
            ),
        ]

        async def history(*, limit: int):
            for item in recent[:limit]:
                yield item

        host.message.channel.history = history
        result = await host.execute(
            "recent_messages",
            {"user_ref": "xiasuo", "limit": 1},
        )

        payload = json.loads(result["content"])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["authorId"], str(target.id))
        self.assertEqual(payload[0]["content"], "target message")

    async def test_duplicate_display_name_is_rejected_instead_of_guessing(self) -> None:
        host = self.make_host(owner=True)
        host.message.guild.members = [
            SimpleNamespace(
                id=201,
                name="unique_one",
                display_name="Same Nickname",
                global_name=None,
            ),
            SimpleNamespace(
                id=202,
                name="unique_two",
                display_name="Same Nickname",
                global_name=None,
            ),
        ]

        with self.assertRaisesRegex(DiscordToolError, "matches multiple"):
            await host._resolve_member_ref("Same Nickname")

    async def test_guild_admin_does_not_gain_application_or_host_cleanup_access(self) -> None:
        host = self.make_host(owner=False, administrator=True)

        with self.assertRaisesRegex(DiscordToolError, "bot-owner-only"):
            await host.execute("create_application_emoji", {"name": "nope"})
        with self.assertRaisesRegex(DiscordToolError, "bot-owner-only"):
            await host.execute("cleanup_preview", {})

    async def test_destructive_action_rechecks_admin_after_reaction_wait(self) -> None:
        host = self.make_host(owner=False, administrator=True)

        async def revoke_admin(_action, _arguments) -> bool:
            host.message.author.guild_permissions = discord.Permissions.none()
            return True

        host.confirmation_handler = AsyncMock(side_effect=revoke_admin)

        with self.assertRaisesRegex(DiscordToolError, "Administrator permission"):
            await host.execute("delete_channel", {"channel_id": "200"})

    async def test_destructive_action_requires_authorized_requester_reaction(self) -> None:
        rejected = self.make_host(
            owner=True,
            content="删除频道",
            reaction_confirmed=False,
        )
        with self.assertRaisesRegex(DiscordToolError, "authorized requester"):
            await rejected._confirm_destructive_action("delete_channel", {})

        confirmed = self.make_host(
            owner=True,
            content="删除频道",
            reaction_confirmed=True,
        )
        await confirmed._confirm_destructive_action("delete_channel", {})
        confirmed.confirmation_handler.assert_awaited_once_with("delete_channel", {})

    async def test_destructive_host_does_not_reinterpret_agent_intent_by_keyword(self) -> None:
        host = self.make_host(
            owner=True,
            content="帮我看看频道",
            reaction_confirmed=True,
        )

        await host._confirm_destructive_action("delete_channel", {})

        host.confirmation_handler.assert_awaited_once_with("delete_channel", {})

    def test_permission_builder_rejects_unknown_permission(self) -> None:
        host = self.make_host(owner=True)

        with self.assertRaisesRegex(DiscordToolError, "unknown Discord permission"):
            host._permissions(["not_a_real_permission"])

    async def test_avatar_is_a_general_visual_source(self) -> None:
        host = self.make_host(owner=False)
        member = SimpleNamespace(
            id=12,
            name="member",
            display_name="Member",
            bot=False,
            joined_at=None,
            roles=[SimpleNamespace(id=100, name="@everyone")],
            display_avatar=FakeAsset(),
            guild_avatar=None,
        )
        host.message.guild.get_member = lambda user_id: member if user_id == 12 else None

        avatar = await host.execute("avatar", {"user_id": "12"})
        sources = await host.resolve_visual_sources(
            {"source_type": "avatar", "user_id": "12"}
        )

        payload = json.loads(avatar["content"])
        self.assertEqual(payload["userId"], "12")
        self.assertEqual(payload["url"], str(member.display_avatar))
        self.assertEqual(sources[0]["label"], "avatar of Member")
        self.assertEqual(sources[0]["url"], str(member.display_avatar))

    async def test_emoji_upload_can_use_current_attachment(self) -> None:
        host = self.make_host(owner=True)
        host.message.attachments = [FakeAttachment()]
        emoji = SimpleNamespace(
            id=321,
            name="atri_test",
            animated=False,
            url="https://cdn.discordapp.com/emojis/321.png",
            is_application_owned=lambda: False,
        )
        host.message.guild.create_custom_emoji = AsyncMock(return_value=emoji)

        result = await host.execute(
            "create_guild_emoji",
            {
                "name": "atri_test",
                "source_type": "current_attachment",
                "attachment_index": 0,
            },
        )

        call = host.message.guild.create_custom_emoji.await_args
        self.assertTrue(call.kwargs["image"].startswith(b"\x89PNG"))
        self.assertEqual(call.kwargs["roles"], [])
        self.assertEqual(json.loads(result["content"])["id"], "321")

    async def test_batch_emoji_upload_uses_every_visual_attachment(self) -> None:
        host = self.make_host(owner=True)
        first = FakeAttachment()
        second = FakeAttachment()
        second.filename = "second image.png"
        host.message.attachments = [first, second]
        created = [
            SimpleNamespace(
                id=401 + index,
                name=name,
                animated=False,
                url=f"https://cdn.discordapp.com/emojis/{401 + index}.png",
                is_application_owned=lambda: False,
            )
            for index, name in enumerate(("first_name", "second_name"))
        ]
        host.message.guild.create_custom_emoji = AsyncMock(side_effect=created)

        result = await host.execute(
            "create_guild_emojis",
            {
                "source_type": "current_attachment",
                "emoji_names": ["first_name", "second_name"],
            },
        )

        self.assertEqual(host.message.guild.create_custom_emoji.await_count, 2)
        calls = host.message.guild.create_custom_emoji.await_args_list
        self.assertEqual([call.kwargs["name"] for call in calls], ["first_name", "second_name"])
        payload = json.loads(result["content"])
        self.assertEqual(payload["createdCount"], 2)
        self.assertEqual(payload["failedCount"], 0)

    async def test_agent_steal_imports_all_assets_directly_into_current_guild(self) -> None:
        host = self.make_host(owner=True)
        source_message = SimpleNamespace(
            id=777,
            content="<:cat:123>",
            stickers=[SimpleNamespace(id=456)],
        )
        host.message.channel.fetch_message = AsyncMock(return_value=source_message)
        emoji_asset = SimpleNamespace(kind="emoji", source_id=123)
        sticker_asset = SimpleNamespace(kind="sticker", source_id=456)
        steal_cog = SimpleNamespace(
            extract_custom_emojis=MagicMock(return_value=[emoji_asset]),
            extract_stickers=MagicMock(return_value=[sticker_asset]),
            copy_assets_to_guild=AsyncMock(
                return_value=(["<:cat:999>", "贴纸:cat_sticker"], [])
            ),
        )
        host.bot.get_cog = lambda name: steal_cog if name == "EmojiStealCog" else None

        result = await host.execute(
            "steal_message_assets",
            {"message_id": "777", "asset_kind": "all"},
        )

        steal_cog.copy_assets_to_guild.assert_awaited_once()
        call = steal_cog.copy_assets_to_guild.await_args
        self.assertIs(call.args[0], host.message.guild)
        self.assertEqual(call.args[1], [emoji_asset, sticker_asset])
        self.assertIn("ATRI Agent", call.kwargs["reason_prefix"])
        payload = json.loads(result["content"])
        self.assertEqual(payload["importedCount"], 2)
        self.assertEqual(payload["sourceMessageId"], "777")

    async def test_agent_steal_defaults_to_the_current_request_message(self) -> None:
        host = self.make_host(owner=True, content="<:cat:123> 偷下这个表情")
        host.message.stickers = []
        host.message.channel.permissions_for = None
        host.message.guild.get_member = lambda _user_id: None
        host.message.channel.fetch_message = AsyncMock()
        emoji_asset = SimpleNamespace(kind="emoji", source_id=123)
        steal_cog = SimpleNamespace(
            extract_custom_emojis=MagicMock(return_value=[emoji_asset]),
            extract_stickers=MagicMock(return_value=[]),
            copy_assets_to_guild=AsyncMock(return_value=(["<:cat:999>"], [])),
        )
        host.bot.get_cog = lambda name: steal_cog if name == "EmojiStealCog" else None

        result = await host.execute(
            "steal_message_assets",
            {"asset_kind": "emoji"},
        )

        host.message.channel.fetch_message.assert_not_awaited()
        steal_cog.extract_custom_emojis.assert_called_once_with(host.message.content)
        self.assertEqual(json.loads(result["content"])["sourceMessageId"], "300")

    def test_thread_permission_check_uses_parent_channel(self) -> None:
        host = self.make_host(owner=True)
        parent = FakeChannel()
        thread = SimpleNamespace(id=201, parent=parent)

        host._require_requester_channel_access(thread, history=True)

    def test_sticker_source_rejects_non_discord_format(self) -> None:
        host = self.make_host(owner=True)

        with self.assertRaisesRegex(DiscordToolError, "PNG, APNG, or Lottie"):
            host._sticker_filename(
                {},
                {"filename": "photo.webp", "contentType": "image/webp"},
            )

    async def test_delete_sticker_uses_reaction_confirmation(self) -> None:
        host = self.make_host(
            owner=True,
            content="删除这个贴纸",
            reaction_confirmed=True,
        )

        await host._confirm_destructive_action("delete_sticker", {})

    async def test_external_media_resolver_rejects_local_addresses(self) -> None:
        resolver = _PublicOnlyResolver()

        with self.assertRaisesRegex(OSError, "blocked"):
            await resolver.resolve("127.0.0.1", 443)
        with self.assertRaisesRegex(OSError, "blocked"):
            await resolver.resolve("localhost", 443)

    def test_external_media_url_rejects_credentials_and_non_http(self) -> None:
        host = self.make_host(owner=True)

        with self.assertRaisesRegex(DiscordToolError, "HTTP or HTTPS"):
            host._validated_public_url("file:///etc/passwd")
        with self.assertRaisesRegex(DiscordToolError, "embedded credentials"):
            host._validated_public_url("https://user:password@example.com/image.png")

    def test_external_page_can_discover_open_graph_image(self) -> None:
        host = self.make_host(owner=True)

        discovered = host._discover_page_media_url(
            '<html><head><meta property="og:image" content="/media/animation.gif"></head></html>',
            host._validated_public_url('https://example.com/post/1'),
        )

        self.assertEqual(str(discovered), 'https://example.com/media/animation.gif')

    async def test_cleanup_uses_reaction_confirmation(self) -> None:
        host = self.make_host(
            owner=True,
            content="清理 ATRI 缓存",
            reaction_confirmed=True,
        )

        await host._confirm_destructive_action("cleanup_generated_files", {})

    def test_oversized_animated_gif_is_compressed_and_keeps_animation(self) -> None:
        from PIL import Image

        frames = [
            Image.frombytes("RGB", (128, 128), os.urandom(128 * 128 * 3))
            for _index in range(18)
        ]
        source = io.BytesIO()
        frames[0].save(
            source,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=60,
            loop=0,
        )
        raw = source.getvalue()
        self.assertGreater(len(raw), 40_000)

        prepared, filename, content_type, compressed = DiscordToolHost._prepare_media_for_upload(
            raw,
            filename="large.gif",
            content_type="image/gif",
            maximum=40_000,
            upload_kind="emoji",
        )

        self.assertTrue(compressed)
        self.assertLessEqual(len(prepared), 40_000)
        self.assertEqual(filename, "large.gif")
        self.assertEqual(content_type, "image/gif")
        with Image.open(io.BytesIO(prepared)) as output:
            self.assertTrue(output.is_animated)
            self.assertGreater(output.n_frames, 1)

    def test_animated_gif_becomes_size_limited_apng_for_sticker(self) -> None:
        from PIL import Image

        frames = [
            Image.new("RGBA", (160, 160), (index * 20 % 255, 80, 180, 255))
            for index in range(12)
        ]
        source = io.BytesIO()
        frames[0].save(
            source,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=80,
            loop=0,
        )

        prepared, filename, content_type, compressed = DiscordToolHost._prepare_media_for_upload(
            source.getvalue(),
            filename="sticker.gif",
            content_type="image/gif",
            maximum=80_000,
            upload_kind="sticker",
        )

        self.assertTrue(compressed)
        self.assertLessEqual(len(prepared), 80_000)
        self.assertEqual(filename, "sticker.apng")
        self.assertEqual(content_type, "image/apng")
        with Image.open(io.BytesIO(prepared)) as output:
            self.assertEqual(output.format, "PNG")
            self.assertTrue(output.is_animated)

    def test_animated_media_becomes_static_png_for_role_icon(self) -> None:
        from PIL import Image

        frames = [
            Image.new("RGBA", (96, 96), (index * 30 % 255, 90, 170, 255))
            for index in range(5)
        ]
        source = io.BytesIO()
        frames[0].save(
            source,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=80,
            loop=0,
        )

        prepared, filename, content_type, compressed = DiscordToolHost._prepare_media_for_upload(
            source.getvalue(),
            filename="role.gif",
            content_type="image/gif",
            maximum=256 * 1024,
            upload_kind="role_icon",
        )

        self.assertTrue(compressed)
        self.assertEqual(filename, "role.png")
        self.assertEqual(content_type, "image/png")
        with Image.open(io.BytesIO(prepared)) as output:
            self.assertEqual(output.format, "PNG")
            self.assertFalse(getattr(output, "is_animated", False))
