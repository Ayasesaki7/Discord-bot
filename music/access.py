"""Shared channel checks for UI and Agent music requests."""
import discord


def is_voice_channel(channel) -> bool:
    return getattr(channel, 'type', None) in {
        discord.ChannelType.voice, discord.ChannelType.stage_voice,
    }


def in_voice_chat(source, voice_channel) -> bool:
    return (is_voice_channel(source) and is_voice_channel(voice_channel)
            and getattr(source, 'id', None) == getattr(voice_channel, 'id', None))


def panel_destination(guild, data):
    voice_client = getattr(guild, 'voice_client', None)
    if voice_client and voice_client.is_connected():
        channel = voice_client.channel
    else:
        channel = data.get('channel')
    if not is_voice_channel(channel):
        return None
    owner_guild = getattr(channel, 'guild', None)
    if owner_guild is not None and getattr(owner_guild, 'id', None) != getattr(guild, 'id', None):
        return None
    return channel


async def reject_interaction(interaction, message):
    response = interaction.response
    if getattr(response, 'is_done', lambda: False)():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await response.send_message(message, ephemeral=True)
