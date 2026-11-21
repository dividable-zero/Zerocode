import time
from pathlib import Path
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from config import (
    ALLOWED_CHANNEL_ID, COOLDOWN_SECONDS,
    MAX_FILE_BYTES, MODELS, load_memory_limit, save_memory_limit
)
from services.ai_client import ask_gemini
from services.code_runner import compile_code, diagnose_failure

LANGUAGES = ["Python", "C", "C++", "Java", "JavaScript", "C#", "Go", "Rust", "PHP", "Ruby", "TypeScript"]
EXTENSIONS = {
    ".py": "Python", ".c": "C", ".cpp": "C++", ".java": "Java", ".js": "JavaScript",
    ".cs": "C#", ".go": "Go", ".rs": "Rust", ".php": "PHP", ".rb": "Ruby", ".ts": "TypeScript",
}
HELP_SYSTEM = (
    "You are ZeroCode, a programming assistant. Be concise (3 to 6 sentences). "
    "Briefly define the concept and give one short code example when helpful."
)


class CodeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldowns: dict[int, float] = {}
        self.memory_limit_mb = load_memory_limit()

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return ALLOWED_CHANNEL_ID is None or interaction.channel_id == ALLOWED_CHANNEL_ID

    def _check_cooldown(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self.cooldowns.get(user_id, 0) < COOLDOWN_SECONDS:
            return False
        self.cooldowns[user_id] = now
        return True

    @app_commands.command(name="help", description="Ask ZeroCode a programming question")
    @app_commands.describe(question="Your programming question", model="AI model to use")
    @app_commands.choices(model=[app_commands.Choice(name=m["label"], value=k) for k, m in MODELS.items()])
    async def help_command(
        self,
        interaction: discord.Interaction,
        question: str,
        model: Optional[app_commands.Choice[str]] = None,
    ):
        if not self._allowed(interaction) or not self._check_cooldown(interaction.user.id):
            await interaction.response.send_message("Command unavailable or on cooldown.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            answer = await ask_gemini(question, HELP_SYSTEM, model.value if model else "default")
            for i in range(0, len(answer), 1900):
                await interaction.followup.send(answer[i:i + 1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as err:
            await interaction.followup.send(f"AI error: {str(err)[:400]}")

    @app_commands.command(name="run", description="Compile and execute programming code")
    @app_commands.choices(language=[app_commands.Choice(name=l, value=l) for l in LANGUAGES])
    async def run_command(
        self,
        interaction: discord.Interaction,
        language: Optional[app_commands.Choice[str]] = None,
        code: Optional[str] = None,
        file: Optional[discord.Attachment] = None,
        stdin: Optional[str] = None,
    ):
        if not self._allowed(interaction) or not self._check_cooldown(interaction.user.id):
            await interaction.response.send_message("Command unavailable or on cooldown.", ephemeral=True)
            return

        if not (code and code.strip()) and not file:
            await interaction.response.send_message("Provide code or attach a file.", ephemeral=True)
            return

        lang = language.value if language else (EXTENSIONS.get(Path(file.filename).suffix.lower()) if file else None)
        if not lang:
            await interaction.response.send_message("Please select a language.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            if file:
                if file.size > MAX_FILE_BYTES:
                    raise RuntimeError("File is too large (max 100 KB).")
                source = (await file.read()).decode("utf-8", errors="replace")
            else:
                source = code

            input_text = stdin.replace("\\n", "\n") if stdin else ""
            result = await compile_code(lang, source, input_text, self.memory_limit_mb)
            success = result["status"] == "0"

            embed = discord.Embed(
                title="ZeroCode | Execution Report",
                color=discord.Color.green() if success else discord.Color.red(),
                description=f"**Output**\n```text\n{result['output'][:3000]}\n```",
            )
            embed.add_field(name="Language", value=lang, inline=True)
            embed.add_field(name="Exit Code", value=result["status"], inline=True)
            embed.add_field(name="Time", value=f"{result['response_time']:.2f}s", inline=True)

            msgs = "\n".join(v.strip() for v in [result["compiler_error"], result["program_error"]] if v and v.strip())
            if msgs:
                embed.add_field(name="Errors", value=f"```text\n{msgs[:900]}\n```", inline=False)

            if not success:
                diag = await diagnose_failure(lang, source, input_text, msgs or result["output"])
                fix_text = f"**Fix:** {diag['fix']}" if diag["fix"] else ""
                embed.add_field(name="AI Diagnosis", value=f"{diag['problem']}\n{fix_text}"[:1024], inline=False)

            await interaction.followup.send(embed=embed)
        except Exception as err:
            await interaction.followup.send(f"Compiler error: {str(err)[:400]}")

    @app_commands.command(name="memory", description="View or change the memory limit (MB)")
    @app_commands.default_permissions(manage_guild=True)
    async def memory_command(
        self,
        interaction: discord.Interaction,
        limit_mb: Optional[app_commands.Range[int, 0, 4096]] = None,
    ):
        if limit_mb is None:
            await interaction.response.send_message(f"Current memory limit: **{self.memory_limit_mb} MB**.", ephemeral=True)
            return
        self.memory_limit_mb = limit_mb
        save_memory_limit(limit_mb)
        await interaction.response.send_message(f"Memory limit set to **{limit_mb} MB**.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CodeCog(bot))
