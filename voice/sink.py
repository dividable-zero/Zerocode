import asyncio
import random
import tempfile
from pathlib import Path
import discord
from discord.ext import voice_recv

from config import VOICE_MIN_PCM_BYTES, VOICE_SILENCE_SECONDS
from services.ai_client import transcribe_and_reply_voice
from services.tts_engine import synthesize_cloned_voice

voice_busy_guilds: set[int] = set()


class GuildVoiceSink(voice_recv.AudioSink):
    """Thread-safe, per-user PCM audio collector that triggers AI replies after silence."""

    def __init__(self, vc: voice_recv.VoiceRecvClient, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.vc = vc
        self.loop = loop
        self.buffers: dict[int, bytearray] = {}
        self.silence_tasks: dict[int, asyncio.Task] = {}

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if user is None or getattr(user, "bot", False):
            return
        guild = self.vc.guild
        if guild is None or guild.id in voice_busy_guilds or self.vc.is_playing():
            return

        buf = self.buffers.setdefault(user.id, bytearray())
        buf.extend(data.pcm)
        self.loop.call_soon_threadsafe(self._reset_silence_timer, user)

    def _reset_silence_timer(self, user: discord.abc.User):
        old_task = self.silence_tasks.get(user.id)
        if old_task and not old_task.done():
            old_task.cancel()
        self.silence_tasks[user.id] = self.loop.create_task(self._handle_silence(user))

    async def _handle_silence(self, user: discord.abc.User):
        try:
            await asyncio.sleep(VOICE_SILENCE_SECONDS)
        except asyncio.CancelledError:
            return

        guild = self.vc.guild
        pcm_chunk = bytes(self.buffers.pop(user.id, b""))

        if guild is None or not self.vc.is_connected():
            return
        if len(pcm_chunk) < VOICE_MIN_PCM_BYTES or guild.id in voice_busy_guilds:
            return

        voice_busy_guilds.add(guild.id)
        temp_wav = Path(tempfile.gettempdir()) / f"zc_vc_{guild.id}_{random.randrange(10**7)}.wav"

        try:
            speaker_name = getattr(user, "display_name", "User")
            transcript, reply = await transcribe_and_reply_voice(pcm_chunk, guild.id, speaker_name)
            if not transcript or len(transcript) < 2 or not reply:
                return

            print(f"[Voice] {speaker_name}: {transcript} -> AI: {reply}")
            await asyncio.to_thread(synthesize_cloned_voice, reply, temp_wav)

            if not self.vc.is_connected() or self.vc.is_playing():
                return

            done_event = asyncio.Event()

            def after_play(error):
                if error:
                    print(f"Voice playback error: {error}")
                self.loop.call_soon_threadsafe(done_event.set)

            self.vc.play(discord.FFmpegPCMAudio(str(temp_wav)), after=after_play)
            await done_event.wait()

        except Exception as error:
            print(f"Voice turn error: {error}")
        finally:
            voice_busy_guilds.discard(guild.id)
            if temp_wav.exists():
                try:
                    temp_wav.unlink()
                except OSError:
                    pass

    def cleanup(self):
        for task in self.silence_tasks.values():
            if not task.done():
                task.cancel()
        self.silence_tasks.clear()
        self.buffers.clear()
