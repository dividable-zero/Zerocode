import logging, sys
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
for _mod in ("discord.ext.voice_recv", "discord.voice_state", "discord.player", "discord.opus"):
    logging.getLogger(_mod).setLevel(logging.DEBUG)

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import discord
from discord.ext import commands
from config import DISCORD_TOKEN


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ZeroCode bot is alive!")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


class ZeroCodeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.voice_cog")
        await self.load_extension("cogs.code_cog")
        await self.tree.sync()

    async def on_ready(self):
        print("--------------------------------")
        print(f"ZeroCode is online: {self.user}")
        print("Commands: /join, /leave, /clearvoice, /run, /help, /memory")
        print("--------------------------------")


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    bot = ZeroCodeBot()
    bot.run(DISCORD_TOKEN)
