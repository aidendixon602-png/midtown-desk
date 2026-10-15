"""
Hidden owner-only text commands (prefix "," by default). Nobody else can see or use them.
Server approval commands live in cogs/auth.py; this file has ,help and the general owner tools.
"""

import os
import platform

import discord
from discord.ext import commands

from midtown import config
from midtown.database import db
from midtown.utils import human_delta, is_dev, log, needs_auth, utcnow

try:
    import resource  # not available on Windows
except ImportError:
    resource = None

P = config.OWNER_PREFIX

HELP_SECTIONS = [
    ("🔐 Server approval", [
        ("authorize [id] [note]", "Approve this server, or one by ID (aliases: auth, approve)"),
        ("revoke <id> [leave]", "Remove approval; add `leave` to kick the bot out (alias: unauth)"),
        ("servers", "Every server the bot is in, ✅ approved or 🔒 waiting (alias: list)"),
        ("authorized", "Only the approved servers"),
    ]),
    ("📊 Info", [
        ("stats", "Servers, members, tickets, uptime, memory"),
        ("server <id>", "Details about one server"),
    ]),
    ("🛠️ Control", [
        ("leave <id>", "Make the bot leave a server"),
        ("maintenance on|off", "Pause the bot for everyone except you"),
        ("status [text]", "Set the bot's status (no text = reset)"),
        ("block <user_id> [reason]", "Block someone from using the bot everywhere"),
        ("unblock <user_id>", "Undo a block"),
    ]),
    ("⚙️ Bot", [
        ("backup", "DM you a copy of the database"),
        ("reload", "Reload all code without restarting"),
        ("sync", "Push slash command changes to Discord"),
    ]),
]


