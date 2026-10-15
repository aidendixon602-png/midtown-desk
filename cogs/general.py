"""Commands anyone can use: /help, /ping, /invite, /about."""

import time

import discord
from discord import app_commands
from discord.ext import commands

from midtown import config
from midtown.database import db
from midtown.utils import human_delta, link_buttons, reply, utcnow


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description=f"How to use {config.BOT_NAME}")
    async def help_cmd(self, inter: discord.Interaction):
        e = discord.Embed(title=f"🎫 {config.BOT_NAME} — Help", color=config.COLOR,
                          description="A ticket system for your server: members open private tickets, staff help them.")
        e.add_field(name="Getting started (admins)", inline=False, value=(
            "`/setup` — creates the staff role, channels and ticket menu in one go\n"
            "`/panel` — post another ticket menu in a channel\n"
            "`/category add/edit/remove/list/reset` — your own ticket types\n"
            "`/settings` — ticket limit, auto-close, transcripts, ratings\n"
            "`/stats` — all-time stats and staff leaderboard\n"
            "`/report` — the last 7 days (also posted weekly in your log channel)"))
        e.add_field(name="Inside a ticket (staff)", inline=False, value=(
            "**Claim** / **Close** buttons\n"
            "`/ticket claim` · `unclaim` · `transfer` — who's handling it\n"
            "`/ticket priority` — 🔴 urgent · 🟠 high · ⚪ normal · 🔵 low\n"
            "`/ticket add` · `remove` · `rename` · `close` · `closerequest`\n"
            "`/ticket history` — a member's past tickets\n"
            "`/ticket block` · `unblock` · `blocked` — stop someone opening tickets"))
        e.add_field(name="Everyone", inline=False, value="`/help` · `/ping` · `/invite` · `/about`")
        e.set_footer(text=f"{config.BOT_NAME} v{config.VERSION}")
        await reply(inter, embed=e, view=link_buttons(self.bot))

    @app_commands.command(name="ping", description="Check the bot's response time")
    async def ping_cmd(self, inter: discord.Interaction):
        start = time.perf_counter()
        db.one("SELECT 1")
        db_ms = (time.perf_counter() - start) * 1000
        await reply(inter, f"🏓 Pong! Gateway **{self.bot.latency * 1000:.0f}ms** · Database **{db_ms:.1f}ms**")

    @app_commands.command(name="invite", description=f"Add {config.BOT_NAME} to your own server")
    async def invite_cmd(self, inter: discord.Interaction):
        await reply(inter, f"Add {config.BOT_NAME} to your server with the button below. 👇",
                    view=link_buttons(self.bot))

    @app_commands.command(name="about", description=f"About {config.BOT_NAME}")
    async def about_cmd(self, inter: discord.Interaction):
        tickets = db.one("SELECT COUNT(*) c FROM tickets")["c"]
        e = discord.Embed(title=f"About {config.BOT_NAME}", color=config.COLOR,
                          description="A free ticket bot with dropdown panels, transcripts, ratings and auto-close.")
        e.add_field(name="Servers", value=f"{len(self.bot.guilds):,}")
        e.add_field(name="Tickets handled", value=f"{tickets:,}")
        e.add_field(name="Uptime", value=human_delta(utcnow() - self.bot.started_at))
        e.add_field(name="Version", value=config.VERSION)
        e.add_field(name="Library", value=f"discord.py {discord.__version__}")
        if self.bot.user.display_avatar:
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
        await reply(inter, embed=e, view=link_buttons(self.bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
