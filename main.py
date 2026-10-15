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
from midtown.console import banner, setup_console_logging
from midtown.tickets import TicketCloseError, load_open_channels
from midtown.utils import gatekeep, reply, report_error, utcnow


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler("logs/bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)  # full timestamps in the log file
    console = setup_console_logging()  # short, colored lines in the host console
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)


class MidtownTree(app_commands.CommandTree):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return await gatekeep(inter)  # blacklist + maintenance mode

    async def on_error(self, inter: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use that."
        elif isinstance(error, app_commands.CheckFailure):
            return  # gatekeep / dev check already told the user
        elif isinstance(original, TicketCloseError):
            msg = str(original)
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
            command_prefix=commands.when_mentioned_or(config.OWNER_PREFIX),
            intents=intents,
            help_command=None,
            tree_cls=MidtownTree,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            activity=discord.Activity(type=discord.ActivityType.watching, name="/help"),
        )
        self.started_at = utcnow()
        self.synced = (0, 0)
        self.owner_guild_ids: set[int] = set()  # servers where /owner is registered
        self.announced_ready = False

    async def setup_hook(self):
        load_open_channels()
        for ext in config.EXTENSIONS:
            await self.load_extension(ext)
        try:
            self.synced = await self.sync_commands()
        except discord.HTTPException:
            logging.getLogger("midtown").exception(
                "Slash command sync failed. The bot will connect; retry with the owner prefix command "
                "sync after checking Discord access and the applications.commands invite scope.")

    async def sync_commands(self) -> tuple[int, int]:
        """Sync public commands everywhere, and /owner in the owner's servers."""
        public = await self.tree.sync()
        for gid in self.owner_guild_ids:
            try:
                await self.tree.sync(guild=discord.Object(gid))
            except discord.HTTPException:
                pass
        self.synced = (len(public), len(self.owner_guild_ids))
        return self.synced


def main():
    if not config.TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and fill it in.")
    banner()
    setup_logging()
    try:
        MidtownBot().run(config.TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("Enable Message Content Intent in Discord Developer Portal > Bot > "
                         "Privileged Gateway Intents, then restart Midtown Desk.") from None
    except discord.LoginFailure:
        raise SystemExit("Discord rejected DISCORD_TOKEN. Set the bot token from Developer Portal > Bot "
                         "in your host environment or .env, then restart.") from None


if __name__ == "__main__":
    main()
