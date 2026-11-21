import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional
import aiohttp
from gradio_client import Client, handle_file
from config import TTS_SPACE_ID, VOICE_REF_FILE

_tts_client: Optional[Client] = None


def get_tts_client() -> Client:
    global _tts_client
    if _tts_client is None:
        _tts_client = Client(TTS_SPACE_ID)
    return _tts_client


def normalize_gdrive_url(url: str) -> str:
    match = re.search(r"drive\.google\.com/(?:file/d/|open\?id=)([A-Za-z0-9_-]+)", url)
    if match:
        return f"https://drive.google.com/uc?export=download&id={match.group(1)}"
    return url


async def prepare_reference_audio(
    url: str,
    target_wav: Path = VOICE_REF_FILE,
    start_sec: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> None:
    clean_url = normalize_gdrive_url(url.strip())

    with tempfile.TemporaryDirectory(prefix="zc_ref_") as tmp:
        raw_download = Path(tmp) / "source_media"

        if "drive.google.com" in clean_url or clean_url.lower().endswith(
            (".wav", ".mp3", ".ogg", ".m4a", ".mp4", ".webm")
        ):
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(clean_url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Download failed (HTTP {resp.status}).")
                    if "text/html" in resp.headers.get("Content-Type", "") and "drive.google.com" in clean_url:
                        raise RuntimeError("Google Drive link is not public ('Anyone with the link').")
                    raw_download.write_bytes(await resp.read())
        else:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "-f", "bestaudio/best", "-o", str(raw_download), clean_url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0 or not raw_download.exists():
                raise RuntimeError(f"yt-dlp failed: {err.decode('utf-8', errors='replace')[:300]}")

        ff_args = ["ffmpeg", "-y"]
        if start_sec is not None and start_sec >= 0:
            ff_args.extend(["-ss", str(start_sec)])
        ff_args.extend(["-i", str(raw_download)])
        if duration_sec is not None and duration_sec > 0:
            ff_args.extend(["-t", str(duration_sec)])
        ff_args.extend(["-ac", "1", "-ar", "24000", str(target_wav)])

        ff = await asyncio.create_subprocess_exec(
            *ff_args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, ff_err = await asyncio.wait_for(ff.communicate(), timeout=30)
        if ff.returncode != 0 or not target_wav.exists():
            raise RuntimeError(f"FFmpeg failed: {ff_err.decode('utf-8', errors='replace')[:300]}")


def synthesize_cloned_voice(text: str, output_wav_path: Path) -> None:
    if not VOICE_REF_FILE.exists():
        raise FileNotFoundError(f"Reference voice audio not found at: {VOICE_REF_FILE}")

    client = get_tts_client()
    generated_path = client.predict(
        ref_audio_input=handle_file(str(VOICE_REF_FILE)),
        ref_text_input="",
        gen_text_input=text,
        remove_silence=True,
        api_name="/basic_tts",
    )
    shutil.copyfile(generated_path, output_wav_path)
