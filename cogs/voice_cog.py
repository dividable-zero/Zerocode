import asyncio
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands, voice_recv

from config import ALLOWED_CHANNEL_ID, VOICE_REF_FILE, VOICE_REF_URL
from services.ai_client import voice_chat_histories
from services.tts_engine import prepare_reference_audio
from voice.dave_patch import apply_voice_recv_patch
from voice.sink import GuildVoiceSink, voice_busy_guilds


class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return ALLOWED_CHANNEL_ID is None or interaction.channel_id == ALLOWED_CHANNEL_ID

    @app_commands.command(name="join", description="Join your current voice channel and listen")
    @app_commands.describe(
        voice_url="Google Drive, YouTube, or direct audio/video link for the voice",
        start_sec="Optional start time in seconds",
        duration_sec="Optional length in seconds",
    )
    async def join_command(
        self,
        interaction: discord.Interaction,
        voice_url: Optional[str] = None,
        start_sec: Optional[float] = None,
        duration_sec: Optional[float] = None,
    ):
        if not self._allowed(interaction):
            await interaction.response.send_message("Not available in this channel.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        source_url = (voice_url or VOICE_REF_URL).strip()
        if source_url:
            try:
                await prepare_reference_audio(source_url, VOICE_REF_FILE, start_sec, duration_sec)
            except Exception as err:
                await interaction.followup.send(f"Failed to load voice link: {str(err)[:400]}", ephemeral=True)
                return

        if not VOICE_REF_FILE.exists():
            await interaction.followup.send(
                "No reference voice found. Pass `voice_url` in `/join` or set `VOICE_REF_URL` in `api.env`.",
                ephemeral=True,
            )
            return

        target_channel = interaction.user.voice.channel
        guild = interaction.guild
        try:
            apply_voice_recv_patch()
            current_vc = guild.voice_client if guild else None

            if isinstance(current_vc, voice_recv.VoiceRecvClient) and current_vc.is_connected():
                if current_vc.channel != target_channel:
                    await current_vc.move_to(target_channel)
                current_vc.listen(GuildVoiceSink(current_vc, asyncio.get_running_loop()))
            else:
                if current_vc and current_vc.is_connected():
                    await current_vc.disconnect(force=True)
                vc = await target_channel.connect(cls=voice_recv.VoiceRecvClient)
                vc.listen(GuildVoiceSink(vc, asyncio.get_running_loop()))

            await interaction.followup.send(f"Joined **{target_channel.name}**.", ephemeral=True)
        except Exception as err:
            await interaction.followup.send(f"Could not join voice channel: {str(err)[:400]}", ephemeral=True)

    @app_commands.command(name="leave", description="Leave the current voice channel")
    async def leave_command(self, interaction: discord.Interaction):
        if not self._allowed(interaction):
            await interaction.response.send_message("Not available in this channel.", ephemeral=True)
            return

        guild = interaction.guild
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected():
            await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
            return

        if guild:
            voice_chat_histories.pop(guild.id, None)
            voice_busy_guilds.discard(guild.id)

        await vc.disconnect(force=True)
        await interaction.response.send_message("Left the voice channel.", ephemeral=True)

    @app_commands.command(name="clearvoice", description="Reset the AI's voice conversation memory")
    async def clear_voice_command(self, interaction: discord.Interaction):
        if interaction.guild:
            voice_chat_histories.pop(interaction.guild.id, None)
        await interaction.response.send_message("Voice conversation history cleared.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceCog(bot))
