import os
import re
import ast
import sys
import io
import base64
import shutil
import json
import time
import tempfile
import random
import asyncio
from pathlib import Path
from typing import Optional

import wave
import aiohttp
import discord
from discord import app_commands
from discord.ext import voice_recv
from dotenv import load_dotenv
from gradio_client import Client, handle_file

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

# 1. Create a tiny web server so Render stays happy
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ZeroCode bot is alive!")
    
    # Suppress console log spam from ping requests
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# 2. Start the web server in a background thread when the script boots
threading.Thread(target=run_web_server, daemon=True).start()

# ---------------- CONFIGURATION ----------------

load_dotenv(Path(__file__).with_name("api.env"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ALLOWED_CHANNEL_ID = os.getenv("ALLOWED_CHANNEL_ID", "").strip()

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Missing Discord or Gemini key in api.env")

ALLOWED_CHANNEL_ID = int(ALLOWED_CHANNEL_ID) if ALLOWED_CHANNEL_ID else None
# ---- VOICE CALL CONFIGURATION ----
VOICE_REF_FILE = Path(os.getenv("VOICE_REF_FILE", Path(__file__).with_name("voice_ref.wav")))
TTS_SPACE_ID = os.getenv("TTS_SPACE_ID", "mrfakename/E2-F5-TTS")
VOICE_SILENCE_SECONDS = 1.1
VOICE_MIN_PCM_BYTES = 48000  # ~0.25s of 48kHz 16-bit stereo PCM

VOICE_PERSONA_SYSTEM = (
    "You are a quiet, calm, stoic pilot and messenger speaking in a voice call. "
    "Speak with emotional restraint, detachment, and brief, understated phrasing. "
    "Do not use exclamation marks often, avoid slang or rambling, and never use "
    "asterisks, stage directions, emojis, or markdown formatting. "
    "Keep every reply to 1 or 2 short sentences so it sounds natural when spoken aloud."
)

voice_chat_histories: dict[int, list[dict]] = {}
voice_busy_guilds: set[int] = set()
_tts_client: Optional[Client] = None


def get_tts_client() -> Client:
    global _tts_client
    if _tts_client is None:
        _tts_client = Client(TTS_SPACE_ID)
    return _tts_client
    

# ---- AI MODELS ----
# To add a model later: add an entry here. If it uses a new provider,
# write an async function like call_gemini() and register it in PROVIDERS.
MODELS = {
    "gemini-flash-lite": {
        "label": "Gemini Flash Lite",
        "provider": "gemini",
        "id": "gemini-3.5-flash-lite",
    },
    # "gemini-pro": {"label": "Gemini Pro", "provider": "gemini", "id": "gemini-3.5-pro"},
}
DEFAULT_MODEL = "gemini-flash-lite"

# ---- MEMORY LIMIT (changeable with /memory, saved to settings.json) ----
DEFAULT_MEMORY_MB = 512
SETTINGS_FILE = Path(__file__).with_name("settings.json")


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps({"memory_mb": memory_limit_mb}))
    except Exception as error:
        print(f"Could not save settings: {error}")


memory_limit_mb = int(load_settings().get("memory_mb", DEFAULT_MEMORY_MB))

MAX_FILE_BYTES = 100_000

# ---- MATPLOTLIB / PLOTS (run inside a locked-down Docker container) ----
# Build the image once:  docker build -t zerocode-plot -f Dockerfile.plot .
PLOT_BACKEND = os.getenv("PLOT_BACKEND", "web").lower()   # "web" (AI + QuickChart) or "docker"
PLOT_IMAGE = os.getenv("PLOT_IMAGE", "zerocode-plot")
PLOT_TIMEOUT = 30        # seconds
MAX_PLOT_IMAGES = 4


# ---------------- DISCORD SETUP ----------------

intents = discord.Intents.default()
intents.message_content = True  # needed to read the input replies (enable it in the Developer Portal too)
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

LANGUAGES = [
    "Python", "C", "C++", "Java", "JavaScript", "C#",
    "Go", "Rust", "PHP", "Ruby", "TypeScript",
]

EXTENSIONS = {
    ".py": "Python", ".c": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".java": "Java", ".js": "JavaScript", ".mjs": "JavaScript",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".php": "PHP",
    ".rb": "Ruby", ".ts": "TypeScript",
}

user_cooldowns = {}
COOLDOWN = 8

compile_semaphore = asyncio.Semaphore(2)
compiler_cache = None


# ---------------- BASIC FUNCTIONS ----------------

def check_channel(interaction):
    if ALLOWED_CHANNEL_ID is None:
        return True
    return interaction.channel_id == ALLOWED_CHANNEL_ID


def check_cooldown(user_id):
    now = time.monotonic()
    previous = user_cooldowns.get(user_id, 0)
    if now - previous < COOLDOWN:
        return False
    user_cooldowns[user_id] = now
    return True


async def send_long(interaction, message):
    chunks = [message[i:i + 1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        await interaction.followup.send(
            chunk, allowed_mentions=discord.AllowedMentions.none()
        )


async def read_attachment(attachment):
    if attachment.size > MAX_FILE_BYTES:
        raise RuntimeError(
            f"`{attachment.filename}` is too large (max {MAX_FILE_BYTES // 1000} KB)."
        )
    data = await attachment.read()
    return data.decode("utf-8", errors="replace")


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


# ---------------- AI PROVIDERS ----------------

async def call_gemini(model_id, prompt, system, json_mode=False,
                      max_tokens=800, temperature=0.3):

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model_id}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    generation_config = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }

    timeout = aiohttp.ClientTimeout(total=90, connect=15)
    retry_statuses = {408, 429, 500, 502, 503, 504}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(4):
            try:
                async with session.post(url, headers=headers, json=payload) as response:
                    raw = await response.text()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {}

                    if response.status != 200:
                        error = data.get("error", {}).get(
                            "message", raw[:400] or "Unknown API error"
                        )
                        if response.status in retry_statuses and attempt < 3:
                            try:
                                delay = float(response.headers.get("Retry-After"))
                            except (TypeError, ValueError):
                                delay = min(2 ** attempt, 12)
                            await asyncio.sleep(delay + random.uniform(0, 0.5))
                            continue
                        raise RuntimeError(f"Gemini HTTP {response.status}: {error}")

                    blocked = data.get("promptFeedback", {}).get("blockReason")
                    if blocked:
                        return f"Gemini blocked this request: {blocked}"

                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise RuntimeError("Gemini returned no candidates.")

                    candidate = candidates[0]
                    parts = candidate.get("content", {}).get("parts", [])
                    answer = "\n".join(
                        p.get("text", "") for p in parts if p.get("text")
                    ).strip()

                    if not answer:
                        reason = candidate.get("finishReason", "Unknown")
                        raise RuntimeError(
                            f"Gemini returned an empty answer. Finish reason: {reason}"
                        )
                    return answer

            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt == 3:
                    raise RuntimeError(f"Gemini connection failed: {error}")
                await asyncio.sleep(min(2 ** attempt, 12) + random.uniform(0, 0.5))

            except RuntimeError as error:
                if "empty answer" in str(error).lower() and attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

    raise RuntimeError("Gemini failed after multiple attempts.")


PROVIDERS = {"gemini": call_gemini}


async def ask_ai(prompt, system, model_key=DEFAULT_MODEL, **kwargs):
    model = MODELS.get(model_key) or MODELS[DEFAULT_MODEL]
    return await PROVIDERS[model["provider"]](model["id"], prompt, system, **kwargs)
def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 48000, channels: int = 2) -> bytes:
    """Wrap raw 16-bit signed PCM audio from Discord into a WAV byte buffer."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


async def transcribe_and_reply_voice(pcm_bytes: bytes, guild_id: int, speaker_name: str,
                                   model_key: str = DEFAULT_MODEL) -> tuple[str, str]:
    """Transcribe user PCM audio with Gemini and generate a concise persona reply."""
    model = MODELS.get(model_key) or MODELS[DEFAULT_MODEL]
    model_id = model["id"]
    wav_b64 = base64.b64encode(pcm_to_wav_bytes(pcm_bytes)).decode("ascii")

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model_id}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    history = voice_chat_histories.setdefault(guild_id, [])
    recent_context = "\n".join(
        f"{item['role']}: {item['text']}" for item in history[-6:]
    )

    prompt = (
        "Listen to the user's voice clip. First transcribe what was said, then write "
        "a short spoken reply following your system instructions.\n"
        "Return ONLY valid JSON with keys \"transcript\" and \"reply\".\n"
        f"Speaker: {speaker_name}\n"
        f"Recent conversation:\n{recent_context or '(none)'}"
    )

    payload = {
        "systemInstruction": {"parts": [{"text": VOICE_PERSONA_SYSTEM}]},
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "audio/wav", "data": wav_b64}},
                {"text": prompt},
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": 220,
            "temperature": 0.4,
            "responseMimeType": "application/json",
        },
    }

    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            raw = await response.text()
            if response.status != 200:
                raise RuntimeError(f"Gemini voice HTTP {response.status}: {raw[:300]}")
            data = json.loads(raw)

    candidates = data.get("candidates", [])
    if not candidates:
        return "", ""
    parts = candidates[0].get("content", {}).get("parts", [])
    text_out = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    parsed = parse_json(text_out)

    transcript = str(parsed.get("transcript", "")).strip()
    reply = str(parsed.get("reply", "")).strip()
    # Strip any accidental stage directions or asterisks so TTS doesn't read them
    reply = re.sub(r"\*[^*]*\*", "", reply).strip()

    if transcript:
        history.append({"role": f"User ({speaker_name})", "text": transcript})
    if reply:
        history.append({"role": "Assistant", "text": reply})
    voice_chat_histories[guild_id] = history[-12:]

    return transcript, reply


def synthesize_cloned_voice(text: str, output_wav_path: Path) -> None:
    """Generate speech from text using VOICE_REF_FILE via a zero-shot TTS endpoint."""
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


class GuildVoiceSink(voice_recv.AudioSink):
    """Collects incoming PCM voice packets and triggers a turn after silence."""

    def __init__(self, vc: voice_recv.VoiceRecvClient, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.vc = vc
        self.loop = loop
        self.buffer = bytearray()
        self.last_speaker: Optional[discord.abc.User] = None
        self.silence_task: Optional[asyncio.Task] = None

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData):
        if user is None or getattr(user, "bot", False):
            return
        guild = self.vc.guild
        if guild is None or guild.id in voice_busy_guilds or self.vc.is_playing():
            return

        self.last_speaker = user
        self.buffer.extend(data.pcm)

        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
        self.silence_task = self.loop.create_task(self._handle_silence())

    async def _handle_silence(self):
        try:
            await asyncio.sleep(VOICE_SILENCE_SECONDS)
        except asyncio.CancelledError:
            return

        guild = self.vc.guild
        if guild is None or not self.vc.is_connected():
            self.buffer.clear()
            return

        pcm_chunk = bytes(self.buffer)
        speaker = self.last_speaker
        self.buffer.clear()
        self.last_speaker = None

        if len(pcm_chunk) < VOICE_MIN_PCM_BYTES or guild.id in voice_busy_guilds:
            return

        voice_busy_guilds.add(guild.id)
        temp_wav = Path(tempfile.gettempdir()) / f"zc_vc_{guild.id}_{random.randrange(10**7)}.wav"

        try:
            speaker_name = getattr(speaker, "display_name", "User")
            transcript, reply = await transcribe_and_reply_voice(
                pcm_chunk, guild.id, speaker_name, DEFAULT_MODEL
            )
            if not transcript or len(transcript) < 2 or not reply:
                return

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
            try:
                if temp_wav.exists():
                    temp_wav.unlink()
            except OSError:
                pass

    def cleanup(self):
        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
        self.buffer.clear()
        


HELP_SYSTEM = (
    "You are ZeroCode, a programming assistant. "
    "Be concise but complete: usually 3 to 6 sentences. "
    "Briefly define the concept, then give one short code example when it helps. "
    "No introductions, filler, or repeated summaries. "
    "If the question is a simple yes/no or one-liner, answer briefly."
)

DIAG_SYSTEM = (
    "You are a debugging assistant. Given source code with numbered lines, the "
    "compiler/runtime messages, and the stdin used, find the exact cause of the failure. "
    "Reply ONLY with JSON: "
    '{"line": <1-based line number in the numbered code, or null>, '
    '"problem": "<one or two sentences explaining what is wrong>", '
    '"fix": "<short corrected line or fix>"}. '
    "If the error is EOFError / reading input with no data, say the program needs "
    "input and that it can be provided with the stdin option."
)

SIM_SYSTEM = (
    "You emulate a {language} runtime that has all common third-party libraries "
    "installed. Mentally execute the program step by step with the given stdin and "
    "work out the exact text it would print to stdout. You MUST always give that result; "
    "never say there is no output unless the program truly prints nothing. "
    "If the result depends on randomness, time or network, give one realistic example result. "
    "Reply in exactly this format and nothing else:\n"
    "<<OUTPUT>>\n"
    "(the exact stdout, no commentary, no code fences)\n"
    "<<ERROR>>\n"
    "(NONE if the program runs fine, otherwise a short explanation of the error)\n"
    "<<LINE>>\n"
    "(1-based line number of the error, or NONE)"
)


# ---------------- LIBRARY DETECTION ----------------

PY_STDLIB = getattr(sys, "stdlib_module_names", None) or {
    "os", "sys", "re", "math", "json", "time", "random", "itertools",
    "collections", "functools", "datetime", "string", "typing", "heapq",
    "bisect", "copy", "io", "pathlib", "decimal", "fractions", "statistics",
}
NODE_BUILTINS = {
    "fs", "path", "os", "readline", "util", "events", "http", "https",
    "crypto", "child_process", "stream", "url", "assert", "buffer", "zlib",
    "net", "process", "timers", "querystring", "string_decoder", "worker_threads",
}
RUBY_STDLIB = {
    "json", "set", "time", "date", "securerandom", "digest", "stringio",
    "bigdecimal", "prime", "matrix", "optparse", "ostruct", "forwardable",
    "benchmark", "pp", "yaml", "erb", "fileutils", "tempfile", "open3", "timeout",
}
C_EXTERNAL_HEADERS = ("boost/", "openssl/", "curl/", "SDL", "GL/", "Eigen/", "opencv", "gtk", "Qt")


def detect_external_libs(language, code):
    libs = set()

    if language == "Python":
        try:
            for node in ast.walk(ast.parse(code)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        libs.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    libs.add(node.module.split(".")[0])
        except SyntaxError:
            for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", code, re.M):
                libs.add(m.group(1))
        return sorted(l for l in libs if l not in PY_STDLIB)

    if language in ("JavaScript", "TypeScript"):
        for m in re.finditer(
            r"""(?:require\(\s*|from\s+|import\s+)['"]([^'"]+)['"]""", code
        ):
            name = m.group(1)
            if name.startswith((".", "/", "node:")):
                continue
            if name.split("/")[0] not in NODE_BUILTINS:
                libs.add(name)
        return sorted(libs)

    if language == "Java":
        for m in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+)", code, re.M):
            if not m.group(1).startswith(("java.", "javax.")):
                libs.add(m.group(1))
        return sorted(libs)

    if language == "Go":
        for block in re.findall(r"import\s*\((.*?)\)", code, re.S):
            for m in re.finditer(r'"([^"]+)"', block):
                libs.add(m.group(1))
        for m in re.finditer(r'^\s*import\s+(?:\w+\s+)?"([^"]+)"', code, re.M):
            libs.add(m.group(1))
        return sorted(l for l in libs if "." in l.split("/")[0])

    if language == "Rust":
        local = set(re.findall(r"^\s*(?:pub\s+)?mod\s+(\w+)", code, re.M))
        ok = {"std", "core", "alloc", "crate", "self", "super"} | local
        for m in re.finditer(r"^\s*extern\s+crate\s+(\w+)", code, re.M):
            libs.add(m.group(1))
        for m in re.finditer(r"^\s*use\s+(\w+)::", code, re.M):
            libs.add(m.group(1))
        return sorted(l for l in libs if l not in ok)

    if language == "C#":
        for m in re.finditer(r"^\s*using\s+([A-Za-z_][\w.]*)\s*;", code, re.M):
            if not m.group(1).startswith(("System", "Microsoft")):
                libs.add(m.group(1))
        return sorted(libs)

    if language == "PHP":
        if "vendor/autoload" in code:
            libs.add("composer packages")
        return sorted(libs)

    if language == "Ruby":
        for m in re.finditer(r"""^\s*require\s+['"]([^'"]+)['"]""", code, re.M):
            if m.group(1) not in RUBY_STDLIB:
                libs.add(m.group(1))
        return sorted(libs)

    if language in ("C", "C++"):
        for m in re.finditer(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", code, re.M):
            if m.group(1).startswith(C_EXTERNAL_HEADERS):
                libs.add(m.group(1))
        return sorted(libs)

    return []


INPUT_TIMEOUT = 60  # seconds to wait for someone to reply with input

INPUT_PATTERNS = {
    "Python": r"\binput\s*\(|sys\.stdin",
    "C": r"\b(scanf|fscanf|gets|fgets|getchar|getline|fgetc|getc)\s*\(",
    "C++": r"\bcin\b|\bgetline\s*\(|\b(scanf|fgets|getchar|gets)\s*\(",
    "Java": r"\bScanner\b|BufferedReader|System\.in",
    "JavaScript": r"readline|process\.stdin|\bprompt\s*\(|readFileSync\(\s*(0|['\"]/dev/stdin)",
    "TypeScript": r"readline|process\.stdin|\bprompt\s*\(|readFileSync\(\s*(0|['\"]/dev/stdin)",
    "C#": r"Console\.(ReadLine|Read|ReadKey|In)\b",
    "Go": r"os\.Stdin|fmt\.(Scan|Scanln|Scanf)\s*\(",
    "Rust": r"\bstdin\s*\(",
    "PHP": r"\bSTDIN\b|\breadline\s*\(|php://stdin",
    "Ruby": r"\bgets\b|\bSTDIN\b|\$stdin|\bARGF\b",
}


def extract_input_prompts(language, code):
    """Return the prompt texts of input("...") calls (Python only), in source order."""
    if language != "Python":
        return []
    prompts = []
    try:
        calls = [
            node for node in ast.walk(ast.parse(code))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "input"
        ]
    except SyntaxError:
        return []

    for node in sorted(calls, key=lambda n: (n.lineno, n.col_offset)):
        if not node.args:
            prompts.append("")
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            prompts.append(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            prompts.append("".join(
                v.value if isinstance(v, ast.Constant) else "…" for v in arg.values
            ))
        else:
            prompts.append("…")
    return prompts


def uses_input(language, code):
    pattern = INPUT_PATTERNS.get(language)
    return bool(pattern and re.search(pattern, code))


RESOURCE_PATTERN = re.compile(
    r"Resource temporarily unavailable|can't start new thread|"
    r"Cannot allocate memory|fork failed|EAGAIN",
    re.I,
)

MISSING_LIB_PATTERN = re.compile(
    r"ModuleNotFoundError|No module named|Cannot find module|cannot find package|"
    r"can't find crate|unresolved import|package [\w.]+ does not exist|"
    r"The type or namespace name|Could not load file or assembly|"
    r"cannot load such file",
    re.I,
)


# ---------------- COMPILER ----------------

async def get_compilers(session):
    global compiler_cache
    if compiler_cache is not None:
        return compiler_cache

    async with session.get("https://wandbox.org/api/list.json") as response:
        if response.status != 200:
            raise RuntimeError("Could not reach compiler service.")
        compiler_cache = await response.json()
    return compiler_cache


def python_memory_prefix(code, limit_mb):
    """One-line header that caps memory (Linux). Shifts code down by 1 line."""
    if limit_mb <= 0 or "from __future__" in code:
        return ""
    b = limit_mb * 1024 * 1024
    # RLIMIT_DATA caps heap memory only. RLIMIT_AS also counts thread stacks and
    # mapped libraries, which can make the sandbox fail with "Resource temporarily unavailable".
    return (
        'exec("try:\\n import resource;'
        f'resource.setrlimit(resource.RLIMIT_DATA,({b},{b}))'
        '\\nexcept Exception:pass")\n'
    )


def shift_line_numbers(text, offset):
    if not text or not offset:
        return text
    return re.sub(
        r"(line\s+)(\d+)",
        lambda m: f"{m.group(1)}{max(int(m.group(2)) - offset, 1)}",
        text,
    )


async def compile_code(language, code, stdin="", memory_mb=DEFAULT_MEMORY_MB):

    aliases = {
        "Python": ["Python"], "C": ["C"], "C++": ["C++"], "Java": ["Java"],
        "JavaScript": ["JavaScript", "Javascript"], "C#": ["C#", "CSharp"],
        "Go": ["Go"], "Rust": ["Rust"], "PHP": ["PHP"], "Ruby": ["Ruby"],
        "TypeScript": ["TypeScript"],
    }

    prefix = python_memory_prefix(code, memory_mb) if language == "Python" else ""
    line_offset = 1 if prefix else 0

    timeout = aiohttp.ClientTimeout(total=45)

    async with compile_semaphore:
        async with aiohttp.ClientSession(timeout=timeout) as session:

            compilers = await get_compilers(session)
            supported = [n.lower() for n in aliases.get(language, [])]

            matches = [
                c for c in compilers
                if c.get("language", "").lower() in supported
            ]
            if not matches:
                raise RuntimeError(f"{language} is not currently available.")

            matches.sort(
                key=lambda c: any(
                    w in (c.get("name", "") + c.get("version", "")).lower()
                    for w in ["head", "experimental", "trunk"]
                )
            )
            compiler = matches[0]

            payload = {
                "compiler": compiler["name"],
                "code": prefix + code,
                "stdin": stdin,
            }

            started = time.perf_counter()

            async with session.post(
                "https://wandbox.org/api/compile.json",
                json=payload,
                headers={"Accept": "application/json"},
            ) as response:

                raw = await response.text()
                elapsed = time.perf_counter() - started

                if response.status != 200:
                    raise RuntimeError(f"Wandbox HTTP {response.status}: {raw[:500]}")
                if not raw.strip():
                    raise RuntimeError("Wandbox returned an empty response.")
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    raise RuntimeError(f"Invalid compiler response: {raw[:300]}")

    return {
        "output": (
            result.get("program_output")
            or result.get("program_message")
            or "No output produced."
        ),
        "compiler_error": shift_line_numbers(result.get("compiler_error", ""), line_offset),
        "compiler_output": shift_line_numbers(result.get("compiler_output", ""), line_offset),
        "program_error": shift_line_numbers(result.get("program_error", ""), line_offset),
        "status": str(result.get("status", "Unknown")),
        "compiler_name": compiler.get("display-name", compiler["name"]),
        "compiler_version": compiler.get("version", "Unknown"),
        "response_time": elapsed,
        "memory_enforced": bool(prefix),
    }


# ---------------- AI RUN / DIAGNOSE ----------------

def get_code_line(code, line):
    lines = code.splitlines()
    if isinstance(line, int) and 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


async def diagnose_failure(language, code, stdin, diagnostics, model_key):
    numbered = "\n".join(
        f"{i}: {text}" for i, text in enumerate(code.splitlines(), 1)
    )[:12000]

    prompt = (
        f"Language: {language}\n\n"
        f"Code:\n{numbered}\n\n"
        f"stdin provided: {stdin if stdin else '(none)'}\n\n"
        f"Compiler/runtime messages:\n{diagnostics[:3000] or '(none)'}"
    )

    raw = await ask_ai(
        prompt, DIAG_SYSTEM, model_key,
        json_mode=True, max_tokens=700, temperature=0.1,
    )
    data = parse_json(raw)

    line = data.get("line")
    line = line if isinstance(line, int) else None

    return {
        "line": line,
        "line_text": get_code_line(code, line),
        "problem": str(data.get("problem") or raw[:500]),
        "fix": str(data.get("fix") or ""),
    }


async def ai_run(language, code, stdin, model_key):
    numbered = "\n".join(
        f"{i}: {text}" for i, text in enumerate(code.splitlines(), 1)
    )[:12000]

    prompt = (
        f"Program ({language}), with line numbers:\n{numbered}\n\n"
        f"stdin:\n{stdin if stdin else '(none)'}"
    )
    raw = await ask_ai(
        prompt, SIM_SYSTEM.format(language=language), model_key,
        max_tokens=2500, temperature=0,
    )

    out = re.search(r"<<OUTPUT>>\s*?\n?(.*?)(?=<<ERROR>>|<<LINE>>|\Z)", raw, re.S)
    err = re.search(r"<<ERROR>>\s*?\n?(.*?)(?=<<OUTPUT>>|<<LINE>>|\Z)", raw, re.S)
    line_match = re.search(r"<<LINE>>\s*(\d+)", raw)

    if out:
        output = out.group(1).strip("\n").rstrip()
    else:
        # Model ignored the format: treat its whole reply as the output
        output = re.sub(r"^```\w*\n?|\n?```$", "", raw.strip())

    error = err.group(1).strip() if err else ""
    if error.upper() == "NONE":
        error = ""

    line = int(line_match.group(1)) if line_match else None

    return {
        "output": output,
        "error": error,
        "line": line,
        "line_text": get_code_line(code, line),
    }


# ---------------- EMBED BUILDERS ----------------

def code_block(text, limit):
    text = text[:limit].replace("```", "`\u200b``")
    return f"```text\n{text}\n```"


def add_diagnosis_field(embed, diag):
    parts = []
    if diag.get("line"):
        parts.append(f"**Line {diag['line']}:** `{diag['line_text'][:150] or '?'}`")
    parts.append(diag["problem"])
    if diag.get("fix"):
        parts.append(f"**Fix:** {diag['fix']}")
    embed.add_field(
        name="AI Diagnosis",
        value="\n".join(parts)[:1024],
        inline=False,
    )


def add_input_field(embed, input_info):
    if not input_info:
        return
    text, by = input_info
    if not text or not text.strip():
        return
    name = f"Input (from {by})" if by else "Input"
    embed.add_field(name=name[:256], value=code_block(text, 900), inline=False)


def build_compiler_embed(language, result, diag, input_info=None):
    success = result["status"] == "0"

    embed = discord.Embed(
        title="ZeroCode | Execution Report",
        color=discord.Color.green() if success else discord.Color.red(),
    )
    embed.add_field(name="Language", value=language, inline=True)
    embed.add_field(name="Status", value="Completed" if success else "Failed", inline=True)
    embed.add_field(name="Exit Code", value=result["status"], inline=True)
    embed.add_field(
        name="Compiler",
        value=f"{result['compiler_name']}\nVersion: {result['compiler_version']}"[:1024],
        inline=False,
    )
    embed.add_field(
        name="Response Time",
        value=f"{result['response_time']:.3f} seconds",
        inline=True,
    )
    embed.add_field(
        name="Memory Limit",
        value=(
            "Off" if not memory_limit_mb
            else f"{memory_limit_mb} MB" + ("" if result["memory_enforced"] else " (not enforced)")
        ),
        inline=True,
    )

    embed.description = f"**Output**\n{code_block(result['output'], 3000)}"

    add_input_field(embed, input_info)

    diagnostics = "\n".join(
        v.strip()
        for v in [result["compiler_output"], result["compiler_error"], result["program_error"]]
        if v and v.strip()
    )
    if diagnostics:
        embed.add_field(
            name="Compiler / Runtime Messages",
            value=code_block(diagnostics, 900),
            inline=False,
        )

    if diag:
        add_diagnosis_field(embed, diag)

    embed.set_footer(text="Memory limit is enforced for Python only (Wandbox has no limit option).")
    return embed


def build_ai_embed(language, result, libs, reason, input_info=None):
    failed = bool(result["error"])

    embed = discord.Embed(
        title="ZeroCode | AI Execution",
        color=discord.Color.orange() if not failed else discord.Color.red(),
    )
    embed.add_field(name="Language", value=language, inline=True)
    embed.add_field(name="Mode", value="AI-simulated", inline=True)

    if libs:
        embed.add_field(
            name="Libraries",
            value=", ".join(libs)[:1024],
            inline=False,
        )

    embed.description = f"**Output**\n{code_block(result['output'] or 'No output produced.', 3500)}"

    add_input_field(embed, input_info)

    if failed:
        parts = []
        if result["line"]:
            parts.append(f"**Line {result['line']}:** `{result['line_text'][:150] or '?'}`")
        parts.append(result["error"])
        embed.add_field(name="Error Explanation", value="\n".join(parts)[:1024], inline=False)

    embed.set_footer(text=reason)
    return embed


# ---------------- MATPLOTLIB RUNNER (DOCKER) ----------------

PLOT_RUNNER = """
import matplotlib
matplotlib.use("Agg")
import runpy

try:
    runpy.run_path("/work/code.py", run_name="__main__")
finally:
    import matplotlib.pyplot as plt
    for i, n in enumerate(plt.get_fignums(), 1):
        plt.figure(n).savefig(f"/out/figure_{i}.png", dpi=110, bbox_inches="tight")
"""


async def run_plot(code, stdin, memory_mb):
    mem = memory_mb if memory_mb > 0 else DEFAULT_MEMORY_MB
    container = f"zc-{random.randrange(10**9)}"

    with tempfile.TemporaryDirectory(prefix="zerocode_") as tmp:
        work = Path(tmp) / "work"
        out = Path(tmp) / "out"
        work.mkdir()
        out.mkdir()
        (work / "code.py").write_text(code, encoding="utf-8")
        (work / "runner.py").write_text(PLOT_RUNNER, encoding="utf-8")
        os.chmod(work, 0o755)
        os.chmod(out, 0o777)

        cmd = [
            "docker", "run", "--rm", "-i", "--name", container,
            "--network", "none",
            "--memory", f"{mem}m", "--memory-swap", f"{mem}m",
            "--cpus", "1", "--pids-limit", "64",
            "--read-only", "--tmpfs", "/tmp",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534",
            "-e", "MPLCONFIGDIR=/tmp", "-e", "HOME=/tmp",
            "-v", f"{work}:/work:ro", "-v", f"{out}:/out", "-w", "/out",
            PLOT_IMAGE, "python", "/work/runner.py",
        ]

        started = time.perf_counter()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("Docker is not installed or not in PATH on the bot's machine.")

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin.encode()), timeout=PLOT_TIMEOUT
            )
        except asyncio.TimeoutError:
            timed_out = True
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", container,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = b"", b""

        runtime = time.perf_counter() - started
        stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.decode("utf-8", errors="replace")

        if "Unable to find image" in stderr or "pull access denied" in stderr:
            raise RuntimeError(
                f"Docker image `{PLOT_IMAGE}` not found. Build it with: "
                "docker build -t zerocode-plot -f Dockerfile.plot ."
            )
        if "Cannot connect to the Docker daemon" in stderr or "docker daemon" in stderr.lower():
            raise RuntimeError("Docker is installed but not running.")

        images = []
        for path in sorted(out.glob("*.png"))[:MAX_PLOT_IMAGES]:
            if path.stat().st_size <= 8_000_000:
                images.append((path.name, path.read_bytes()))

    if timed_out:
        stderr = (stderr + f"\nTimed out after {PLOT_TIMEOUT} seconds.").strip()

    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": 124 if timed_out else proc.returncode,
        "runtime": runtime,
        "memory_mb": mem,
        "images": images,
    }


PLOT_SYSTEM = (
    "You convert Python matplotlib programs into Chart.js v4 chart configs that a chart "
    "service will draw. Mentally execute the program (including numpy/pandas maths) to get "
    "the real data values, and work out exactly what it prints to stdout.\n"
    "Chart rules:\n"
    "- plt.plot with numeric x: type \"scatter\", datasets with data as [{\"x\":..,\"y\":..}], "
    "\"showLine\": true, \"pointRadius\": 0 when there are more than 30 points, \"borderWidth\": 2.\n"
    "- plt.plot with text x values: type \"line\" with data.labels.\n"
    "- bar: type \"bar\" (barh: options.indexAxis = \"y\"). scatter: type \"scatter\". "
    "pie: type \"pie\". hist: type \"bar\" with the binned counts and bin ranges as labels.\n"
    "- Title via options.plugins.title {\"display\":true,\"text\":..}; axis labels via "
    "options.scales.x.title and options.scales.y.title {\"display\":true,\"text\":..}; "
    "show the legend only if the program gave labels.\n"
    "- At most 120 points per dataset (downsample if needed), numbers rounded to 4 decimals.\n"
    "- Use matplotlib's default colours in order: #1f77b4, #ff7f0e, #2ca02c, #d62728, #9467bd "
    "(set borderColor and backgroundColor explicitly).\n"
    "- One chart per figure or subplot, in order, at most 4.\n"
    "- Each chart must be pure valid JSON: no comments, no functions, no code fences.\n"
    "Reply in exactly this format and nothing else:\n"
    "<<OUTPUT>>\n(exact stdout, or empty)\n"
    "<<ERROR>>\n(NONE if the program runs fine, otherwise a short explanation of the error)\n"
    "<<LINE>>\n(1-based line number of the error, or NONE)\n"
    "<<CHART>>\n(Chart.js JSON for the first chart)\n"
    "<<CHART>>\n(Chart.js JSON for the next chart, only if there is one)"
)


def parse_plot_reply(raw):
    out = re.search(r"<<OUTPUT>>\s*?\n?(.*?)(?=<<ERROR>>|<<LINE>>|<<CHART>>|\Z)", raw, re.S)
    err = re.search(r"<<ERROR>>\s*?\n?(.*?)(?=<<OUTPUT>>|<<LINE>>|<<CHART>>|\Z)", raw, re.S)
    line_match = re.search(r"<<LINE>>\s*(\d+)", raw)

    error = err.group(1).strip() if err else ""
    if error.upper() == "NONE":
        error = ""

    charts = []
    for block in re.findall(r"<<CHART>>\s*(.*?)(?=<<CHART>>|\Z)", raw, re.S):
        data = parse_json(block)
        if isinstance(data, dict) and "type" in data and "data" in data:
            charts.append(data)

    return {
        "output": out.group(1).strip("\n").rstrip() if out else "",
        "error": error,
        "line": int(line_match.group(1)) if line_match else None,
        "charts": charts[:MAX_PLOT_IMAGES],
    }


async def run_plot_web(code, stdin, model_key):
    """AI turns the matplotlib program into Chart.js configs; QuickChart draws them."""
    started = time.perf_counter()

    numbered = "\n".join(
        f"{i}: {text}" for i, text in enumerate(code.splitlines(), 1)
    )[:12000]
    prompt = (
        f"Program with line numbers:\n{numbered}\n\n"
        f"stdin:\n{stdin if stdin else '(none)'}"
    )

    raw = await ask_ai(prompt, PLOT_SYSTEM, model_key, max_tokens=6000, temperature=0)
    parsed = parse_plot_reply(raw)

    images = []
    problems = []

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i, config in enumerate(parsed["charts"], 1):
            payload = {
                "chart": config,
                "width": 800,
                "height": 500,
                "backgroundColor": "white",
                "format": "png",
                "version": "4",
            }
            try:
                async with session.post("https://quickchart.io/chart", json=payload) as resp:
                    data = await resp.read()
                    if resp.status == 200 and data:
                        images.append((f"figure_{i}.png", data))
                    else:
                        problems.append(f"Chart {i}: QuickChart HTTP {resp.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                problems.append(f"Chart {i}: could not reach QuickChart ({error})")

    diag = None
    if parsed["error"]:
        diag = {
            "line": parsed["line"],
            "line_text": get_code_line(code, parsed["line"]),
            "problem": parsed["error"],
            "fix": "",
        }

    failed = bool(parsed["error"]) or (parsed["charts"] and not images)

    return {
        "web": True,
        "stdout": parsed["output"],
        "stderr": "\n".join(problems),
        "returncode": 1 if failed else 0,
        "runtime": time.perf_counter() - started,
        "images": images,
        "diag": diag,
    }


def build_plot_embed(plot, diag, input_info=None):
    web = plot.get("web", False)
    success = plot["returncode"] == 0

    embed = discord.Embed(
        title="ZeroCode | Plot Report",
        color=discord.Color.green() if success else discord.Color.red(),
    )
    embed.add_field(name="Language", value="Python (matplotlib)", inline=True)
    embed.add_field(name="Status", value="Completed" if success else "Failed", inline=True)
    if web:
        embed.add_field(name="Mode", value="AI-rendered", inline=True)
    else:
        embed.add_field(name="Exit Code", value=str(plot["returncode"]), inline=True)
    embed.add_field(name="Runtime", value=f"{plot['runtime']:.2f} seconds", inline=True)
    if not web:
        embed.add_field(name="Memory Limit", value=f"{plot['memory_mb']} MB", inline=True)
    embed.add_field(name="Plots", value=str(len(plot["images"])), inline=True)

    stdout = plot["stdout"].strip()
    embed.description = f"**Output**\n{code_block(stdout or 'No text output.', 3000)}"

    add_input_field(embed, input_info)

    if plot["stderr"].strip():
        embed.add_field(
            name="Runtime Messages",
            value=code_block(plot["stderr"].strip()[-900:], 900),
            inline=False,
        )

    if diag:
        add_diagnosis_field(embed, diag)

    files = [discord.File(io.BytesIO(data), filename=name) for name, data in plot["images"]]
    if plot["images"]:
        embed.set_image(url=f"attachment://{plot['images'][0][0]}")
        if web:
            embed.set_footer(
                text="Chart approximated by AI and drawn with QuickChart; "
                     "values may differ slightly from real matplotlib."
            )
    elif success:
        embed.set_footer(text="No figure was created. Call plt.plot(), plt.bar(), etc. before the script ends.")

    return embed, files


# ---------------- PYODIDE (REAL PYTHON, NO DOCKER) ----------------
# Needs Deno installed (https://deno.com). Python code runs in WebAssembly inside a
# Deno process that has no env/run/ffi access, no file access outside pyodide_data/,
# and network access only to the package CDN.

PYODIDE_VERSION = os.getenv("PYODIDE_VERSION", "0.29.4")
DENO_PATH = os.getenv("DENO_PATH", "deno")
PYODIDE_TIMEOUT = 60           # seconds once packages are cached
PYODIDE_COLD_TIMEOUT = 240     # seconds before the first successful run (downloads packages)
PYODIDE_ALLOW_NET = os.getenv("PYODIDE_ALLOW_NET", "cdn.jsdelivr.net")
PYODIDE_EXTRA_FLAGS = os.getenv("PYODIDE_DENO_FLAGS", "").split()
MAX_PY_IMAGES = 5
MAX_PY_TOTAL_BYTES = 8_000_000

PYODIDE_DIR = Path(__file__).resolve().with_name("pyodide_data")
PYODIDE_DENO_DIR = PYODIDE_DIR / "deno"
PYODIDE_PKG_DIR = PYODIDE_DIR / "packages"
PYODIDE_RUNNER_FILE = PYODIDE_DIR / "runner.mjs"
PYODIDE_MARK = "__ZC_RESULT__"

pyodide_semaphore = asyncio.Semaphore(2)
pyodide_ready = False
pyodide_warm_started = False


class PyodideUnavailable(Exception):
    """Raised when Pyodide itself cannot run (Deno missing, crash, download failure)."""


PYODIDE_RUNNER = r'''
import * as pyodideModule from "npm:pyodide@__PYODIDE_VERSION__";

const loadPyodide = pyodideModule.loadPyodide ?? pyodideModule.default?.loadPyodide;
const MARK = "__ZC_RESULT__";
const cacheDir = Deno.args[0];

try {
  const input = JSON.parse(await new Response(Deno.stdin.readable).text());
  const py = await loadPyodide({
    packageCacheDir: cacheDir,
    stdout: () => {},
    stderr: () => {},
  });
  await py.loadPackagesFromImports(input.code, {
    messageCallback: () => {},
    errorCallback: () => {},
  });
  py.globals.set("zc_code", input.code);
  py.globals.set("zc_stdin", input.stdin);
  const result = await py.runPythonAsync(input.wrapper);
  console.log(MARK + result);
} catch (error) {
  const message = String((error && error.message) || error).slice(0, 800);
  console.log(MARK + JSON.stringify({ infra_error: message }));
}
Deno.exit(0);
'''

# Runs inside Pyodide. Handles stdin, output capture, errors and "display" calls.
PYODIDE_WRAPPER = r'''
import sys, io, os, json, base64, traceback, linecache

_zc_out, _zc_err = io.StringIO(), io.StringIO()
sys.stdout, sys.stderr = _zc_out, _zc_err
sys.stdin = io.StringIO(zc_stdin)

import builtins


def _zc_input(prompt=""):
    # Behave like a terminal: show the prompt, then the typed answer and a newline
    sys.stdout.write(str(prompt))
    line = sys.stdin.readline()
    if line == "":
        raise EOFError("EOF when reading a line")
    line = line.rstrip("\r\n")
    sys.stdout.write(line + "\n")
    return line


builtins.input = _zc_input

_zc_images = []
_plt = None

# ---- matplotlib: force a headless backend and capture figures on show() / at the end
try:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as _plt
    import matplotlib.figure as _mfig

    _zc_orig_savefig = _mfig.Figure.savefig

    def _zc_savefig(self, fname, *a, **k):
        result = _zc_orig_savefig(self, fname, *a, **k)
        if isinstance(fname, (str, os.PathLike)):
            self._zc_saved = True      # the file itself will be collected below
        return result

    _mfig.Figure.savefig = _zc_savefig

    def _zc_capture(*a, **k):
        for num in _plt.get_fignums():
            fig = _plt.figure(num)
            if getattr(fig, "_zc_saved", False):
                continue
            fig._zc_saved = True
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
            _zc_images.append(("figure_%d.png" % (len(_zc_images) + 1), buf.getvalue()))

    _plt.switch_backend("Agg")   # initialise the real backend BEFORE neutralising user calls
    _plt.show = _zc_capture
    _plt.pause = _zc_capture
    _plt.switch_backend = lambda *a, **k: None
    matplotlib.use = lambda *a, **k: None
except Exception:
    _plt = None

# ---- Pillow: Image.show() would try to open a viewer
try:
    from PIL import Image as _PILImage

    def _zc_pil_show(self, *a, **k):
        buf = io.BytesIO()
        try:
            self.save(buf, format="PNG")
        except Exception:
            self.convert("RGB").save(buf, format="PNG")
        _zc_images.append(("image_%d.png" % (len(_zc_images) + 1), buf.getvalue()))

    _PILImage.Image.show = _zc_pil_show
except Exception:
    pass


def _zc_files():
    found = {}
    for name in os.listdir("."):
        if os.path.isfile(name):
            st = os.stat(name)
            found[name] = (st.st_size, st.st_mtime)
    return found


_zc_before = _zc_files()
_zc_exit = 0
linecache.cache["main.py"] = (len(zc_code), None, zc_code.splitlines(True), "main.py")

try:
    exec(compile(zc_code, "main.py", "exec"), {"__name__": "__main__", "__file__": "main.py"})
except SystemExit as e:
    if e.code is None:
        _zc_exit = 0
    elif isinstance(e.code, int):
        _zc_exit = e.code
    else:
        _zc_exit = 1
        sys.stderr.write(str(e.code) + "\n")
except BaseException as e:
    _zc_exit = 1
    _tb = e.__traceback__.tb_next if e.__traceback__ is not None else None
    sys.stderr.write("".join(traceback.format_exception(type(e), e, _tb)))

try:
    if _plt is not None:
        _zc_capture()
except Exception:
    pass

# ---- images the program saved itself (plt.savefig("x.png"), PIL save, ...)
_zc_after = _zc_files()
for _name in sorted(_zc_after):
    if _name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg")) and _zc_before.get(_name) != _zc_after[_name]:
        with open(_name, "rb") as _f:
            _zc_images.append((_name, _f.read()))

json.dumps({
    "stdout": _zc_out.getvalue(),
    "stderr": _zc_err.getvalue(),
    "exit_code": _zc_exit,
    "images": [[n, base64.b64encode(d).decode()] for n, d in _zc_images[:12]],
})
'''


def pyodide_env():
    keep = [
        "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH",
    ]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["DENO_DIR"] = str(PYODIDE_DENO_DIR)
    env["NO_COLOR"] = "1"
    return env


def ensure_pyodide_files():
    PYODIDE_DENO_DIR.mkdir(parents=True, exist_ok=True)
    PYODIDE_PKG_DIR.mkdir(parents=True, exist_ok=True)
    source = PYODIDE_RUNNER.replace("__PYODIDE_VERSION__", PYODIDE_VERSION)
    if (
        not PYODIDE_RUNNER_FILE.exists()
        or PYODIDE_RUNNER_FILE.read_text(encoding="utf-8") != source
    ):
        PYODIDE_RUNNER_FILE.write_text(source, encoding="utf-8")


async def run_pyodide(code, stdin, timeout=None):
    global pyodide_ready

    if shutil.which(DENO_PATH) is None:
        raise PyodideUnavailable("Deno is not installed or not in PATH.")

    ensure_pyodide_files()

    if timeout is None:
        timeout = PYODIDE_TIMEOUT if pyodide_ready else PYODIDE_COLD_TIMEOUT

    job = json.dumps(
        {"code": code, "stdin": stdin, "wrapper": PYODIDE_WRAPPER}
    ).encode("utf-8")

    cmd = [
        DENO_PATH, "run", "--quiet", "--no-prompt",
        f"--allow-read={PYODIDE_DIR}",
        f"--allow-write={PYODIDE_PKG_DIR}",
        f"--allow-net={PYODIDE_ALLOW_NET}",
        *PYODIDE_EXTRA_FLAGS,
        str(PYODIDE_RUNNER_FILE),
        str(PYODIDE_PKG_DIR) + os.sep,
    ]

    async with pyodide_semaphore:
        started = time.perf_counter()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=pyodide_env(),
            )
        except (FileNotFoundError, PermissionError) as error:
            raise PyodideUnavailable(f"Could not start Deno: {error}")

        try:
            out, err = await asyncio.wait_for(proc.communicate(job), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            if not pyodide_ready:
                raise PyodideUnavailable(
                    "First run timed out (packages are still being downloaded)."
                )
            return {
                "stdout": "",
                "stderr": f"Timed out after {timeout} seconds.",
                "exit_code": 124,
                "runtime": time.perf_counter() - started,
                "images": [],
            }

    runtime = time.perf_counter() - started
    text = out.decode("utf-8", errors="replace")
    stderr_text = err.decode("utf-8", errors="replace").strip()

    index = text.rfind(PYODIDE_MARK)
    if index == -1:
        raise PyodideUnavailable(f"Runner produced no result. {stderr_text[-400:]}")

    try:
        data = json.loads(text[index + len(PYODIDE_MARK):].strip().splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        raise PyodideUnavailable("Runner returned invalid data.")

    if "infra_error" in data:
        raise PyodideUnavailable(data["infra_error"])

    pyodide_ready = True

    images = []
    used = set()
    total = 0
    for name, encoded in data.get("images", [])[:MAX_PY_IMAGES]:
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            continue
        total += len(raw)
        if total > MAX_PY_TOTAL_BYTES:
            break
        safe = re.sub(r"[^\w.\-]", "_", name) or "image.png"
        if safe in used:
            safe = f"{len(used) + 1}_{safe}"
        used.add(safe)
        images.append((safe, raw))

    return {
        "stdout": data.get("stdout", ""),
        "stderr": data.get("stderr", ""),
        "exit_code": int(data.get("exit_code", 1)),
        "runtime": runtime,
        "images": images,
    }


async def warm_pyodide():
    """Download Pyodide + common packages in the background so the first user run is fast."""
    try:
        await run_pyodide(
            "import numpy, pandas, matplotlib.pyplot as plt\nprint('ready')", "",
            timeout=900,
        )
        print("Pyodide is ready.")
    except PyodideUnavailable as error:
        print(f"Pyodide unavailable (Python will use Wandbox): {error}")
    except Exception as error:
        print(f"Pyodide warm-up failed: {error}")


def build_pyodide_embed(result, diag, input_info=None):
    success = result["exit_code"] == 0

    embed = discord.Embed(
        title="ZeroCode | Execution Report",
        color=discord.Color.green() if success else discord.Color.red(),
    )
    embed.add_field(name="Language", value="Python", inline=True)
    embed.add_field(name="Status", value="Completed" if success else "Failed", inline=True)
    embed.add_field(name="Exit Code", value=str(result["exit_code"]), inline=True)
    embed.add_field(name="Engine", value="Pyodide", inline=True)
    embed.add_field(name="Runtime", value=f"{result['runtime']:.2f} seconds", inline=True)
    embed.add_field(name="Images", value=str(len(result["images"])), inline=True)

    stdout = result["stdout"].strip()
    embed.description = f"**Output**\n{code_block(stdout or 'No output produced.', 3000)}"

    add_input_field(embed, input_info)

    if result["stderr"].strip():
        embed.add_field(
            name="Error" if not success else "Messages",
            value=code_block(result["stderr"].strip()[-900:], 900),
            inline=False,
        )

    if diag:
        add_diagnosis_field(embed, diag)

    files = [discord.File(io.BytesIO(data), filename=name) for name, data in result["images"]]

    for name, _ in result["images"]:
        if not name.lower().endswith(".svg"):
            embed.set_image(url=f"attachment://{name}")
            break

    embed.set_footer(text="Real Python running in Pyodide (WebAssembly). Memory limit does not apply here.")
    return embed, files


async def send_report(interaction, embed, files=None):
    """Send the embed + files and add a download button for each attached file."""
    kwargs = {
        "embed": embed,
        "wait": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if files:
        kwargs["files"] = files

    message = await interaction.followup.send(**kwargs)

    if message.attachments:
        view = discord.ui.View(timeout=None)
        for attachment in message.attachments[:5]:
            view.add_item(
                discord.ui.Button(
                    label=f"Download {attachment.filename}"[:80],
                    style=discord.ButtonStyle.link,
                    url=attachment.url,
                )
            )
        try:
            await message.edit(view=view)
        except discord.HTTPException:
            pass

    return message


# ---------------- /HELP COMMAND ----------------

@tree.command(name="help", description="Ask ZeroCode a programming question")
@app_commands.describe(
    question="Your programming question or error",
    model="AI model to use",
)
@app_commands.choices(
    model=[app_commands.Choice(name=m["label"], value=key) for key, m in MODELS.items()]
)
async def help_command(
    interaction: discord.Interaction,
    question: str,
    model: Optional[app_commands.Choice[str]] = None,
):
    if not check_channel(interaction):
        await interaction.response.send_message(
            "This command is not available in this channel.", ephemeral=True
        )
        return

    if not check_cooldown(interaction.user.id):
        await interaction.response.send_message(
            "Please wait before using another command.", ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        model_key = model.value if model else DEFAULT_MODEL
        answer = await ask_ai(question, HELP_SYSTEM, model_key)
        await send_long(interaction, answer)

    except Exception as error:
        await interaction.followup.send(f"AI error: {str(error)[:500]}")


# ---------------- /RUN COMMAND ----------------

@tree.command(name="run", description="Compile and execute programming code")
@app_commands.describe(
    language="Programming language (auto-detected from file extension if you attach a file)",
    code="Your source code (or attach a file instead)",
    file="Source file, e.g. .py .c .cpp .java .js .cs .go .rs .php .rb .ts",
    stdin="Input for the program (use \\n for new lines)",
    stdin_file="Text file to use as the program's input",
)
@app_commands.choices(
    language=[app_commands.Choice(name=lang, value=lang) for lang in LANGUAGES]
)
async def run_command(
    interaction: discord.Interaction,
    language: Optional[app_commands.Choice[str]] = None,
    code: Optional[str] = None,
    file: Optional[discord.Attachment] = None,
    stdin: Optional[str] = None,
    stdin_file: Optional[discord.Attachment] = None,
):
    if not check_channel(interaction):
        await interaction.response.send_message(
            "This command is not available in this channel.", ephemeral=True
        )
        return

    if not check_cooldown(interaction.user.id):
        await interaction.response.send_message(
            "Please wait before running another command.", ephemeral=True
        )
        return

    if not (code and code.strip()) and not file:
        await interaction.response.send_message(
            "You need to provide code or attach a source file.", ephemeral=True
        )
        return

    # Resolve language
    lang = language.value if language else None
    if not lang and file:
        lang = EXTENSIONS.get(Path(file.filename).suffix.lower())
    if not lang:
        await interaction.response.send_message(
            "Please choose a language (could not detect it from the file extension).",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        # Source code
        source = await read_attachment(file) if file else code

        if not source.strip():
            await interaction.followup.send("The source code is empty.")
            return

        # Program input
        if stdin_file:
            input_text = await read_attachment(stdin_file)
        elif stdin:
            input_text = stdin.replace("\\n", "\n")
        else:
            input_text = ""
        # Interactive input: program reads input but none was supplied.
        # Only a message that REPLIES to the prompt below is used as input.
        input_by = None
        if not input_text and uses_input(lang, source):
            asked = extract_input_prompts(lang, source)
            asked_text = ""
            if asked:
                shown = [
                    f"{i}. {(text.strip() or '(no prompt)')[:100]}".replace("`", "'")
                    for i, text in enumerate(asked[:8], 1)
                ]
                if len(asked) > 8:
                    shown.append(f"… and {len(asked) - 8} more")
                asked_text = "\n**The program asks:**\n" + "\n".join(shown)

            prompt_message = await interaction.followup.send(
                f"📥 This program needs input. **Reply to this message** "
                f"(use the Reply button) within {INPUT_TIMEOUT}s. Use several lines "
                f"for several inputs." + asked_text,
                allowed_mentions=discord.AllowedMentions.none(),
                wait=True,
            )

            def input_check(message):
                return (
                    message.reference is not None
                    and message.reference.message_id == prompt_message.id
                    and not message.author.bot
                    and bool(message.content.strip())
                )

            try:
                reply = await bot.wait_for(
                    "message", check=input_check, timeout=INPUT_TIMEOUT
                )
                input_text = reply.content
                input_by = reply.author.display_name
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    "⏱️ No reply received, running without input."
                )

        if input_text and not input_text.endswith("\n"):
            input_text += "\n"

        input_info = (input_text, input_by)

        # Libraries -> AI runs it
        # Python: prefer Pyodide (real execution, real matplotlib images)
        if lang == "Python":
            py_result = None
            try:
                py_result = await run_pyodide(source, input_text)
            except PyodideUnavailable as error:
                print(f"Pyodide unavailable, falling back: {error}")

            if py_result is not None:
                library_missing = (
                    py_result["exit_code"] != 0
                    and MISSING_LIB_PATTERN.search(py_result["stderr"])
                )
                if not library_missing:
                    diag = None
                    if py_result["exit_code"] != 0:
                        try:
                            diag = await diagnose_failure(
                                lang, source, input_text,
                                py_result["stderr"] or py_result["stdout"], DEFAULT_MODEL,
                            )
                        except Exception as error:
                            diag = {
                                "line": None, "line_text": "", "fix": "",
                                "problem": f"AI diagnosis unavailable: {str(error)[:200]}",
                            }
                    embed, files = build_pyodide_embed(py_result, diag, input_info)
                    await send_report(interaction, embed, files)
                    return
                # A library Pyodide does not have: continue to the AI / Wandbox paths below

        libs = detect_external_libs(lang, source)

        # matplotlib -> web backend (AI + QuickChart) or Docker backend
        if lang == "Python" and "matplotlib" in libs:
            if PLOT_BACKEND == "docker":
                plot = await run_plot(source, input_text, memory_limit_mb)

                diag = None
                if plot["returncode"] != 0:
                    try:
                        diag = await diagnose_failure(
                            lang, source, input_text,
                            plot["stderr"] or plot["stdout"], DEFAULT_MODEL,
                        )
                    except Exception as error:
                        diag = {
                            "line": None, "line_text": "", "fix": "",
                            "problem": f"AI diagnosis unavailable: {str(error)[:200]}",
                        }
            else:
                plot = await run_plot_web(source, input_text, DEFAULT_MODEL)
                diag = plot["diag"]

            embed, files = build_plot_embed(plot, diag, input_info)
            await send_report(interaction, embed, files)
            return

        if libs:
            result = await ai_run(lang, source, input_text, DEFAULT_MODEL)
            embed = build_ai_embed(
                lang, result, libs,
                "Uses libraries the compiler lacks, so the AI predicted the result.",
                input_info,
            )
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            return

        # Normal run
        def collect_messages(res):
            return "\n".join(
                v.strip()
                for v in [res["compiler_output"], res["compiler_error"], res["program_error"]]
                if v and v.strip()
            )

        result = await compile_code(lang, source, input_text, memory_limit_mb)
        messages = collect_messages(result)

        # Sandbox/resource errors are not the user's fault: retry, first without
        # the memory limit (if one was applied), then once more after a short wait.
        for _ in range(2):
            if result["status"] == "0" or not RESOURCE_PATTERN.search(messages):
                break
            if not result["memory_enforced"]:
                await asyncio.sleep(2)
            result = await compile_code(lang, source, input_text, 0)
            messages = collect_messages(result)

        if result["status"] != "0" and RESOURCE_PATTERN.search(messages):
            await interaction.followup.send(
                "The compiler service is out of resources right now. "
                "This is not a problem with your code, please try again in a moment."
            )
            return

        # Missing library discovered at runtime -> AI runs it
        if result["status"] != "0" and MISSING_LIB_PATTERN.search(messages):
            ai_result = await ai_run(lang, source, input_text, DEFAULT_MODEL)
            embed = build_ai_embed(
                lang, ai_result, [],
                "Compiler was missing a library, so the AI predicted the result.",
                input_info,
            )
            await interaction.followup.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            return

        # Failed -> AI diagnosis with problem line
        diag = None
        if result["status"] != "0":
            try:
                diag = await diagnose_failure(
                    lang, source, input_text, messages or result["output"], DEFAULT_MODEL
                )
            except Exception as error:
                diag = {
                    "line": None, "line_text": "", "fix": "",
                    "problem": f"AI diagnosis unavailable: {str(error)[:200]}",
                }

        embed = build_compiler_embed(lang, result, diag, input_info)
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    except Exception as error:
        await interaction.followup.send(f"Compiler error: {str(error)[:500]}")


# ---------------- /MEMORY COMMAND ----------------

@tree.command(name="memory", description="View or change the program memory limit (MB)")
@app_commands.describe(limit_mb="New limit in MB, 0 = off (leave empty to just view)")
@app_commands.default_permissions(manage_guild=True)
async def memory_command(
    interaction: discord.Interaction,
    limit_mb: Optional[app_commands.Range[int, 0, 4096]] = None,
):
    global memory_limit_mb

    if limit_mb is None:
        current = f"{memory_limit_mb} MB" if memory_limit_mb else "off"
        await interaction.response.send_message(
            f"Current memory limit: **{current}** (default {DEFAULT_MEMORY_MB} MB).",
            ephemeral=True,
        )
        return

    if not interaction.permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to change this.", ephemeral=True
        )
        return

    memory_limit_mb = limit_mb
    save_settings()

    await interaction.response.send_message(
        f"Memory limit set to **{memory_limit_mb} MB**." if memory_limit_mb
        else "Memory limit turned **off**.",
        ephemeral=True,
    )
    # ---------------- /JOIN & /LEAVE VOICE COMMANDS ----------------

@tree.command(name="join", description="Join your current voice channel and listen")
async def join_command(interaction: discord.Interaction):
    if not check_channel(interaction):
        await interaction.response.send_message(
            "This command is not available in this channel.", ephemeral=True
        )
        return

    if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "Join a voice channel first.", ephemeral=True
        )
        return

    if not VOICE_REF_FILE.exists():
        await interaction.response.send_message(
            f"Reference voice file `{VOICE_REF_FILE.name}` is missing on the server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    target_channel = interaction.user.voice.channel
    guild = interaction.guild

    try:
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

        await interaction.followup.send(
            f"Joined **{target_channel.name}**.", ephemeral=True
        )
    except Exception as error:
        await interaction.followup.send(
            f"Could not join voice channel: {str(error)[:400]}", ephemeral=True
        )


@tree.command(name="leave", description="Leave the current voice channel")
async def leave_command(interaction: discord.Interaction):
    if not check_channel(interaction):
        await interaction.response.send_message(
            "This command is not available in this channel.", ephemeral=True
        )
        return

    guild = interaction.guild
    vc = guild.voice_client if guild else None

    if not vc or not vc.is_connected():
        await interaction.response.send_message(
            "I am not in a voice channel.", ephemeral=True
        )
        return

    if guild:
        voice_chat_histories.pop(guild.id, None)
        voice_busy_guilds.discard(guild.id)

    await vc.disconnect(force=True)
    await interaction.response.send_message("Left the voice channel.", ephemeral=True)
    


# ---------------- BOT STARTUP ----------------

@bot.event
async def on_ready():
    global pyodide_warm_started

    await tree.sync()

    if not pyodide_warm_started:
        pyodide_warm_started = True
        if shutil.which(DENO_PATH):
            asyncio.create_task(warm_pyodide())
        else:
            print("Deno not found: Python will run on Wandbox. Install Deno to enable Pyodide.")

    print("--------------------------------")
    print(f"ZeroCode is online: {bot.user}")
    print("Commands: /run, /help, /memory, /join and /leave")
    print("--------------------------------")


bot.run(DISCORD_TOKEN)
