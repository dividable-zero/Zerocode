import asyncio
import json
import time
import aiohttp
from services.ai_client import ask_gemini, parse_json

compile_semaphore = asyncio.Semaphore(2)
_compiler_cache = None

DIAG_SYSTEM = (
    "You are a debugging assistant. Given source code with numbered lines, compiler/runtime "
    "messages, and stdin, find the exact cause of failure. Reply ONLY with JSON: "
    '{"line": <1-based line number or null>, "problem": "<1-2 sentences>", "fix": "<short fix>"}.'
)


async def get_compilers(session: aiohttp.ClientSession):
    global _compiler_cache
    if _compiler_cache is None:
        async with session.get("https://wandbox.org/api/list.json") as resp:
            if resp.status != 200:
                raise RuntimeError("Could not reach Wandbox compiler service.")
            _compiler_cache = await resp.json()
    return _compiler_cache


async def compile_code(language: str, code: str, stdin: str = "", memory_mb: int = 512) -> dict:
    aliases = {
        "Python": ["Python"], "C": ["C"], "C++": ["C++"], "Java": ["Java"],
        "JavaScript": ["JavaScript", "Javascript"], "C#": ["C#", "CSharp"],
        "Go": ["Go"], "Rust": ["Rust"], "PHP": ["PHP"], "Ruby": ["Ruby"],
        "TypeScript": ["TypeScript"],
    }

    timeout = aiohttp.ClientTimeout(total=45)
    async with compile_semaphore:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            compilers = await get_compilers(session)
            supported = [n.lower() for n in aliases.get(language, [])]
            matches = [c for c in compilers if c.get("language", "").lower() in supported]
            if not matches:
                raise RuntimeError(f"{language} is not currently available.")

            matches.sort(
                key=lambda c: any(w in (c.get("name", "") + c.get("version", "")).lower() for w in ["head", "experimental", "trunk"])
            )
            compiler = matches[0]

            payload = {"compiler": compiler["name"], "code": code, "stdin": stdin}
            started = time.perf_counter()

            async with session.post(
                "https://wandbox.org/api/compile.json",
                json=payload,
                headers={"Accept": "application/json"},
            ) as resp:
                raw = await resp.text()
                elapsed = time.perf_counter() - started
                if resp.status != 200:
                    raise RuntimeError(f"Wandbox HTTP {resp.status}: {raw[:400]}")
                result = json.loads(raw)

    return {
        "output": result.get("program_output") or result.get("program_message") or "No output produced.",
        "compiler_error": result.get("compiler_error", ""),
        "compiler_output": result.get("compiler_output", ""),
        "program_error": result.get("program_error", ""),
        "status": str(result.get("status", "Unknown")),
        "compiler_name": compiler.get("display-name", compiler["name"]),
        "compiler_version": compiler.get("version", "Unknown"),
        "response_time": elapsed,
    }


async def diagnose_failure(language: str, code: str, stdin: str, diagnostics: str) -> dict:
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(code.splitlines(), 1))[:12000]
    prompt = (
        f"Language: {language}\n\nCode:\n{numbered}\n\n"
        f"stdin: {stdin or '(none)'}\n\nMessages:\n{diagnostics[:3000] or '(none)'}"
    )
    raw = await ask_gemini(prompt, DIAG_SYSTEM, json_mode=True, max_tokens=500, temperature=0.1)
    data = parse_json(raw)
    line = data.get("line") if isinstance(data.get("line"), int) else None
    lines = code.splitlines()
    line_text = lines[line - 1].strip() if line and 1 <= line <= len(lines) else ""
    return {
        "line": line,
        "line_text": line_text,
        "problem": str(data.get("problem") or raw[:400]),
        "fix": str(data.get("fix") or ""),
    }
