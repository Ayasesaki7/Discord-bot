from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp
import discord
from yarl import URL

from ..tls import build_verified_ssl_context
from tools.cleanup import GeneratedArtifactCleaner, collect_cleanup_protection


_QUERY_ACTIONS = {
    "context",
    "guilds",
    "channels",
    "roles",
    "members",
    "member",
    "avatar",
    "emojis",
    "stickers",
    "threads",
    "scheduled_events",
    "invites",
    "bans",
    "cleanup_preview",
    "recent_messages",
    "audit_log",
}
_MANAGE_ACTIONS = {
    "send_message",
    "create_text_channel",
    "create_voice_channel",
    "create_category",
    "edit_channel",
    "delete_channel",
    "create_role",
    "edit_role",
    "delete_role",
    "add_role",
    "remove_role",
    "set_channel_permissions",
    "timeout_member",
    "kick_member",
    "ban_member",
    "unban_member",
    "delete_message",
    "delete_messages",
    "pin_message",
    "unpin_message",
    "add_reaction",
    "remove_reaction",
    "create_thread",
    "edit_member",
    "create_guild_emoji",
    "create_guild_emojis",
    "steal_message_assets",
    "edit_guild_emoji",
    "delete_guild_emoji",
    "create_application_emoji",
    "edit_application_emoji",
    "delete_application_emoji",
    "create_sticker",
    "edit_sticker",
    "delete_sticker",
    "cleanup_generated_files",
    "edit_guild",
}
_BOT_OWNER_ONLY_MANAGE_ACTIONS = {
    "create_application_emoji",
    "edit_application_emoji",
    "delete_application_emoji",
    "cleanup_generated_files",
}
_DESTRUCTIVE_ACTIONS = {
    "delete_channel",
    "delete_role",
    "kick_member",
    "ban_member",
    "unban_member",
    "delete_message",
    "delete_messages",
    "delete_guild_emoji",
    "delete_application_emoji",
    "delete_sticker",
    "cleanup_generated_files",
}
_RESULT_MAX_CHARS = 12_000
_EMOJI_MAX_BYTES = 256 * 1024
_STICKER_MAX_BYTES = 512 * 1024
_ROLE_ICON_MAX_BYTES = 256 * 1024
_MEDIA_SOURCE_MAX_BYTES = 16 * 1024 * 1024
_MEDIA_MAX_PIXELS = 20_000_000
_MEDIA_MAX_FRAMES = 500
_EXTERNAL_MEDIA_TIMEOUT_SECONDS = 20
_EXTERNAL_MEDIA_MAX_REDIRECTS = 4
_EXTERNAL_PAGE_MAX_BYTES = 1024 * 1024
_VISUAL_ATTACHMENT_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".apng",
    ".avif", ".tif", ".tiff", ".ico",
}
_HOLOGRAPHIC_ROLE_COLORS = (11_127_295, 16_759_788, 16_761_760)
_ROLE_ICON_MEDIA_ARGUMENTS = {
    "source_type",
    "source_url",
    "source_emoji_id",
    "source_sticker_id",
    "attachment_index",
}


class DiscordToolError(RuntimeError):
    pass


DestructiveConfirmationHandler = Callable[
    [str, dict[str, object]],
    Awaitable[bool],
]


class _PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Resolve public Internet addresses while rejecting SSRF destinations."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        normalized = host.strip().rstrip('.').casefold()
        if not normalized or normalized == 'localhost' or normalized.endswith('.localhost'):
            raise OSError('local network destinations are blocked')
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            normalized,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        records: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for resolved_family, _type, protocol, _canonname, sockaddr in infos:
            address = str(sockaddr[0]).split('%', 1)[0]
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError as exc:
                raise OSError('DNS returned an invalid address') from exc
            if not parsed.is_global:
                raise OSError('local, private, reserved, and link-local destinations are blocked')
            key = (int(resolved_family), address)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    'hostname': normalized,
                    'host': address,
                    'port': port,
                    'family': resolved_family,
                    'proto': protocol,
                    'flags': socket.AI_NUMERICHOST,
                }
            )
        if not records:
            raise OSError('DNS did not return a public address')
        return records

    async def close(self) -> None:
        return None


class _PageMediaMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or '').strip() for key, value in attrs}
        if tag.casefold() == 'meta':
            name = (values.get('property') or values.get('name') or '').casefold()
            if name in {'og:image', 'og:image:url', 'twitter:image', 'twitter:image:src'}:
                self._add(values.get('content'))
        elif tag.casefold() == 'link':
            rel = values.get('rel', '').casefold().split()
            if 'image_src' in rel:
                self._add(values.get('href'))

    def _add(self, value: str | None) -> None:
        if value and value not in self.candidates:
            self.candidates.append(value)


