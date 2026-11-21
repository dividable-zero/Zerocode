import asyncio
import base64
import io
import json
import random
import re
import wave
import aiohttp
from config import GEMINI_API_KEY, MODELS, VOICE_PERSONA_SYSTEM

voice_chat_histories: dict[int, list[dict]] = {}


def parse_json(text: str) -> dict:
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


async def ask_gemini(
    prompt: str,
    system: str,
    model_key: str = "default",
    json_mode: bool = False,
    max_tokens: int = 800,
    temperature: float = 0.3,
) -> str:
    model_id = (MODELS.get(model_key) or MODELS["default"])["id"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    gen_config = {"maxOutputTokens": max_tokens, "temperature": temperature}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }

    timeout = aiohttp.ClientTimeout(total=90, connect=15)
    retry_statuses = {408, 429, 500, 502, 503, 504}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(4):
            try:
                async with session.post(url, headers=headers, json=payload) as resp:
                    raw = await resp.text()
                    data = parse_json(raw) if raw else {}

                    if resp.status != 200:
                        err = data.get("error", {}).get("message", raw[:300] or "API error")
                        if resp.status in retry_statuses and attempt < 3:
                            await asyncio.sleep(min(2 ** attempt, 12) + random.uniform(0, 0.5))
                            continue
                        raise RuntimeError(f"Gemini HTTP {resp.status}: {err}")

                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise RuntimeError("Gemini returned no candidates.")

                    parts = candidates[0].get("content", {}).get("parts", [])
                    answer = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                    if answer:
                        return answer
                    raise RuntimeError("Gemini returned an empty answer.")

            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                if attempt == 3:
                    raise RuntimeError(f"Gemini connection failed: {err}")
                await asyncio.sleep(min(2 ** attempt, 12) + random.uniform(0, 0.5))

    raise RuntimeError("Gemini failed after multiple attempts.")


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 48000, channels: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


async def transcribe_and_reply_voice(
    pcm_bytes: bytes, guild_id: int, speaker_name: str, model_key: str = "default"
) -> tuple[str, str]:
    model_id = (MODELS.get(model_key) or MODELS["default"])["id"]
    wav_b64 = base64.b64encode(pcm_to_wav_bytes(pcm_bytes)).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    history = voice_chat_histories.setdefault(guild_id, [])
    recent_context = "\n".join(f"{item['role']}: {item['text']}" for item in history[-6:])

    prompt = (
        "Listen to the user's voice clip. First transcribe what was said, then write "
        "a short spoken reply following your system instructions.\n"
        'Return ONLY valid JSON with keys "transcript" and "reply".\n'
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
        async with session.post(url, headers=headers, json=payload) as resp:
            raw = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Gemini voice HTTP {resp.status}: {raw[:300]}")
            data = json.loads(raw)

    candidates = data.get("candidates", [])
    if not candidates:
        return "", ""

    parts = candidates[0].get("content", {}).get("parts", [])
    text_out = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    parsed = parse_json(text_out)

    transcript = str(parsed.get("transcript", "")).strip()
    reply = re.sub(r"\*[^*]*\*", "", str(parsed.get("reply", ""))).strip()

    if transcript:
        history.append({"role": f"User ({speaker_name})", "text": transcript})
    if reply:
        history.append({"role": "Assistant", "text": reply})
    voice_chat_histories[guild_id] = history[-12:]

    return transcript, reply