class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_dev(ctx.author)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return  # silent for everyone except the owner
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(f"Usage: `{P}{ctx.command.qualified_name} {ctx.command.signature}`")
            return
        log.exception("Owner command error", exc_info=error)
        await ctx.reply(f"Error: `{error}`")

    # ---- help ----

    @commands.command(name="help", aliases=["h", "commands"])
    async def help_cmd(self, ctx: commands.Context):
        e = discord.Embed(title=f"🛡️ {config.BOT_NAME} — Owner commands", color=config.COLOR,
                          description=f"Only you can use these. Prefix: `{P}`")
        for title, cmds in HELP_SECTIONS:
            e.add_field(name=title, inline=False,
                        value="\n".join(f"`{P}{usage}` — {desc}" for usage, desc in cmds))
        waiting = sum(1 for g in self.bot.guilds if needs_auth(g.id))
        footer = f"v{config.VERSION} · {len(self.bot.guilds)} servers"
        if waiting:
            footer += f" · {waiting} waiting for approval"
        e.set_footer(text=footer)
        await ctx.reply(embed=e, mention_author=False)

    # ---- info ----

    @commands.command(name="stats")
    async def stats_cmd(self, ctx: commands.Context):
        guilds = self.bot.guilds
        t = db.one("SELECT COUNT(*) c, SUM(status='open') o FROM tickets")
        size = os.path.getsize(db.path) / 1024 / 1024 if os.path.exists(db.path) else 0
        memory = f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB" if resource else "n/a"
        waiting = sum(1 for g in guilds if needs_auth(g.id))
        e = discord.Embed(title=f"📊 {config.BOT_NAME} stats", color=config.COLOR, timestamp=utcnow())
        e.add_field(name="Servers", value=f"{len(guilds):,}" + (f" ({waiting} waiting)" if waiting else ""))
        e.add_field(name="Members", value=f"{sum(g.member_count or 0 for g in guilds):,}")
        e.add_field(name="Tickets", value=f"{t['c']:,} total · {t['o'] or 0:,} open")
        e.add_field(name="Uptime", value=human_delta(utcnow() - self.bot.started_at))
        e.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f}ms")
        e.add_field(name="Memory", value=memory)
        e.add_field(name="Database", value=f"{size:.2f} MB")
        e.add_field(name="Maintenance", value="🛠️ On" if db.get_state("maintenance") == "1" else "Off")
        e.set_footer(text=f"v{config.VERSION} · Python {platform.python_version()} · discord.py {discord.__version__}")
        await ctx.reply(embed=e, mention_author=False)

    @commands.command(name="server", aliases=["guild"])
    async def server_cmd(self, ctx: commands.Context, server_id: int):
        guild = self.bot.get_guild(server_id)
        c = db.one("SELECT COUNT(*) c, SUM(status='open') o FROM tickets WHERE guild_id=?", (server_id,))
        cfg = db.one("SELECT staff_role_id FROM guilds WHERE guild_id=?", (server_id,))
        e = discord.Embed(title=guild.name if guild else f"Server {server_id}", color=config.COLOR)
        e.add_field(name="Bot in server", value="✅ Yes" if guild else "❌ No")
        e.add_field(name="Approved", value="🔒 No" if needs_auth(server_id) else "✅ Yes")
        e.add_field(name="Set up", value="✅ Yes" if cfg and cfg["staff_role_id"] else "❌ No")
        if guild:
            e.add_field(name="Members", value=f"{guild.member_count or 0:,}")
            e.add_field(name="Owner", value=f"<@{guild.owner_id}> `{guild.owner_id}`")
            if guild.icon:
                e.set_thumbnail(url=guild.icon.url)
        e.add_field(name="Tickets", value=f"{c['c']:,} total · {c['o'] or 0:,} open")
        await ctx.reply(embed=e, mention_author=False)

    # ---- control ----

    @commands.command(name="leave")
    async def leave_cmd(self, ctx: commands.Context, server_id: int):
        guild = self.bot.get_guild(server_id)
        if guild is None:
            return await ctx.reply("I'm not in a server with that ID.")
        await guild.leave()
        if ctx.guild is None or ctx.guild.id != server_id:
            await ctx.reply(f"👋 Left **{guild.name}**.")

    @commands.command(name="maintenance", aliases=["maint"])
    async def maintenance_cmd(self, ctx: commands.Context, mode: str):
        on = mode.lower() in ("on", "true", "yes", "1")
        db.set_state("maintenance", "1" if on else None)
        events = self.bot.get_cog("Events")
        if events:
            await events.apply_status()
        await ctx.reply("🛠️ Maintenance **on**. Only you can use the bot." if on else "✅ Maintenance **off**.")

    @commands.command(name="status")
    async def status_cmd(self, ctx: commands.Context, *, text: str | None = None):
        db.set_state("custom_status", text[:120] if text else None)
        events = self.bot.get_cog("Events")
        if events:
            await events.apply_status()
        await ctx.reply(f"✅ Status set to **{text[:120]}**." if text else "✅ Status reset.")

    @commands.command(name="block")
    async def block_cmd(self, ctx: commands.Context, user_id: int, *, reason: str | None = None):
        if is_dev(discord.Object(user_id)):
            return await ctx.reply("You can't block an owner.")
        db.blacklist_add("user", user_id, reason, ctx.author.id)
        await ctx.reply(f"⛔ <@{user_id}> can no longer use the bot anywhere.")

    @commands.command(name="unblock")
    async def unblock_cmd(self, ctx: commands.Context, user_id: int):
        removed = db.blacklist_remove("user", user_id)
        await ctx.reply(f"✅ Unblocked <@{user_id}>." if removed else "They weren't blocked.")

    # ---- bot ----

    @commands.command(name="backup")
    async def backup_cmd(self, ctx: commands.Context):
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(os.path.dirname(db.path) or ".", f"backup-{stamp}.db")
        db.backup_to(path)
        try:
            if os.path.getsize(path) > 9 * 1024 * 1024:
                return await ctx.reply(f"Backup is too big to send. Saved on the host as `{path}`.")
            await ctx.author.send(f"💾 Database backup ({stamp} UTC)", file=discord.File(path, f"midtown-{stamp}.db"))
            if ctx.guild:
                await ctx.reply("💾 Sent the backup to your DMs.", mention_author=False)
        except discord.Forbidden:
            await ctx.reply("I couldn't DM you. Open your DMs and try again.")
        finally:
            if os.path.exists(path) and os.path.getsize(path) <= 9 * 1024 * 1024:
                os.remove(path)

    @commands.command(name="reload")
    async def reload_cmd(self, ctx: commands.Context):
        results = []
        for ext in config.EXTENSIONS:
            try:
                await self.bot.reload_extension(ext)
                results.append(f"✅ {ext.split('.')[-1]}")
            except Exception as e:
                log.exception("Reload failed for %s", ext)
                results.append(f"❌ {ext.split('.')[-1]}: `{type(e).__name__}: {str(e)[:120]}`")
        await ctx.reply("\n".join(results) + f"\n-# Run `{P}sync` if you changed slash command names or options.")

    @commands.command(name="sync")
    async def sync_cmd(self, ctx: commands.Context):
        public, dev = await self.bot.sync_commands()
        self.bot.synced = (public, dev)
        await ctx.reply(f"🔄 Synced **{public}** slash commands" + (f" and **{dev}** dev commands." if dev else "."))


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