class DiscordToolHost:
    """Turn-bound Discord capabilities with host-enforced tenant boundaries."""

    def __init__(
        self,
        *,
        bot: discord.Client,
        message: discord.Message,
        owner_user_id: int,
        confirmation_handler: DestructiveConfirmationHandler | None = None,
    ) -> None:
        self.bot = bot
        self.message = message
        self.owner_user_id = int(owner_user_id)
        self.confirmation_handler = confirmation_handler
        # One host instance is bound to exactly one Discord request. A model may
        # retry an identical destructive call after a zero-result response or a
        # transient API failure; the owner's reaction authorizes that bounded
        # operation for this turn only. A changed target/scope gets a new key
        # and therefore requires another reaction.
        self._confirmed_destructive_scopes: set[str] = set()

    async def execute(
        self,
        action: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        normalized = str(action or "").strip().casefold()
        if normalized not in _QUERY_ACTIONS | _MANAGE_ACTIONS:
            raise DiscordToolError(f"unsupported Discord action: {normalized}")
        if normalized in _QUERY_ACTIONS:
            return await self._query(normalized, arguments)
        return await self._manage(normalized, arguments)

    async def _query(
        self,
        action: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        guild = self._guild()
        if action == "context":
            channel = self.message.channel
            bot_member = guild.me
            permissions = (
                self._enabled_permissions(channel.permissions_for(bot_member))
                if bot_member is not None and hasattr(channel, "permissions_for")
                else []
            )
            return self._result(
                "Resolved the current Discord execution context.",
                {
                    "guild": {
                        "id": str(guild.id),
                        "name": guild.name,
                        "ownerId": str(guild.owner_id),
                        "memberCount": guild.member_count,
                        "features": list(getattr(guild, "features", [])),
                        "supportsEnhancedRoleColors": "ENHANCED_ROLE_COLORS"
                        in getattr(guild, "features", []),
                        "supportsRoleIcons": "ROLE_ICONS"
                        in getattr(guild, "features", []),
                    },
                    "channel": {
                        "id": str(channel.id),
                        "name": getattr(channel, "name", ""),
                        "type": str(getattr(channel, "type", "unknown")),
                    },
                    "requester": {
                        "id": str(self.message.author.id),
                        "name": self.message.author.display_name,
                        "isOwner": self._is_owner(),
                        "isGuildOwner": self._is_guild_owner(),
                        "isAdministrator": self._is_guild_administrator(),
                        "canManageGuild": self._can_manage_guild(),
                    },
                    "bot": {
                        "id": str(self.bot.user.id) if self.bot.user is not None else "",
                        "guildCount": len(self.bot.guilds),
                        "permissionsHere": permissions,
                    },
                },
            )

        if action == "guilds":
            self._require_owner()
            payload = [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "memberCount": item.member_count,
                }
                for item in self.bot.guilds[:200]
            ]
            return self._result(
                f"The bot is connected to {len(self.bot.guilds)} guild(s).",
                payload,
                truncated=len(self.bot.guilds) > len(payload),
            )

        if action == "channels":
            channels: list[dict[str, object]] = []
            for channel in guild.channels:
                if not self._requester_can_view(channel):
                    continue
                channels.append(
                    {
                        "id": str(channel.id),
                        "name": channel.name,
                        "type": str(channel.type),
                        "categoryId": str(channel.category_id) if channel.category_id else None,
                        "position": channel.position,
                    }
                )
            return self._result(
                f"Found {len(channels)} channel(s) visible to the requester.",
                channels,
            )

        if action == "roles":
            roles = [
                {
                    **self._role_payload(role),
                    "managed": role.managed,
                    "mentionable": role.mentionable,
                    "permissions": self._enabled_permissions(role.permissions),
                }
                for role in reversed(guild.roles)
            ]
            return self._result(f"Found {len(roles)} role(s).", roles)

        if action == "members":
            query = str(arguments.get("query") or "").strip().casefold()
            limit = self._bounded_int(arguments.get("limit"), default=25, maximum=100)
            members = []
            for member in guild.members:
                searchable = f"{member.id} {member.name} {member.display_name}".casefold()
                if query and query not in searchable:
                    continue
                members.append(self._member_payload(member))
                if len(members) >= limit:
                    break
            return self._result(f"Returned {len(members)} cached member(s).", members)

        if action == "member":
            member = await self._member_from_arguments(arguments)
            return self._result("Resolved the requested guild member.", self._member_payload(member))

        if action == "avatar":
            member = await self._member_from_arguments(arguments)
            return self._result(
                "Resolved the requested member avatar without visually analyzing it.",
                self._avatar_payload(member),
            )

        if action == "emojis":
            emojis = [
                {
                    "id": str(emoji.id),
                    "name": emoji.name,
                    "animated": emoji.animated,
                    "available": emoji.available,
                    "applicationOwned": False,
                }
                for emoji in guild.emojis
            ]
            fetch_application_emojis = getattr(self.bot, "fetch_application_emojis", None)
            application_emojis = (
                await fetch_application_emojis()
                if callable(fetch_application_emojis)
                else list(getattr(self.bot, "application_emojis", []))
            )
            application_payload = [
                {**self._emoji_payload(emoji), "applicationOwned": True}
                for emoji in application_emojis
            ]
            return self._result(
                f"Found {len(emojis)} guild emoji(s) and {len(application_payload)} application emoji(s).",
                {"guildEmojis": emojis, "applicationEmojis": application_payload},
            )

        if action == "stickers":
            stickers = [self._sticker_payload(sticker) for sticker in guild.stickers]
            return self._result(f"Found {len(stickers)} guild sticker(s).", stickers)

        if action == "threads":
            threads = [
                {
                    **self._channel_payload(thread),
                    "parentId": str(thread.parent_id) if thread.parent_id else None,
                    "archived": thread.archived,
                    "locked": thread.locked,
                    "messageCount": thread.message_count,
                }
                for thread in guild.threads
                if self._requester_can_view(thread)
            ]
            return self._result(f"Found {len(threads)} active thread(s) visible to the requester.", threads)

        if action == "scheduled_events":
            events = [
                {
                    "id": str(event.id),
                    "name": event.name,
                    "description": event.description,
                    "status": str(event.status),
                    "entityType": str(event.entity_type),
                    "channelId": str(event.channel_id) if event.channel_id else None,
                    "startTime": event.start_time.isoformat(),
                    "endTime": event.end_time.isoformat() if event.end_time else None,
                    "location": event.location,
                }
                for event in guild.scheduled_events
            ]
            return self._result(f"Found {len(events)} scheduled event(s).", events)

        if action == "invites":
            self._require_guild_manager()
            invites = await guild.invites()
            payload = [
                {
                    "code": invite.code,
                    "channelId": str(invite.channel.id) if invite.channel else None,
                    "creatorId": str(invite.inviter.id) if invite.inviter else None,
                    "maxAge": invite.max_age,
                    "maxUses": invite.max_uses,
                    "uses": invite.uses,
                    "temporary": invite.temporary,
                }
                for invite in invites[:100]
            ]
            return self._result(
                f"Found {len(invites)} invite(s).",
                payload,
                truncated=len(invites) > len(payload),
            )

        if action == "bans":
            self._require_guild_manager()
            limit = self._bounded_int(arguments.get("limit"), default=25, maximum=100)
            entries = []
            async for entry in guild.bans(limit=limit):
                entries.append(
                    {
                        "userId": str(entry.user.id),
                        "user": str(entry.user),
                        "reason": entry.reason,
                    }
                )
            return self._result(f"Found {len(entries)} ban entrie(s).", entries)

        if action == "cleanup_preview":
            self._require_owner()
            return await self._generated_cleanup_result(arguments, execute=False)

        if action == "recent_messages":
            channel = await self._message_channel(arguments.get("channel_id"))
            self._require_requester_channel_access(channel, history=True)
            limit = self._bounded_int(arguments.get("limit"), default=20, maximum=50)
            author = None
            if (
                arguments.get("user_ref") not in {None, ""}
                or arguments.get("user_id") not in {None, ""}
            ):
                author = await self._member_from_arguments(arguments)
            messages = []
            scan_limit = limit if author is None else min(500, max(limit * 10, 50))
            async for item in channel.history(limit=scan_limit):
                if author is not None and item.author.id != author.id:
                    continue
                messages.append(
                    {
                        "id": str(item.id),
                        "authorId": str(item.author.id),
                        "author": item.author.display_name,
                        "content": item.content[:1500],
                        "createdAt": item.created_at.isoformat(),
                        "attachmentNames": [attachment.filename for attachment in item.attachments[:10]],
                    }
                )
                if len(messages) >= limit:
                    break
            return self._result(f"Read {len(messages)} recent message(s).", messages)

        if action == "audit_log":
            self._require_guild_manager()
            limit = self._bounded_int(arguments.get("limit"), default=20, maximum=50)
            entries = []
            async for entry in guild.audit_logs(limit=limit):
                entries.append(
                    {
                        "id": str(entry.id),
                        "action": str(entry.action),
                        "actorId": str(entry.user.id) if entry.user else None,
                        "actor": str(entry.user) if entry.user else None,
                        "target": str(entry.target)[:300],
                        "reason": entry.reason,
                        "createdAt": entry.created_at.isoformat(),
                    }
                )
            return self._result(f"Read {len(entries)} audit-log entrie(s).", entries)

        raise DiscordToolError(f"unsupported Discord query: {action}")

    async def _manage(
        self,
        action: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        self._require_manage_action_authorized(action)
        guild = self._guild()
        if action == "delete_messages" and arguments.get("after_message_id") in {
            None,
            "",
        } and arguments.get("before_message_id") in {None, ""}:
            raise DiscordToolError(
                "delete_messages requires after_message_id or before_message_id"
            )
        if action in _DESTRUCTIVE_ACTIONS:
            await self._confirm_destructive_action(action, arguments)
            # Administrator roles can change while the reaction prompt is
            # waiting. Re-check immediately before the Discord mutation.
            self._require_manage_action_authorized(action)
        reason = self._audit_reason(arguments)

        if action == "send_message":
            channel = await self._message_channel(arguments.get("channel_id"))
            content = self._required_string(arguments, "content", maximum=1900)
            sent = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            return self._result("Discord message sent.", {"messageId": str(sent.id), "channelId": str(channel.id)})

        if action in {"create_text_channel", "create_voice_channel", "create_category"}:
            name = self._required_string(arguments, "name", maximum=100)
            category = await self._optional_category(arguments.get("category_id"))
            if action == "create_text_channel":
                created = await guild.create_text_channel(
                    name,
                    topic=self._optional_string(arguments.get("topic"), maximum=1024),
                    category=category,
                    reason=reason,
                )
            elif action == "create_voice_channel":
                created = await guild.create_voice_channel(name, category=category, reason=reason)
            else:
                created = await guild.create_category(name, reason=reason)
            return self._result("Discord channel created.", self._channel_payload(created))

        if action == "edit_channel":
            channel = await self._guild_channel(self._required_id(arguments, "channel_id"))
            kwargs: dict[str, object] = {"reason": reason}
            for key, limit in (("name", 100), ("topic", 1024)):
                if key in arguments:
                    kwargs[key] = self._optional_string(arguments.get(key), maximum=limit)
            if "slowmode_seconds" in arguments:
                kwargs["slowmode_delay"] = self._bounded_int(
                    arguments.get("slowmode_seconds"), default=0, maximum=21_600, minimum=0
                )
            if "nsfw" in arguments:
                kwargs["nsfw"] = bool(arguments.get("nsfw"))
            if "category_id" in arguments:
                kwargs["category"] = await self._optional_category(arguments.get("category_id"))
            if len(kwargs) == 1:
                raise DiscordToolError("edit_channel requires at least one editable field")
            updated = await channel.edit(**kwargs)
            return self._result("Discord channel updated.", self._channel_payload(updated))

        if action == "delete_channel":
            channel = await self._guild_channel(self._required_id(arguments, "channel_id"))
            payload = self._channel_payload(channel)
            await channel.delete(reason=reason)
            return self._result("Discord channel deleted.", payload)

        if action == "create_role":
            colour_kwargs = self._role_colour_kwargs(arguments, creating=True)
            icon_kwargs, icon_source = await self._role_icon_kwargs(
                arguments,
                allow_clear=False,
            )
            role = await guild.create_role(
                name=self._required_string(arguments, "name", maximum=100),
                permissions=self._permissions(arguments.get("permission_names")),
                hoist=bool(arguments.get("hoist", False)),
                mentionable=bool(arguments.get("mentionable", False)),
                reason=reason,
                **colour_kwargs,
                **icon_kwargs,
            )
            payload = self._role_payload(role)
            if icon_source is not None:
                payload["iconSource"] = icon_source
            return self._result("Discord role created.", payload)

        if action == "edit_role":
            role = self._role_from_arguments(arguments)
            self._require_editable_role(role)
            kwargs = {"reason": reason}
            if "name" in arguments:
                kwargs["name"] = self._required_string(arguments, "name", maximum=100)
            if "permission_names" in arguments:
                kwargs["permissions"] = self._permissions(arguments.get("permission_names"))
            kwargs.update(self._role_colour_kwargs(arguments, creating=False, role=role))
            icon_kwargs, icon_source = await self._role_icon_kwargs(
                arguments,
                allow_clear=True,
            )
            kwargs.update(icon_kwargs)
            if "hoist" in arguments:
                kwargs["hoist"] = bool(arguments.get("hoist"))
            if "mentionable" in arguments:
                kwargs["mentionable"] = bool(arguments.get("mentionable"))
            if len(kwargs) == 1:
                raise DiscordToolError("edit_role requires at least one editable field")
            updated = await role.edit(**kwargs)
            payload = self._role_payload(updated or role)
            if icon_source is not None:
                payload["iconSource"] = icon_source
            return self._result("Discord role updated.", payload)

        if action == "delete_role":
            role = self._role_from_arguments(arguments)
            self._require_editable_role(role)
            payload = self._role_payload(role)
            await role.delete(reason=reason)
            return self._result("Discord role deleted.", payload)

        if action in {"add_role", "remove_role"}:
            member = await self._member_from_arguments(arguments)
            role = self._role_from_arguments(arguments)
            self._require_editable_role(role)
            if action == "add_role":
                await member.add_roles(role, reason=reason)
            else:
                await member.remove_roles(role, reason=reason)
            return self._result(
                f"Discord role {'added' if action == 'add_role' else 'removed'}.",
                {"userId": str(member.id), "roleId": str(role.id)},
            )

        if action == "set_channel_permissions":
            channel = await self._guild_channel(self._required_id(arguments, "channel_id"))
            target_type = str(arguments.get("target_type") or "role").strip().casefold()
            if target_type not in {"role", "member"}:
                raise DiscordToolError("target_type must be role or member")
            target_id = self._required_id(arguments, "target_id")
            target = self._role(target_id) if target_type == "role" else await self._member(target_id)
            raw_values = arguments.get("permission_values")
            if not isinstance(raw_values, dict) or not raw_values:
                raise DiscordToolError("permission_values must be a non-empty object")
            overwrite = discord.PermissionOverwrite()
            valid_names = {name for name, _value in discord.Permissions.all()}
            for name, value in raw_values.items():
                if name not in valid_names or not (
                    value is True or value is False or value is None
                ):
                    raise DiscordToolError(f"invalid channel permission value: {name}")
                setattr(overwrite, name, value)
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)
            return self._result("Channel permission overwrite updated.", {"channelId": str(channel.id), "targetId": str(target_id)})

        if action == "timeout_member":
            member = await self._member_from_arguments(arguments)
            minutes = self._bounded_int(arguments.get("duration_minutes"), default=0, maximum=40_320, minimum=0)
            until = None if minutes == 0 else discord.utils.utcnow() + timedelta(minutes=minutes)
            await member.timeout(until, reason=reason)
            return self._result("Member timeout updated.", {"userId": str(member.id), "durationMinutes": minutes})

        if action in {"kick_member", "ban_member"}:
            member = await self._member_from_arguments(arguments)
            if action == "kick_member":
                await member.kick(reason=reason)
            else:
                delete_seconds = self._bounded_int(
                    arguments.get("delete_message_seconds"), default=0, maximum=604_800, minimum=0
                )
                await guild.ban(member, reason=reason, delete_message_seconds=delete_seconds)
            return self._result(f"Member {action.removesuffix('_member')} completed.", {"userId": str(member.id)})

        if action == "unban_member":
            user_id = await self._user_id_from_arguments(arguments)
            await guild.unban(discord.Object(id=user_id), reason=reason)
            return self._result("Member unbanned.", {"userId": str(user_id)})

        if action in {"delete_message", "pin_message", "unpin_message"}:
            channel = await self._message_channel(arguments.get("channel_id"))
            target = await channel.fetch_message(self._required_id(arguments, "message_id"))
            if action == "delete_message":
                await target.delete()
            elif action == "pin_message":
                await target.pin(reason=reason)
            else:
                await target.unpin(reason=reason)
            return self._result(f"Discord {action.replace('_', ' ')} completed.", {"messageId": str(target.id), "channelId": str(channel.id)})

        if action == "delete_messages":
            channel = await self._message_channel(arguments.get("channel_id"))
            if not hasattr(channel, "purge"):
                raise DiscordToolError("message range deletion is unavailable in this channel")

            raw_after = arguments.get("after_message_id")
            raw_before = arguments.get("before_message_id")
            after_id = (
                self._coerce_id(raw_after, "after_message_id")
                if raw_after not in {None, ""}
                else None
            )
            before_id = (
                self._coerce_id(raw_before, "before_message_id")
                if raw_before not in {None, ""}
                else None
            )
            if after_id is not None and before_id is not None and after_id >= before_id:
                raise DiscordToolError(
                    "after_message_id must be older than before_message_id"
                )

            limit = self._bounded_int(
                arguments.get("limit"), default=5000, maximum=5000, minimum=1
            )
            raw_author_id = arguments.get("author_id")
            author_id = (
                self._coerce_id(raw_author_id, "author_id")
                if raw_author_id not in {None, ""}
                else None
            )

            def matches(item: discord.Message) -> bool:
                return author_id is None or item.author.id == author_id

            purge_kwargs: dict[str, object] = {
                "limit": limit,
                "oldest_first": after_id is not None,
                "reason": reason,
                "bulk": True,
            }
            if after_id is not None:
                purge_kwargs["after"] = discord.Object(id=after_id)
            if before_id is not None:
                purge_kwargs["before"] = discord.Object(id=before_id)
            if author_id is not None:
                purge_kwargs["check"] = matches

            deleted = list(await channel.purge(**purge_kwargs))
            deleted_ids = {int(item.id) for item in deleted}

            boundary_specs = (
                (after_id, bool(arguments.get("include_after"))),
                (before_id, bool(arguments.get("include_before"))),
            )
            for boundary_id, include_boundary in boundary_specs:
                if (
                    boundary_id is None
                    or not include_boundary
                    or boundary_id in deleted_ids
                ):
                    continue
                boundary = await channel.fetch_message(boundary_id)
                if not matches(boundary):
                    continue
                await boundary.delete()
                deleted.append(boundary)
                deleted_ids.add(boundary_id)

            ordered_ids = [str(item.id) for item in deleted]
            return self._result(
                f"Deleted {len(deleted)} Discord message(s) in the requested range.",
                {
                    "channelId": str(channel.id),
                    "deletedCount": len(deleted),
                    "deletedMessageIds": ordered_ids[:100],
                    "afterMessageId": str(after_id) if after_id is not None else None,
                    "beforeMessageId": str(before_id) if before_id is not None else None,
                    "includedAfter": bool(arguments.get("include_after")),
                    "includedBefore": bool(arguments.get("include_before")),
                    "authorId": str(author_id) if author_id is not None else None,
                    "possiblyTruncated": len(deleted) >= limit,
                },
                truncated=len(ordered_ids) > 100,
            )

        if action in {"add_reaction", "remove_reaction"}:
            channel = await self._message_channel(arguments.get("channel_id"))
            target = await channel.fetch_message(self._required_id(arguments, "message_id"))
            emoji = await self._reaction_emoji(self._required_string(arguments, "emoji", maximum=100))
            if action == "add_reaction":
                await target.add_reaction(emoji)
            else:
                if self.bot.user is None:
                    raise DiscordToolError("bot identity is unavailable")
                await target.remove_reaction(emoji, self.bot.user)
            return self._result(
                f"Discord {action.replace('_', ' ')} completed.",
                {"messageId": str(target.id), "channelId": str(channel.id), "emoji": str(emoji)},
            )

        if action == "create_thread":
            channel = await self._message_channel(arguments.get("channel_id"))
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                raise DiscordToolError("threads can only be created in text or forum channels")
            name = self._required_string(arguments, "name", maximum=100)
            if isinstance(channel, discord.ForumChannel):
                content = self._required_string(arguments, "content", maximum=1900)
                created = await channel.create_thread(
                    name=name,
                    content=content,
                    reason=reason,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                thread = created.thread
            else:
                raw_message_id = arguments.get("message_id")
                if raw_message_id not in {None, ""}:
                    starter = await channel.fetch_message(self._coerce_id(raw_message_id, "message_id"))
                    thread = await starter.create_thread(name=name, reason=reason)
                else:
                    thread = await channel.create_thread(name=name, reason=reason)
            return self._result("Discord thread created.", self._channel_payload(thread))

        if action == "edit_member":
            member = await self._member_from_arguments(arguments)
            kwargs: dict[str, object] = {"reason": reason}
            if "nickname" in arguments:
                kwargs["nick"] = self._optional_string(arguments.get("nickname"), maximum=32)
            if "voice_channel_id" in arguments:
                raw_voice_id = arguments.get("voice_channel_id")
                if raw_voice_id in {None, ""}:
                    kwargs["voice_channel"] = None
                else:
                    voice = await self._guild_channel(self._coerce_id(raw_voice_id, "voice_channel_id"))
                    if not isinstance(voice, (discord.VoiceChannel, discord.StageChannel)):
                        raise DiscordToolError("voice_channel_id is not a voice or stage channel")
                    kwargs["voice_channel"] = voice
            if "mute" in arguments:
                kwargs["mute"] = bool(arguments.get("mute"))
            if "deafen" in arguments:
                kwargs["deafen"] = bool(arguments.get("deafen"))
            if len(kwargs) == 1:
                raise DiscordToolError("edit_member requires nickname or voice-state fields")
            updated = await member.edit(**kwargs)
            return self._result("Discord member updated.", self._member_payload(updated or member))

        if action == "create_guild_emojis":
            source_type = str(
                arguments.get("source_type") or "current_attachment"
            ).strip().casefold()
            if source_type not in {"current_attachment", "message_attachment"}:
                raise DiscordToolError(
                    "create_guild_emojis batch mode requires current_attachment or "
                    "message_attachment; use create_guild_emoji repeatedly for other sources"
                )

            target = self.message
            if source_type == "message_attachment":
                channel = await self._message_channel(arguments.get("channel_id"))
                self._require_requester_channel_access(channel, history=True)
                target = await channel.fetch_message(
                    self._required_id(arguments, "message_id")
                )
            indexed_attachments = [
                (index, attachment)
                for index, attachment in enumerate(getattr(target, "attachments", []))
                if self._attachment_is_visual(attachment)
            ]
            if not indexed_attachments:
                raise DiscordToolError(
                    "the selected Discord message has no visual attachments to upload"
                )
            if len(indexed_attachments) > 25:
                raise DiscordToolError(
                    "one batch may contain at most 25 visual attachments"
                )

            names = self._batch_emoji_names(
                arguments.get("emoji_names"),
                [attachment for _index, attachment in indexed_attachments],
            )
            roles = self._roles_from_ids(arguments.get("role_ids"))
            successes: list[dict[str, object]] = []
            failures: list[dict[str, object]] = []
            for (attachment_index, attachment), name in zip(
                indexed_attachments,
                names,
                strict=True,
            ):
                item_arguments = dict(arguments)
                item_arguments["attachment_index"] = attachment_index
                try:
                    image, source = await self._read_media_source(
                        item_arguments,
                        maximum=_EMOJI_MAX_BYTES,
                        upload_kind="emoji",
                    )
                    created = await guild.create_custom_emoji(
                        name=name,
                        image=image,
                        roles=roles,
                        reason=reason,
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "name": name,
                            "filename": str(getattr(attachment, "filename", "")),
                            "error": (str(exc).strip() or exc.__class__.__name__)[:300],
                        }
                    )
                    continue
                successes.append(
                    {
                        **self._emoji_payload(created),
                        "source": source,
                    }
                )
            if not successes:
                details = "; ".join(
                    f"{item['name']}: {item['error']}" for item in failures[:3]
                )
                raise DiscordToolError(
                    f"all {len(failures)} emoji uploads failed: {details}"
                )
            return self._result(
                f"Created {len(successes)} of {len(indexed_attachments)} Discord emoji(s).",
                {
                    "created": successes,
                    "failed": failures,
                    "requestedCount": len(indexed_attachments),
                    "createdCount": len(successes),
                    "failedCount": len(failures),
                },
            )

        if action == "steal_message_assets":
            channel = await self._message_channel(arguments.get("channel_id"))
            raw_message_id = arguments.get("message_id")
            if raw_message_id in {None, ""}:
                source_message = self.message
                channel = self.message.channel
            else:
                message_id = self._coerce_id(raw_message_id, "message_id")
                if message_id == self.message.id and channel.id == self.message.channel.id:
                    source_message = self.message
                else:
                    self._require_requester_channel_access(channel, history=True)
                    source_message = await channel.fetch_message(message_id)
            asset_kind = str(arguments.get("asset_kind") or "all").strip().casefold()
            if asset_kind not in {"all", "emoji", "sticker"}:
                raise DiscordToolError("asset_kind must be all, emoji, or sticker")
            steal_cog = self.bot.get_cog("EmojiStealCog")
            if steal_cog is None:
                raise DiscordToolError("the emoji/sticker import module is not loaded")
            extract_emojis = getattr(steal_cog, "extract_custom_emojis", None)
            extract_stickers = getattr(steal_cog, "extract_stickers", None)
            copy_assets = getattr(steal_cog, "copy_assets_to_guild", None)
            if not all(callable(item) for item in (extract_emojis, extract_stickers, copy_assets)):
                raise DiscordToolError("the emoji/sticker import module is unavailable")
            assets = []
            if asset_kind in {"all", "emoji"}:
                assets.extend(extract_emojis(source_message.content))
            if asset_kind in {"all", "sticker"}:
                assets.extend(extract_stickers(source_message))
            if not assets:
                raise DiscordToolError(
                    "the selected message contains no custom emoji or supported sticker"
                )
            if len(assets) > 25:
                raise DiscordToolError(
                    "one Agent import may contain at most 25 emoji/sticker assets"
                )
            successes, failures = await copy_assets(
                guild,
                assets,
                reason_prefix=f"Imported by ATRI Agent for user {self.message.author.id}",
            )
            if not successes:
                detail = "; ".join(str(item) for item in failures[:3])
                raise DiscordToolError(
                    f"all {len(assets)} emoji/sticker imports failed: {detail}"
                )
            return self._result(
                f"Imported {len(successes)} of {len(assets)} emoji/sticker asset(s) into the current guild.",
                {
                    "sourceMessageId": str(source_message.id),
                    "sourceChannelId": str(channel.id),
                    "imported": successes,
                    "failed": failures,
                    "requestedCount": len(assets),
                    "importedCount": len(successes),
                    "failedCount": len(failures),
                },
            )

        if action in {"create_guild_emoji", "create_application_emoji"}:
            image, source = await self._read_media_source(
                arguments,
                maximum=_EMOJI_MAX_BYTES,
                upload_kind="emoji",
            )
            name = self._required_string(arguments, "name", maximum=32)
            if action == "create_application_emoji":
                created = await self.bot.create_application_emoji(name=name, image=image)
                application_owned = True
            else:
                roles = self._roles_from_ids(arguments.get("role_ids"))
                created = await guild.create_custom_emoji(
                    name=name,
                    image=image,
                    roles=roles,
                    reason=reason,
                )
                application_owned = False
            return self._result(
                "Discord emoji created.",
                {**self._emoji_payload(created), "applicationOwned": application_owned, "source": source},
            )

        if action in {"edit_guild_emoji", "edit_application_emoji"}:
            application_owned = action == "edit_application_emoji"
            emoji = await self._emoji(
                self._required_id(arguments, "emoji_id"),
                application_owned=application_owned,
            )
            kwargs: dict[str, object] = {}
            if "name" in arguments:
                kwargs["name"] = self._required_string(arguments, "name", maximum=32)
            if "role_ids" in arguments and not application_owned:
                kwargs["roles"] = self._roles_from_ids(arguments.get("role_ids"))
            if not application_owned:
                kwargs["reason"] = reason
            if not kwargs or (not application_owned and len(kwargs) == 1):
                raise DiscordToolError("edit emoji requires at least one editable field")
            updated = await emoji.edit(**kwargs)
            return self._result("Discord emoji updated.", self._emoji_payload(updated))

        if action in {"delete_guild_emoji", "delete_application_emoji"}:
            emoji = await self._emoji(
                self._required_id(arguments, "emoji_id"),
                application_owned=action == "delete_application_emoji",
            )
            payload = self._emoji_payload(emoji)
            await emoji.delete(**({} if action == "delete_application_emoji" else {"reason": reason}))
            return self._result("Discord emoji deleted.", payload)

        if action == "create_sticker":
            image, source = await self._read_media_source(
                arguments,
                maximum=_STICKER_MAX_BYTES,
                upload_kind="sticker",
            )
            filename = self._sticker_filename(arguments, source)
            sticker = await guild.create_sticker(
                name=self._required_string(arguments, "name", maximum=30),
                description=self._optional_string(arguments.get("description"), maximum=100) or "",
                emoji=self._required_string(arguments, "emoji", maximum=32),
                file=discord.File(io.BytesIO(image), filename=filename),
                reason=reason,
            )
            return self._result(
                "Discord sticker created.",
                {**self._sticker_payload(sticker), "source": source},
            )

        if action == "edit_sticker":
            sticker = self._sticker(self._required_id(arguments, "sticker_id"))
            kwargs: dict[str, object] = {"reason": reason}
            if "name" in arguments:
                kwargs["name"] = self._required_string(arguments, "name", maximum=30)
            if "description" in arguments:
                kwargs["description"] = self._optional_string(arguments.get("description"), maximum=100) or ""
            if "emoji" in arguments:
                kwargs["emoji"] = self._required_string(arguments, "emoji", maximum=32)
            if len(kwargs) == 1:
                raise DiscordToolError("edit_sticker requires at least one editable field")
            updated = await sticker.edit(**kwargs)
            return self._result("Discord sticker updated.", self._sticker_payload(updated))

        if action == "delete_sticker":
            sticker = self._sticker(self._required_id(arguments, "sticker_id"))
            payload = self._sticker_payload(sticker)
            await sticker.delete(reason=reason)
            return self._result("Discord sticker deleted.", payload)

        if action == "cleanup_generated_files":
            return await self._generated_cleanup_result(arguments, execute=True)

        if action == "edit_guild":
            kwargs = {"reason": reason}
            if "name" in arguments:
                kwargs["name"] = self._required_string(arguments, "name", maximum=100)
            if "description" in arguments:
                kwargs["description"] = self._optional_string(arguments.get("description"), maximum=120)
            if len(kwargs) == 1:
                raise DiscordToolError("edit_guild requires at least one editable field")
            updated = await guild.edit(**kwargs)
            return self._result("Discord guild updated.", {"id": str(updated.id), "name": updated.name})

        raise DiscordToolError(f"unsupported Discord management action: {action}")

    def _guild(self) -> discord.Guild:
        guild = self.message.guild
        if guild is None:
            raise DiscordToolError("Discord tools are unavailable in DMs")
        return guild

    def _is_owner(self) -> bool:
        return self.message.author.id == self.owner_user_id

    def _requester_member(self) -> object:
        guild = self._guild()
        getter = getattr(guild, "get_member", None)
        member = getter(self.message.author.id) if callable(getter) else None
        return member if member is not None else self.message.author

    def _is_guild_owner(self) -> bool:
        return self.message.author.id == self._guild().owner_id

    def _is_guild_administrator(self) -> bool:
        permissions = getattr(self._requester_member(), "guild_permissions", None)
        return bool(getattr(permissions, "administrator", False))

    def _can_manage_guild(self) -> bool:
        return self._is_owner() or self._is_guild_owner() or self._is_guild_administrator()

    def _require_owner(self) -> None:
        if not self._is_owner():
            raise DiscordToolError("this application-level Discord action is bot-owner-only")

    def _require_guild_manager(self) -> None:
        if not self._can_manage_guild():
            raise DiscordToolError(
                "Discord server management requires the bot owner, current guild owner, "
                "or Administrator permission in this guild"
            )

    def _require_manage_action_authorized(self, action: str) -> None:
        if action in _BOT_OWNER_ONLY_MANAGE_ACTIONS:
            self._require_owner()
        else:
            self._require_guild_manager()

    async def _confirm_destructive_action(
        self,
        action: str,
        arguments: dict[str, object],
    ) -> None:
        scope = self._destructive_confirmation_scope(action, arguments)
        if scope in self._confirmed_destructive_scopes:
            return
        handler = self.confirmation_handler
        if handler is None:
            raise DiscordToolError("reaction confirmation is unavailable for this Discord turn")
        confirmed = await handler(action, dict(arguments))
        if not confirmed:
            raise DiscordToolError(
                "destructive Discord action was not confirmed by the authorized requester's reaction"
            )
        self._confirmed_destructive_scopes.add(scope)

    def _destructive_confirmation_scope(
        self,
        action: str,
        arguments: dict[str, object],
    ) -> str:
        # Audit prose does not alter the affected Discord objects. Range
        # pagination also does not change its geometric/author boundary and may
        # vary during a zero-result retry. Normalize omitted current-channel
        # values so an equivalent explicit retry reuses the same approval.
        if action == "delete_messages":
            raw_channel_id = arguments.get("channel_id")
            current_channel_id = getattr(getattr(self.message, "channel", None), "id", None)
            channel_id = (
                self._coerce_id(raw_channel_id, "channel_id")
                if raw_channel_id not in {None, ""}
                else current_channel_id
            )
            bounded_arguments = {
                "channel_id": str(channel_id) if channel_id is not None else None,
                "after_message_id": str(arguments.get("after_message_id") or "") or None,
                "before_message_id": str(arguments.get("before_message_id") or "") or None,
                "include_after": bool(arguments.get("include_after")),
                "include_before": bool(arguments.get("include_before")),
                "author_id": str(arguments.get("author_id") or "") or None,
            }
        elif action == "delete_message":
            raw_channel_id = arguments.get("channel_id")
            current_channel_id = getattr(getattr(self.message, "channel", None), "id", None)
            channel_id = (
                self._coerce_id(raw_channel_id, "channel_id")
                if raw_channel_id not in {None, ""}
                else current_channel_id
            )
            bounded_arguments = {
                "channel_id": str(channel_id) if channel_id is not None else None,
                "message_id": str(arguments.get("message_id") or "") or None,
            }
        else:
            bounded_arguments = {
                str(key): value
                for key, value in arguments.items()
                if str(key) != "reason"
            }
        try:
            encoded = json.dumps(
                {"action": action, "arguments": bounded_arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DiscordToolError(
                "destructive Discord arguments are not serializable"
            ) from exc
        return encoded

    def _requester_can_view(self, channel: discord.abc.GuildChannel) -> bool:
        requester = self._guild().get_member(self.message.author.id)
        return requester is not None and channel.permissions_for(requester).view_channel

    def _require_requester_channel_access(self, channel: Any, *, history: bool = False) -> None:
        requester = self._guild().get_member(self.message.author.id)
        if requester is None and getattr(self.message.author, "guild", None) is self._guild():
            requester = self.message.author
        permission_channel = channel
        permission_getter = getattr(permission_channel, "permissions_for", None)
        if not callable(permission_getter):
            parent = getattr(channel, "parent", None)
            parent_getter = getattr(parent, "permissions_for", None)
            if callable(parent_getter):
                permission_channel = parent
                permission_getter = parent_getter
        if requester is None or not callable(permission_getter):
            raise DiscordToolError("requester channel permissions are unavailable")
        permissions = permission_getter(requester)
        if not permissions.view_channel or (history and not permissions.read_message_history):
            raise DiscordToolError("the requester cannot access that channel")

    async def _guild_channel(self, channel_id: int) -> discord.abc.GuildChannel:
        channel = self._guild().get_channel(channel_id)
        if channel is None:
            raise DiscordToolError("channel was not found in the current guild")
        return channel

    async def _message_channel(self, raw_channel_id: object) -> Any:
        channel_id = self.message.channel.id if raw_channel_id in {None, ""} else self._coerce_id(raw_channel_id, "channel_id")
        channel = self._guild().get_channel_or_thread(channel_id)
        if channel is None or not hasattr(channel, "history") or not hasattr(channel, "send"):
            raise DiscordToolError("message channel was not found in the current guild")
        return channel

    async def _optional_category(self, raw_category_id: object) -> discord.CategoryChannel | None:
        if raw_category_id in {None, ""}:
            return None
        category = await self._guild_channel(self._coerce_id(raw_category_id, "category_id"))
        if not isinstance(category, discord.CategoryChannel):
            raise DiscordToolError("category_id is not a category in the current guild")
        return category

    async def _member(self, user_id: int) -> discord.Member:
        guild = self._guild()
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise DiscordToolError("member was not found in the current guild") from exc
        return member

    async def _member_from_arguments(
        self,
        arguments: dict[str, object],
    ) -> discord.Member:
        raw = arguments.get("user_ref")
        if raw in {None, ""}:
            raw = arguments.get("user_id")
        return await self._resolve_member_ref(raw)

    async def _user_id_from_arguments(
        self,
        arguments: dict[str, object],
    ) -> int:
        raw = arguments.get("user_ref")
        if raw in {None, ""}:
            raw = arguments.get("user_id")
        text = str(raw or "").strip()
        mention = re.fullmatch(r"<@!?(\d+)>", text)
        if mention:
            text = mention.group(1)
        if text.isdecimal():
            return self._coerce_id(text, "user_ref")
        return (await self._resolve_member_ref(text)).id

    async def _resolve_member_ref(self, raw: object) -> discord.Member:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return await self._member(self._coerce_id(raw, "user_ref"))
        text = str(raw or "").strip()
        if not text:
            raise DiscordToolError(
                "user_ref must be a member name, display name, mention, or quoted snowflake"
            )
        mention = re.fullmatch(r"<@!?(\d+)>", text)
        if mention:
            text = mention.group(1)
        if text.isdecimal():
            return await self._member(self._coerce_id(text, "user_ref"))

        query = text.removeprefix("@").casefold()
        members = list(getattr(self._guild(), "members", []))
        exact = [
            member
            for member in members
            if query
            in {
                str(getattr(member, "name", "") or "").casefold(),
                str(getattr(member, "display_name", "") or "").casefold(),
                str(getattr(member, "global_name", "") or "").casefold(),
            }
        ]
        if len(exact) == 1:
            return exact[0]
        partial = [
            member
            for member in members
            if query
            and any(
                query in candidate
                for candidate in (
                    str(getattr(member, "name", "") or "").casefold(),
                    str(getattr(member, "display_name", "") or "").casefold(),
                    str(getattr(member, "global_name", "") or "").casefold(),
                )
                if candidate
            )
        ]
        matches = exact or partial
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DiscordToolError(
                "user_ref matches multiple current-guild members; call members with query first"
            )
        raise DiscordToolError("user_ref did not match a member in the current guild")

    def _role_from_arguments(self, arguments: dict[str, object]) -> discord.Role:
        raw = arguments.get("role_ref")
        if raw in {None, ""}:
            raw = arguments.get("role_id")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return self._role(self._coerce_id(raw, "role_ref"))
        text = str(raw or "").strip()
        if not text:
            raise DiscordToolError(
                "role_ref must be a role name, mention, or quoted snowflake"
            )
        mention = re.fullmatch(r"<@&(\d+)>", text)
        if mention:
            text = mention.group(1)
        if text.isdecimal():
            return self._role(self._coerce_id(text, "role_ref"))
        query = text.removeprefix("@").casefold()
        roles = list(getattr(self._guild(), "roles", []))
        exact = [
            role
            for role in roles
            if str(getattr(role, "name", "") or "").casefold() == query
        ]
        partial = [
            role
            for role in roles
            if query in str(getattr(role, "name", "") or "").casefold()
        ]
        matches = exact or partial
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DiscordToolError(
                "role_ref matches multiple current-guild roles; call roles first"
            )
        raise DiscordToolError("role_ref did not match a role in the current guild")

    async def resolve_visual_sources(
        self,
        arguments: dict[str, object],
    ) -> list[dict[str, str]]:
        """Resolve host-trusted Discord media without exposing arbitrary URL fetching."""

        source_type = str(arguments.get("source_type") or "").strip().casefold()
        if not source_type:
            source_type = (
                "avatar"
                if (
                    arguments.get("user_ref") not in {None, ""}
                    or arguments.get("user_id") not in {None, ""}
                )
                else "current_attachment"
            )

        if source_type == "avatar":
            member = await self._member_from_arguments(arguments)
            return [{"label": f"avatar of {member.display_name}", "url": self._avatar_url(member, size=1024)}]

        if source_type in {"current_attachment", "message_attachment"}:
            target = self.message
            if source_type == "message_attachment":
                channel = await self._message_channel(arguments.get("channel_id"))
                self._require_requester_channel_access(channel, history=True)
                target = await channel.fetch_message(self._required_id(arguments, "message_id"))
            attachments = [
                attachment
                for attachment in getattr(target, "attachments", [])
                if self._attachment_is_visual(attachment)
            ]
            if not attachments:
                raise DiscordToolError("the selected Discord message has no visual attachments")
            raw_index = arguments.get("attachment_index")
            if raw_index in {None, ""}:
                selected = attachments[:4]
            else:
                index = self._bounded_int(raw_index, default=0, minimum=0, maximum=len(attachments) - 1)
                selected = [attachments[index]]
            return [
                {"label": f"attachment {attachment.filename}", "url": attachment.url or attachment.proxy_url}
                for attachment in selected
            ]

        if source_type == "emoji":
            emoji_id = self._required_id(arguments, "emoji_id")
            try:
                emoji = await self._emoji(emoji_id, application_owned=False)
            except DiscordToolError:
                emoji = await self._emoji(emoji_id, application_owned=True)
            return [{"label": f"emoji {emoji.name}", "url": str(emoji.url)}]

        if source_type == "sticker":
            sticker = self._sticker(self._required_id(arguments, "sticker_id"))
            if sticker.format == discord.StickerFormatType.lottie:
                url = (
                    f"https://media.discordapp.net/stickers/{sticker.id}.png"
                    "?size=320&quality=lossless"
                )
            else:
                url = str(sticker.url)
            return [{"label": f"sticker {sticker.name}", "url": url}]

        raise DiscordToolError(
            "source_type must be avatar, current_attachment, message_attachment, emoji, or sticker"
        )

    def _role_colour_kwargs(
        self,
        arguments: dict[str, object],
        *,
        creating: bool,
        role: discord.Role | None = None,
    ) -> dict[str, object]:
        """Translate a model-facing role style into discord.py's role colors."""

        raw_style = self._optional_string(
            arguments.get("role_color_style"),
            maximum=20,
        )
        style = raw_style.casefold() if raw_style else ""
        if style and style not in {"solid", "gradient", "holographic"}:
            raise DiscordToolError(
                "role_color_style must be solid, gradient, or holographic"
            )

        has_primary = arguments.get("color") not in {None, ""}
        has_secondary = arguments.get("secondary_color") not in {None, ""}
        has_tertiary = arguments.get("tertiary_color") not in {None, ""}
        if not style:
            if has_tertiary:
                style = "holographic"
            elif has_secondary:
                style = "gradient"
            elif has_primary:
                return {
                    "color": discord.Colour(
                        self._bounded_int(
                            arguments.get("color"),
                            default=0,
                            maximum=0xFFFFFF,
                            minimum=0,
                        )
                    )
                }
            elif creating:
                return {"color": discord.Colour.default()}
            else:
                return {}

        if style == "solid":
            result: dict[str, object] = {}
            if has_primary or creating:
                result["color"] = discord.Colour(
                    self._bounded_int(
                        arguments.get("color"),
                        default=0,
                        maximum=0xFFFFFF,
                        minimum=0,
                    )
                )
            if not creating:
                # Explicit solid style also converts an existing enhanced role
                # back to one color. American aliases are used because
                # discord.py distinguishes an explicit None from MISSING here.
                result["secondary_color"] = None
                result["tertiary_color"] = None
            return result

        self._require_guild_feature(
            "ENHANCED_ROLE_COLORS",
            "gradient and holographic role colors",
        )
        if style == "gradient":
            if has_tertiary:
                raise DiscordToolError(
                    "tertiary_color is reserved for Discord's holographic role preset"
                )
            if not has_secondary:
                raise DiscordToolError(
                    "gradient roles require secondary_color"
                )
            if has_primary:
                primary_value = self._bounded_int(
                    arguments.get("color"),
                    default=0,
                    maximum=0xFFFFFF,
                    minimum=0,
                )
            elif role is not None:
                primary_value = int(role.colour.value)
            else:
                raise DiscordToolError("gradient roles require color")
            secondary_value = self._bounded_int(
                arguments.get("secondary_color"),
                default=0,
                maximum=0xFFFFFF,
                minimum=0,
            )
            return {
                "color": discord.Colour(primary_value),
                "secondary_color": discord.Colour(secondary_value),
                "tertiary_color": None,
            }

        expected_fields = dict(
            zip(
                ("color", "secondary_color", "tertiary_color"),
                _HOLOGRAPHIC_ROLE_COLORS,
                strict=True,
            )
        )
        for field, expected in expected_fields.items():
            if arguments.get(field) in {None, ""}:
                continue
            supplied = self._bounded_int(
                arguments.get(field),
                default=expected,
                maximum=0xFFFFFF,
                minimum=0,
            )
            if supplied != expected:
                raise DiscordToolError(
                    "Discord holographic roles use the fixed official color preset; "
                    "omit custom color fields"
                )
        return {
            "color": discord.Colour(_HOLOGRAPHIC_ROLE_COLORS[0]),
            "secondary_color": discord.Colour(_HOLOGRAPHIC_ROLE_COLORS[1]),
            "tertiary_color": discord.Colour(_HOLOGRAPHIC_ROLE_COLORS[2]),
        }

    async def _role_icon_kwargs(
        self,
        arguments: dict[str, object],
        *,
        allow_clear: bool,
    ) -> tuple[dict[str, object], dict[str, str] | None]:
        clear_icon = arguments.get("clear_role_icon") is True
        unicode_emoji = self._optional_string(
            arguments.get("unicode_emoji"),
            maximum=32,
        )
        media_requested = any(
            arguments.get(key) not in {None, ""}
            for key in _ROLE_ICON_MEDIA_ARGUMENTS
        )
        selected_count = sum((clear_icon, unicode_emoji is not None, media_requested))
        if selected_count > 1:
            raise DiscordToolError(
                "choose only one role icon source, unicode_emoji, or clear_role_icon"
            )
        if clear_icon:
            if not allow_clear:
                raise DiscordToolError("clear_role_icon is only valid when editing a role")
            return {"display_icon": None}, {"type": "cleared"}
        if unicode_emoji is not None:
            self._require_guild_feature("ROLE_ICONS", "role icons")
            if unicode_emoji.startswith("<") and unicode_emoji.endswith(">"):
                raise DiscordToolError(
                    "unicode_emoji must be a Unicode emoji; use source_type=emoji for a custom emoji"
                )
            return {"display_icon": unicode_emoji}, {"type": "unicode_emoji"}
        if not media_requested:
            return {}, None

        self._require_guild_feature("ROLE_ICONS", "role icons")
        media_arguments = dict(arguments)
        if media_arguments.get("source_type") in {None, ""}:
            if media_arguments.get("source_url") not in {None, ""}:
                media_arguments["source_type"] = "external_url"
            elif media_arguments.get("source_emoji_id") not in {None, ""}:
                media_arguments["source_type"] = "emoji"
            elif media_arguments.get("source_sticker_id") not in {None, ""}:
                media_arguments["source_type"] = "sticker"
            else:
                media_arguments["source_type"] = "current_attachment"
        image, source = await self._read_media_source(
            media_arguments,
            maximum=_ROLE_ICON_MAX_BYTES,
            upload_kind="role_icon",
        )
        return {"display_icon": image}, source

    def _require_guild_feature(self, feature: str, capability: str) -> None:
        available = {
            str(value).strip().upper()
            for value in getattr(self._guild(), "features", [])
        }
        if feature not in available:
            raise DiscordToolError(
                f"this Discord server does not expose {capability} ({feature} is unavailable)"
            )

    async def _read_media_source(
        self,
        arguments: dict[str, object],
        *,
        maximum: int,
        upload_kind: str,
    ) -> tuple[bytes, dict[str, str]]:
        source_type = str(arguments.get("source_type") or "current_attachment").strip().casefold()
        filename = "media.png"
        content_type = ""

        if source_type in {"current_attachment", "message_attachment"}:
            target = self.message
            if source_type == "message_attachment":
                channel = await self._message_channel(arguments.get("channel_id"))
                self._require_requester_channel_access(channel, history=True)
                target = await channel.fetch_message(self._required_id(arguments, "message_id"))
            attachments = list(getattr(target, "attachments", []))
            if not attachments:
                raise DiscordToolError("the selected Discord message has no attachment to upload")
            index = self._bounded_int(
                arguments.get("attachment_index"),
                default=0,
                minimum=0,
                maximum=len(attachments) - 1,
            )
            attachment = attachments[index]
            attachment_size = int(getattr(attachment, "size", 0) or 0)
            if attachment_size > _MEDIA_SOURCE_MAX_BYTES:
                raise DiscordToolError(
                    f"the selected media source is too large ({attachment_size} bytes; "
                    f"source limit {_MEDIA_SOURCE_MAX_BYTES})"
                )
            data = await attachment.read(use_cached=True)
            filename = attachment.filename or filename
            content_type = attachment.content_type or ""
        elif source_type == "avatar":
            member = await self._member_from_arguments(arguments)
            asset = member.display_avatar.replace(size=512, format="png")
            data = await asset.read()
            filename = f"avatar-{member.id}.png"
            content_type = "image/png"
        elif source_type == "emoji":
            emoji_id = self._required_id(arguments, "source_emoji_id")
            try:
                emoji = await self._emoji(emoji_id, application_owned=False)
            except DiscordToolError:
                emoji = await self._emoji(emoji_id, application_owned=True)
            data = await emoji.read()
            filename = f"{emoji.name}.{'gif' if emoji.animated else 'png'}"
            content_type = "image/gif" if emoji.animated else "image/png"
        elif source_type == "sticker":
            sticker = self._sticker(self._required_id(arguments, "source_sticker_id"))
            data = await sticker.read()
            filename = f"{sticker.name}.png"
            content_type = "image/png"
        elif source_type == "external_url":
            source_url = self._required_string(arguments, "source_url", maximum=2000)
            data, filename, content_type = await self._download_public_media(
                source_url,
                maximum=_MEDIA_SOURCE_MAX_BYTES,
            )
        else:
            raise DiscordToolError(
                "source_type must be current_attachment, message_attachment, avatar, emoji, sticker, or external_url"
            )

        if not data:
            raise DiscordToolError("the selected media source was empty")
        if len(data) > _MEDIA_SOURCE_MAX_BYTES:
            raise DiscordToolError(
                f"the selected media source is too large ({len(data)} bytes; "
                f"source limit {_MEDIA_SOURCE_MAX_BYTES})"
            )
        original_size = len(data)
        data, filename, content_type, compressed = await asyncio.to_thread(
            self._prepare_media_for_upload,
            data,
            filename=filename,
            content_type=content_type,
            maximum=maximum,
            upload_kind=upload_kind,
        )
        return data, {
            "type": source_type,
            "filename": filename,
            "contentType": content_type,
            "autoCompressed": str(compressed).lower(),
            "originalBytes": str(original_size),
            "uploadBytes": str(len(data)),
        }

    @staticmethod
    def _prepare_media_for_upload(
        data: bytes,
        *,
        filename: str,
        content_type: str,
        maximum: int,
        upload_kind: str,
    ) -> tuple[bytes, str, str, bool]:
        if upload_kind not in {"emoji", "sticker", "role_icon"}:
            raise DiscordToolError("invalid Discord media upload kind")

        normalized_type = str(content_type or "").split(";", 1)[0].strip().casefold()
        lowered_filename = filename.casefold()
        looks_like_json = (
            normalized_type == "application/json"
            or lowered_filename.endswith(".json")
        )
        if looks_like_json:
            if upload_kind != "sticker":
                raise DiscordToolError("Lottie JSON can only be uploaded as a Discord sticker")
            if len(data) > maximum:
                raise DiscordToolError(
                    f"Lottie sticker exceeds the {maximum}-byte Discord limit and cannot be raster-compressed"
                )
            return data, filename, "application/json", False

        try:
            from PIL import Image, UnidentifiedImageError
        except ImportError as exc:
            if upload_kind == "role_icon":
                raise DiscordToolError(
                    "Pillow is required to validate and convert Discord role icons"
                ) from exc
            if len(data) > maximum:
                raise DiscordToolError(
                    "media exceeds the Discord upload limit and Pillow is unavailable for automatic compression"
                ) from exc
            return data, filename, normalized_type or "application/octet-stream", False

        try:
            with Image.open(io.BytesIO(data)) as source:
                source_format = str(source.format or "").upper()
                frame_count = int(getattr(source, "n_frames", 1) or 1)
                animated = bool(getattr(source, "is_animated", False) and frame_count > 1)
                width, height = source.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise DiscordToolError("the selected media is not a supported image") from exc

        if width <= 0 or height <= 0 or width * height > _MEDIA_MAX_PIXELS:
            raise DiscordToolError("the selected image dimensions are unsafe for automatic compression")
        if frame_count > _MEDIA_MAX_FRAMES:
            raise DiscordToolError(
                f"the animation has too many frames ({frame_count}; maximum {_MEDIA_MAX_FRAMES})"
            )

        emoji_native = source_format in {"PNG", "JPEG", "JPG", "GIF", "WEBP"}
        sticker_native = source_format == "PNG"
        role_icon_native = source_format in {"PNG", "JPEG", "JPG"} and not animated
        if len(data) <= maximum and (
            (upload_kind == "emoji" and emoji_native)
            or (upload_kind == "sticker" and sticker_native)
            or (upload_kind == "role_icon" and role_icon_native)
        ):
            if upload_kind == "sticker" and animated:
                return data, DiscordToolHost._replace_extension(filename, ".apng"), "image/apng", False
            return data, filename, normalized_type or DiscordToolHost._image_content_type(source_format), False

        try:
            with Image.open(io.BytesIO(data)) as source:
                if animated and upload_kind != "role_icon":
                    prepared = DiscordToolHost._compress_animated_media(
                        source,
                        maximum=maximum,
                        upload_kind=upload_kind,
                    )
                    extension = ".gif" if upload_kind == "emoji" else ".apng"
                    mime_type = "image/gif" if upload_kind == "emoji" else "image/apng"
                else:
                    if animated:
                        source.seek(0)
                    prepared = DiscordToolHost._compress_static_media(
                        source,
                        maximum=maximum,
                        upload_kind=upload_kind,
                    )
                    extension = ".png"
                    mime_type = "image/png"
        except DiscordToolError:
            raise
        except (OSError, ValueError) as exc:
            raise DiscordToolError("automatic Discord media compression failed") from exc

        return (
            prepared,
            DiscordToolHost._replace_extension(filename, extension),
            mime_type,
            True,
        )

    @staticmethod
    def _compress_static_media(source: Any, *, maximum: int, upload_kind: str) -> bytes:
        from PIL import Image

        sides = (
            (256, 192, 160, 128, 112, 96, 80, 64, 48)
            if upload_kind in {"emoji", "role_icon"}
            else (320, 288, 256, 224, 192, 160, 128, 96, 80)
        )
        colors = (256, 192, 128, 96, 64, 48, 32, 24, 16)
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        quantize_method = getattr(getattr(Image, "Quantize", Image), "FASTOCTREE")
        dither_none = getattr(getattr(Image, "Dither", Image), "NONE")
        base = source.convert("RGBA")

        for side, color_count in zip(sides, colors):
            frame = base.copy()
            frame.thumbnail((side, side), resample=resampling)
            quantized = frame.quantize(
                colors=color_count,
                method=quantize_method,
                dither=dither_none,
            )
            buffer = io.BytesIO()
            quantized.save(buffer, format="PNG", optimize=True, compress_level=9)
            candidate = buffer.getvalue()
            if candidate and len(candidate) <= maximum:
                return candidate
        raise DiscordToolError(
            f"image could not be compressed below the {maximum}-byte Discord limit"
        )

    @staticmethod
    def _compress_animated_media(source: Any, *, maximum: int, upload_kind: str) -> bytes:
        from PIL import Image

        if upload_kind == "emoji":
            plans = (
                (256, 80, 256), (192, 60, 160), (160, 48, 128),
                (128, 36, 96), (112, 24, 64), (96, 16, 48),
                (80, 12, 32), (64, 8, 24), (48, 6, 16),
            )
        else:
            plans = (
                (320, 60, 256), (288, 48, 192), (256, 36, 128),
                (224, 28, 96), (192, 22, 64), (160, 16, 48),
                (128, 12, 32), (96, 8, 24), (80, 6, 16),
            )

        frame_count = int(getattr(source, "n_frames", 1) or 1)
        loop_count = int(source.info.get("loop", 0) or 0)
        durations: list[int] = []
        for index in range(frame_count):
            source.seek(index)
            durations.append(max(int(source.info.get("duration", 100) or 100), 20))
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        quantize_method = getattr(getattr(Image, "Quantize", Image), "FASTOCTREE")
        dither_none = getattr(getattr(Image, "Dither", Image), "NONE")

        for side, frame_cap, color_count in plans:
            stride = max(1, (frame_count + frame_cap - 1) // frame_cap)
            indexes = list(range(0, frame_count, stride))[:frame_cap]
            frames: list[Any] = []
            sampled_durations: list[int] = []
            for position, index in enumerate(indexes):
                source.seek(index)
                frame = source.convert("RGBA")
                frame.thumbnail((side, side), resample=resampling)
                if upload_kind == "emoji":
                    frame = frame.quantize(
                        colors=color_count,
                        method=quantize_method,
                        dither=dither_none,
                    )
                frames.append(frame.copy())
                next_index = indexes[position + 1] if position + 1 < len(indexes) else frame_count
                sampled_durations.append(sum(durations[index:next_index]))
            if not frames:
                continue

            buffer = io.BytesIO()
            if upload_kind == "emoji":
                frames[0].save(
                    buffer,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=sampled_durations,
                    loop=loop_count,
                    optimize=True,
                    disposal=2,
                )
            else:
                frames[0].save(
                    buffer,
                    format="PNG",
                    save_all=True,
                    append_images=frames[1:],
                    duration=sampled_durations,
                    loop=loop_count,
                    optimize=True,
                    compress_level=9,
                    disposal=2,
                    blend=0,
                )
            candidate = buffer.getvalue()
            if candidate and len(candidate) <= maximum:
                return candidate
        raise DiscordToolError(
            f"animation could not be compressed below the {maximum}-byte Discord limit"
        )

    @staticmethod
    def _replace_extension(filename: str, extension: str) -> str:
        base = str(filename or "media").rsplit(".", 1)[0] or "media"
        return f"{base}{extension}"

    @staticmethod
    def _image_content_type(source_format: str) -> str:
        return {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "JPG": "image/jpeg",
            "GIF": "image/gif",
            "WEBP": "image/webp",
        }.get(source_format, "application/octet-stream")

    async def _download_public_media(
        self,
        raw_url: str,
        *,
        maximum: int,
    ) -> tuple[bytes, str, str]:
        current = self._validated_public_url(raw_url)
        timeout = aiohttp.ClientTimeout(
            total=_EXTERNAL_MEDIA_TIMEOUT_SECONDS,
            connect=min(_EXTERNAL_MEDIA_TIMEOUT_SECONDS, 8),
        )
        connector = aiohttp.TCPConnector(
            ssl=build_verified_ssl_context(),
            resolver=_PublicOnlyResolver(),
        )
        headers = {
            'User-Agent': 'ATRI-DiscordMedia/1.0',
            'Accept': 'image/avif,image/webp,image/apng,image/*,application/json;q=0.5',
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=headers,
            trust_env=False,
        ) as session:
            for redirect_index in range(_EXTERNAL_MEDIA_MAX_REDIRECTS + 1):
                try:
                    async with session.get(current, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            if redirect_index >= _EXTERNAL_MEDIA_MAX_REDIRECTS:
                                raise DiscordToolError('external media URL redirected too many times')
                            location = response.headers.get('Location', '').strip()
                            if not location:
                                raise DiscordToolError('external media redirect did not include a destination')
                            current = self._validated_public_url(str(current.join(URL(location))))
                            continue
                        if response.status != 200:
                            raise DiscordToolError(
                                f'external media download returned HTTP {response.status}'
                            )

                        content_type = (
                            response.headers.get('Content-Type', '')
                            .split(';', 1)[0]
                            .strip()
                            .casefold()
                        )
                        declared_size = response.content_length
                        if content_type in {'text/html', 'application/xhtml+xml'}:
                            if declared_size is not None and declared_size > _EXTERNAL_PAGE_MAX_BYTES:
                                raise DiscordToolError(
                                    'external page is too large to inspect for image metadata'
                                )
                            page_chunks: list[bytes] = []
                            page_bytes = 0
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                page_bytes += len(chunk)
                                if page_bytes > _EXTERNAL_PAGE_MAX_BYTES:
                                    raise DiscordToolError(
                                        'external page exceeded the metadata inspection limit'
                                    )
                                page_chunks.append(chunk)
                            charset = response.charset or 'utf-8'
                            page_text = b''.join(page_chunks).decode(charset, errors='replace')
                            discovered = self._discover_page_media_url(page_text, current)
                            if discovered is None:
                                raise DiscordToolError(
                                    'external page did not expose an Open Graph or Twitter image URL'
                                )
                            current = discovered
                            continue

                        if declared_size is not None and declared_size > maximum:
                            raise DiscordToolError(
                                f'external media is too large ({declared_size} bytes; maximum {maximum})'
                            )
                        filename = self._external_media_filename(
                            current,
                            response.content_disposition.filename
                            if response.content_disposition is not None
                            else None,
                            content_type,
                        )
                        if not self._looks_like_downloadable_media(content_type, filename):
                            raise DiscordToolError(
                                'external URL did not return an image or Lottie JSON asset'
                            )

                        chunks: list[bytes] = []
                        downloaded = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            downloaded += len(chunk)
                            if downloaded > maximum:
                                raise DiscordToolError(
                                    f'external media exceeded the {maximum}-byte limit while downloading'
                                )
                            chunks.append(chunk)
                        data = b''.join(chunks)
                        if not data:
                            raise DiscordToolError('external media download returned an empty body')
                        return data, filename, content_type
                except DiscordToolError:
                    raise
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    raise DiscordToolError(
                        f'external media download failed: {exc.__class__.__name__}'
                    ) from exc

        raise DiscordToolError('external media download did not complete')

    async def _generated_cleanup_result(
        self,
        arguments: dict[str, object],
        *,
        execute: bool,
    ) -> dict[str, object]:
        minimum_age_hours = self._bounded_int(
            arguments.get('minimum_age_hours'),
            default=24,
            minimum=1,
            maximum=8760,
        )
        cleaner = GeneratedArtifactCleaner(Path(__file__).resolve().parents[2])
        protection = collect_cleanup_protection(self.bot)
        operation = cleaner.clean if execute else cleaner.preview
        report = await asyncio.to_thread(
            operation,
            minimum_age_hours=minimum_age_hours,
            protection=protection,
        )
        payload = report.to_dict(include_items=50)
        if execute:
            summary = (
                f"Deleted {report.deleted_count} ATRI-generated cache/log artifact(s) "
                f"and freed {report.freed_bytes} bytes."
            )
        else:
            summary = (
                f"Previewed {len(report.candidates)} old ATRI-generated cache/log artifact(s) "
                f"using a {minimum_age_hours}-hour minimum age. No files were deleted."
            )
        return self._result(summary, payload, truncated=bool(payload.get('truncated')))

    @staticmethod
    def _validated_public_url(raw_url: str) -> URL:
        try:
            url = URL(raw_url.strip().strip('<>'))
        except (TypeError, ValueError) as exc:
            raise DiscordToolError('source_url is not a valid URL') from exc
        if url.scheme not in {'http', 'https'} or not url.host:
            raise DiscordToolError('source_url must use HTTP or HTTPS')
        if url.user is not None or url.password is not None:
            raise DiscordToolError('source_url must not contain embedded credentials')
        return url

    @classmethod
    def _discover_page_media_url(cls, page_text: str, page_url: URL) -> URL | None:
        parser = _PageMediaMetadataParser()
        try:
            parser.feed(page_text)
        except (ValueError, TypeError):
            return None
        for candidate in parser.candidates:
            try:
                return cls._validated_public_url(str(page_url.join(URL(candidate))))
            except DiscordToolError:
                continue
        return None

    @staticmethod
    def _external_media_filename(
        url: URL,
        suggested: str | None,
        content_type: str,
    ) -> str:
        raw_name = suggested or PurePosixPath(url.path).name or 'download'
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', raw_name).strip('._')[:120] or 'download'
        if '.' not in safe_name:
            extension = {
                'image/png': '.png',
                'image/apng': '.apng',
                'image/gif': '.gif',
                'image/jpeg': '.jpg',
                'image/webp': '.webp',
                'image/avif': '.avif',
                'application/json': '.json',
            }.get(content_type, '.bin')
            safe_name += extension
        return safe_name

    @staticmethod
    def _looks_like_downloadable_media(content_type: str, filename: str) -> bool:
        if content_type.startswith('image/') or content_type == 'application/json':
            return True
        lowered = filename.casefold()
        return lowered.endswith(
            ('.png', '.apng', '.gif', '.jpg', '.jpeg', '.webp', '.avif', '.json')
        )

    def _role(self, role_id: int) -> discord.Role:
        guild = self._guild()
        role = guild.get_role(role_id)
        if role is None:
            raise DiscordToolError("role was not found in the current guild")
        return role

    async def _emoji(self, emoji_id: int, *, application_owned: bool) -> discord.Emoji:
        if application_owned:
            try:
                emoji = await self.bot.fetch_application_emoji(emoji_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise DiscordToolError("application emoji was not found") from exc
            if not emoji.is_application_owned():
                raise DiscordToolError("emoji is not owned by this application")
            return emoji
        emoji = self._guild().get_emoji(emoji_id)
        if emoji is None:
            raise DiscordToolError("emoji was not found in the current guild")
        return emoji

    def _sticker(self, sticker_id: int) -> discord.GuildSticker:
        sticker = self._guild().get_sticker(sticker_id)
        if sticker is None:
            raise DiscordToolError("sticker was not found in the current guild")
        return sticker

    async def _reaction_emoji(self, value: str) -> str | discord.Emoji:
        raw = value.strip()
        try:
            emoji_id = int(raw)
        except ValueError:
            return raw
        try:
            return await self._emoji(emoji_id, application_owned=False)
        except DiscordToolError:
            return await self._emoji(emoji_id, application_owned=True)

    def _require_editable_role(self, role: discord.Role) -> None:
        if role.is_default() or role.managed:
            raise DiscordToolError("default and integration-managed roles cannot be edited")
        bot_member = self._guild().me
        if bot_member is None or bot_member.top_role <= role:
            raise DiscordToolError("the role is above the bot's editable role boundary")

    @staticmethod
    def _enabled_permissions(permissions: discord.Permissions) -> list[str]:
        return [name for name, enabled in permissions if enabled]

    @staticmethod
    def _member_payload(member: discord.Member) -> dict[str, object]:
        return {
            "id": str(member.id),
            "name": member.name,
            "displayName": member.display_name,
            "bot": member.bot,
            "joinedAt": member.joined_at.isoformat() if member.joined_at else None,
            "roles": [{"id": str(role.id), "name": role.name} for role in member.roles[1:]],
            "avatar": DiscordToolHost._avatar_payload(member),
        }

    @staticmethod
    def _avatar_url(member: discord.Member, *, size: int) -> str:
        asset = member.display_avatar
        format_name = "gif" if asset.is_animated() else "png"
        return str(asset.replace(size=size, format=format_name))

    @staticmethod
    def _avatar_payload(member: discord.Member) -> dict[str, object]:
        return {
            "userId": str(member.id),
            "displayName": member.display_name,
            "url": DiscordToolHost._avatar_url(member, size=1024),
            "animated": member.display_avatar.is_animated(),
            "guildSpecific": member.guild_avatar is not None,
        }

    @staticmethod
    def _emoji_payload(emoji: discord.Emoji) -> dict[str, object]:
        return {
            "id": str(emoji.id),
            "name": emoji.name,
            "animated": emoji.animated,
            "url": str(emoji.url),
            "applicationOwned": emoji.is_application_owned(),
        }

    @staticmethod
    def _sticker_payload(sticker: discord.GuildSticker) -> dict[str, object]:
        return {
            "id": str(sticker.id),
            "name": sticker.name,
            "description": sticker.description,
            "emoji": sticker.emoji,
            "format": str(sticker.format),
            "url": str(sticker.url),
        }

    @staticmethod
    def _channel_payload(channel: Any) -> dict[str, object]:
        return {"id": str(channel.id), "name": channel.name, "type": str(channel.type)}

    @staticmethod
    def _role_payload(role: discord.Role) -> dict[str, object]:
        primary = getattr(role, "colour", getattr(role, "color", None))
        secondary = getattr(
            role,
            "secondary_colour",
            getattr(role, "secondary_color", None),
        )
        tertiary = getattr(
            role,
            "tertiary_colour",
            getattr(role, "tertiary_color", None),
        )

        def colour_value(value: object) -> int | None:
            raw = getattr(value, "value", value)
            return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None

        secondary_value = colour_value(secondary)
        tertiary_value = colour_value(tertiary)
        style = (
            "holographic"
            if tertiary_value is not None
            else "gradient"
            if secondary_value is not None
            else "solid"
        )
        display_icon = getattr(role, "display_icon", None)
        return {
            "id": str(role.id),
            "name": role.name,
            "position": role.position,
            "colorStyle": style,
            "color": colour_value(primary),
            "secondaryColor": secondary_value,
            "tertiaryColor": tertiary_value,
            "displayIcon": str(display_icon) if display_icon is not None else None,
        }

    def _permissions(self, raw_names: object) -> discord.Permissions:
        if raw_names is None or raw_names == "":
            return discord.Permissions.none()
        if not isinstance(raw_names, list) or len(raw_names) > 80:
            raise DiscordToolError("permission_names must be an array")
        valid_names = {name for name, _value in discord.Permissions.all()}
        permissions = discord.Permissions.none()
        for raw_name in raw_names:
            name = str(raw_name or "").strip()
            if name not in valid_names:
                raise DiscordToolError(f"unknown Discord permission: {name}")
            setattr(permissions, name, True)
        return permissions

    def _roles_from_ids(self, raw_ids: object) -> list[discord.Role]:
        if raw_ids is None or raw_ids == "":
            return []
        if not isinstance(raw_ids, list) or len(raw_ids) > 100:
            raise DiscordToolError("role_ids must be an array")
        roles = [self._role(self._coerce_id(value, "role_id")) for value in raw_ids]
        return roles

    @staticmethod
    def _batch_emoji_names(
        raw_names: object,
        attachments: list[discord.Attachment],
    ) -> list[str]:
        if raw_names is not None and raw_names != "":
            if not isinstance(raw_names, list) or len(raw_names) != len(attachments):
                raise DiscordToolError(
                    "emoji_names must be an array matching the number of visual attachments"
                )
            candidates = [str(item or "").strip() for item in raw_names]
        else:
            candidates = [
                Path(str(getattr(attachment, "filename", "") or "emoji")).stem
                for attachment in attachments
            ]

        names: list[str] = []
        used: set[str] = set()
        for index, candidate in enumerate(candidates, start=1):
            base = re.sub(r"[^A-Za-z0-9_]+", "_", candidate).strip("_")
            if len(base) < 2:
                base = f"emoji_{index}"
            base = base[:32]
            name = base
            suffix_index = 2
            while name.casefold() in used:
                suffix = f"_{suffix_index}"
                name = f"{base[:32 - len(suffix)]}{suffix}"
                suffix_index += 1
            used.add(name.casefold())
            names.append(name)
        return names

    @staticmethod
    def _attachment_is_visual(attachment: discord.Attachment) -> bool:
        content_type = str(getattr(attachment, "content_type", "") or "").casefold()
        if content_type.startswith("image/"):
            return True
        filename = str(getattr(attachment, "filename", "") or "").casefold()
        return any(filename.endswith(suffix) for suffix in _VISUAL_ATTACHMENT_SUFFIXES)

    @staticmethod
    def _sticker_filename(
        arguments: dict[str, object],
        source: dict[str, str],
    ) -> str:
        requested = str(arguments.get("filename") or "").strip()
        filename = requested or source.get("filename", "sticker.png")
        lowered = filename.casefold()
        if lowered.endswith((".png", ".apng", ".json")):
            return filename
        content_type = source.get("contentType", "").casefold()
        if content_type in {"image/png", "image/apng"}:
            return f"{filename.rsplit('.', 1)[0]}.png"
        raise DiscordToolError("Discord stickers require a PNG, APNG, or Lottie JSON source")

    def _audit_reason(self, arguments: dict[str, object]) -> str:
        detail = self._optional_string(arguments.get("reason"), maximum=300) or "owner-requested Agent action"
        return f"ATRI owner {self.message.author.id}: {detail}"[:512]

    @staticmethod
    def _required_string(arguments: dict[str, object], key: str, *, maximum: int) -> str:
        value = str(arguments.get(key) or "").strip()
        if not value or len(value) > maximum:
            raise DiscordToolError(f"{key} must contain between 1 and {maximum} characters")
        return value

    @staticmethod
    def _optional_string(value: object, *, maximum: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if len(text) > maximum:
            raise DiscordToolError(f"text exceeds {maximum} characters")
        return text or None

    @classmethod
    def _required_id(cls, arguments: dict[str, object], key: str) -> int:
        return cls._coerce_id(arguments.get(key), key)

    @staticmethod
    def _coerce_id(value: object, label: str) -> int:
        if isinstance(value, bool):
            raise DiscordToolError(
                f"{label} must be a Discord snowflake id encoded as a quoted decimal string"
            )
        if isinstance(value, int) and value > 2**53 - 1:
            raise DiscordToolError(
                f"{label} is an unsafe JSON integer; retry with the exact Discord snowflake "
                "encoded as a quoted decimal string"
            )
        if isinstance(value, float):
            raise DiscordToolError(
                f"{label} must be a Discord snowflake id encoded as a quoted decimal string"
            )
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise DiscordToolError(f"{label} must be a Discord snowflake id") from exc
        if parsed <= 0 or parsed >= 2**64:
            raise DiscordToolError(f"{label} must be a positive Discord snowflake id")
        return parsed

    @staticmethod
    def _bounded_int(
        value: object,
        *,
        default: int,
        maximum: int,
        minimum: int = 1,
    ) -> int:
        if value is None or value == "":
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise DiscordToolError("numeric argument must be an integer")
        if not minimum <= value <= maximum:
            raise DiscordToolError(f"numeric argument must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _result(
        summary: str,
        payload: object,
        *,
        truncated: bool = False,
    ) -> dict[str, object]:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(content) > _RESULT_MAX_CHARS:
            content = content[:_RESULT_MAX_CHARS] + "...[truncated]"
            truncated = True
        return {"summary": summary, "content": content, "truncated": truncated}
