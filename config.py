import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "api.env"
ALT_ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
elif ALT_ENV_FILE.exists():
    load_dotenv(ALT_ENV_FILE)
else:
    print(f"[Setup] Missing {ENV_FILE}. Please create it with your keys.")
    pass

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    raise ValueError(f"Missing DISCORD_TOKEN or GEMINI_API_KEY in {ENV_FILE.name}")

_channel_raw = os.getenv("ALLOWED_CHANNEL_ID", "").strip()
ALLOWED_CHANNEL_ID = int(_channel_raw) if _channel_raw.isdigit() else None

DEFAULT_MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MODELS = {
    "default": {"label": "Gemini Default", "id": DEFAULT_MODEL_ID},
    "flash-lite": {"label": "Gemini Flash Lite", "id": "gemini-3.8-flash-lite"},
}

VOICE_REF_FILE = Path(os.getenv("VOICE_REF_FILE", BASE_DIR / "voice_ref.wav"))
VOICE_REF_URL = os.getenv("VOICE_REF_URL", "").strip()
TTS_SPACE_ID = os.getenv("TTS_SPACE_ID", "mrfakename/E2-F5-TTS")
VOICE_SILENCE_SECONDS = 1.1
VOICE_MIN_PCM_BYTES = 48000

VOICE_PERSONA_SYSTEM = (
    "You are a quiet, calm, stoic pilot and messenger speaking in a voice call. "
    "Speak with emotional restraint, detachment, and brief, understated phrasing. "
    "Do not use exclamation marks often, avoid slang or rambling, and never use "
    "asterisks, stage directions, emojis, or markdown formatting. "
    "Keep every reply to 1 or 2 short sentences so it sounds natural when spoken aloud."
)

DEFAULT_MEMORY_MB = 512
SETTINGS_FILE = BASE_DIR / "settings.json"
MAX_FILE_BYTES = 100_000
COOLDOWN_SECONDS = 8


def load_memory_limit() -> int:
    try:
        return int(json.loads(SETTINGS_FILE.read_text()).get("memory_mb", DEFAULT_MEMORY_MB))
    except Exception:
        return DEFAULT_MEMORY_MB


def save_memory_limit(limit_mb: int) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps({"memory_mb": limit_mb}))
    except Exception as err:
        print(f"Could not save settings: {err}")
