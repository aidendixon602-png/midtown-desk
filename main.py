"""
Midtown Desk - a multi-server ticket bot for Discord.

Run with:  python main.py
"""

import logging
import os
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands

from midtown import config
from midtown.tickets import load_open_channels
from midtown.utils import gatekeep, reply, report_error, utcnow


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


class MidtownTree(app_commands.CommandTree):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return await gatekeep(inter)  # blacklist + maintenance mode

    async def on_error(self, inter: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure):
            return  # gatekeep / dev check already told the user
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use that."
        elif isinstance(original, discord.Forbidden):
            msg = ("I'm missing permissions for that. Make sure my role has **Manage Channels** and "
                   "**Manage Roles**, and can see the channel.")
        else:
            name = inter.command.qualified_name if inter.command else "unknown"
            code = await report_error(inter.client, original, f"/{name} in {inter.guild_id}")
            msg = f"Something went wrong (error `{code}`). The developers have been notified."
        try:
            await reply(inter, msg)
        except discord.HTTPException:
            pass


class MidtownBot(commands.AutoShardedBot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for ticket transcripts
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            tree_cls=MidtownTree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            activity=discord.Activity(type=discord.ActivityType.watching, name="/help"),
        )
        self.started_at = utcnow()

    async def setup_hook(self):
        load_open_channels()
        for ext in config.EXTENSIONS:
            await self.load_extension(ext)
        public, dev = await self.sync_commands()
        logging.getLogger("midtown").info("Synced %d public and %d dev commands", public, dev)

    async def sync_commands(self) -> tuple[int, int]:
        public = await self.tree.sync()
        dev = []
        if config.DEV_GUILD_ID:
            dev = await self.tree.sync(guild=discord.Object(config.DEV_GUILD_ID))
        return len(public), len(dev)


def main():
    if not config.TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and fill it in.")
    setup_logging()
    MidtownBot().run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
