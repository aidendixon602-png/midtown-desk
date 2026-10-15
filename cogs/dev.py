"""
Developer-only commands (/dev ...).

They only show up in your dev server (DEV_GUILD_ID), are hidden from non-admins there,
and only work for user IDs listed in DEV_IDS.
"""

import asyncio
import math
import os
import platform
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from midtown import config
from midtown.database import db
from midtown.utils import human_delta, is_dev, log, reply, utcnow

try:
    import resource  # not available on Windows
except ImportError:
    resource = None

EXTENSION_CHOICES = [app_commands.Choice(name="all", value="all")] + [
    app_commands.Choice(name=ext.split(".")[-1], value=ext) for ext in config.EXTENSIONS]
KIND_CHOICES = [app_commands.Choice(name="user", value="user"), app_commands.Choice(name="server", value="guild")]


def parse_id(value: str) -> int | None:
    value = value.strip().strip("<@!#&>")
    return int(value) if value.isdigit() else None


class DevCommands(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(name="dev", description="Developer-only commands", guild_only=True,
                         default_permissions=discord.Permissions(administrator=True))
        self.bot = bot

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if is_dev(inter.user):
            return True
        await reply(inter, "⛔ Developer only.")
        return False

    # ---- info ----

    @app_commands.command(name="stats", description="Bot-wide stats")
    async def stats(self, inter: discord.Interaction):
        guilds = self.bot.guilds
        tickets = db.one("SELECT COUNT(*) c, SUM(status='open') o FROM tickets")
        setup_count = db.one("SELECT COUNT(*) c FROM guilds WHERE staff_role_id IS NOT NULL")["c"]
        db_size = os.path.getsize(db.path) / 1024 / 1024 if os.path.exists(db.path) else 0
        memory = f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB" if resource else "n/a"

        e = discord.Embed(title=f"🛠️ {config.BOT_NAME} stats", color=config.COLOR, timestamp=utcnow())
        e.add_field(name="Servers", value=f"{len(guilds):,} ({setup_count:,} set up)")
        e.add_field(name="Members", value=f"{sum(g.member_count or 0 for g in guilds):,}")
        e.add_field(name="Shards", value=str(self.bot.shard_count or 1))
        e.add_field(name="Tickets", value=f"{tickets['c']:,} total · {tickets['o'] or 0:,} open")
        e.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f}ms")
        e.add_field(name="Uptime", value=human_delta(utcnow() - self.bot.started_at))
        e.add_field(name="Memory", value=memory)
        e.add_field(name="Database", value=f"{db_size:.2f} MB")
        e.add_field(name="Maintenance", value="🛠️ On" if db.get_state("maintenance") == "1" else "Off")
        e.set_footer(text=f"v{config.VERSION} · Python {platform.python_version()} · discord.py {discord.__version__}")
        await reply(inter, embed=e)

    @app_commands.command(name="servers", description="List the servers the bot is in (biggest first)")
    async def servers(self, inter: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        per_page = 15
        pages = max(1, math.ceil(len(guilds) / per_page))
        page = min(page, pages)
        counts = {r["guild_id"]: r["c"] for r in db.all("SELECT guild_id, COUNT(*) c FROM tickets GROUP BY guild_id")}
        start = (page - 1) * per_page
        lines = [f"`{i}.` **{discord.utils.escape_markdown(g.name[:40])}** · `{g.id}`\n"
                 f"   {g.member_count or 0:,} members · {counts.get(g.id, 0):,} tickets"
                 for i, g in enumerate(guilds[start:start + per_page], start=start + 1)]
        e = discord.Embed(title=f"🌐 Servers ({len(guilds):,})", description="\n".join(lines) or "None",
                          color=config.COLOR)
        e.set_footer(text=f"Page {page}/{pages} · use page: to see more")
        await reply(inter, embed=e)

    @app_commands.command(name="server", description="Details about one server")
    @app_commands.describe(server_id="The server's ID")
    async def server(self, inter: discord.Interaction, server_id: str):
        gid = parse_id(server_id)
        if gid is None:
            return await reply(inter, "That's not a valid ID.")
        guild = self.bot.get_guild(gid)
        cfg = db.one("SELECT * FROM guilds WHERE guild_id=?", (gid,))
        counts = db.one("SELECT COUNT(*) c, SUM(status='open') o, AVG(rating) r FROM tickets WHERE guild_id=?", (gid,))
        e = discord.Embed(title=guild.name if guild else f"Server {gid}", color=config.COLOR)
        e.add_field(name="In server", value="✅ Yes" if guild else "❌ No")
        if guild:
            e.add_field(name="Members", value=f"{guild.member_count or 0:,}")
            e.add_field(name="Owner", value=f"<@{guild.owner_id}> `{guild.owner_id}`")
            if guild.me.joined_at:
                e.add_field(name="Joined", value=f"<t:{int(guild.me.joined_at.timestamp())}:R>")
            if guild.icon:
                e.set_thumbnail(url=guild.icon.url)
        e.add_field(name="Set up", value="✅ Yes" if cfg and cfg["staff_role_id"] else "❌ No")
        e.add_field(name="Tickets", value=f"{counts['c']:,} total · {counts['o'] or 0:,} open")
        e.add_field(name="Avg rating", value=f"{counts['r']:.2f} ⭐" if counts["r"] else "—")
        e.add_field(name="Ticket types", value=str(len(db.categories(gid))))
        e.add_field(name="Blacklisted", value="⛔ Yes" if db.blacklisted("guild", gid) else "No")
        await reply(inter, embed=e)

    # ---- moderation ----

    @app_commands.command(name="leave", description="Make the bot leave a server")
    @app_commands.describe(server_id="The server's ID", also_blacklist="Stop it from being added back")
    async def leave(self, inter: discord.Interaction, server_id: str, also_blacklist: bool = False,
                    reason: Optional[str] = None):
        gid = parse_id(server_id)
        guild = self.bot.get_guild(gid) if gid else None
        if guild is None:
            return await reply(inter, "I'm not in a server with that ID.")
        if also_blacklist:
            db.blacklist_add("guild", gid, reason, inter.user.id)
        await guild.leave()
        await reply(inter, f"👋 Left **{guild.name}**" + (" and blacklisted it." if also_blacklist else "."))

    @app_commands.command(name="blacklist", description="Block a user or server from using the bot")
    @app_commands.describe(action="What to do", kind="User or server", target_id="Their ID", reason="Why (optional)")
    @app_commands.choices(
        action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"),
                app_commands.Choice(name="list", value="list")],
        kind=KIND_CHOICES)
    async def blacklist(self, inter: discord.Interaction, action: app_commands.Choice[str],
                        kind: Optional[app_commands.Choice[str]] = None, target_id: Optional[str] = None,
                        reason: Optional[str] = None):
        if action.value == "list":
            rows = db.blacklist_all()
            lines = [f"{'👤' if r['kind'] == 'user' else '🌐'} `{r['target_id']}` — {r['reason'] or 'no reason'}"
                     for r in rows[:40]]
            return await reply(inter, embed=discord.Embed(
                title=f"⛔ Blacklist ({len(rows)})", description="\n".join(lines) or "Empty", color=config.RED))

        tid = parse_id(target_id or "")
        if kind is None or tid is None:
            return await reply(inter, "Give a `kind` and a valid `target_id`.")
        if tid in config.DEV_IDS:
            return await reply(inter, "You can't blacklist a developer.")

        if action.value == "add":
            db.blacklist_add(kind.value, tid, reason, inter.user.id)
            msg = f"⛔ Blacklisted {kind.name} `{tid}`."
            guild = self.bot.get_guild(tid) if kind.value == "guild" else None
            if guild:
                await guild.leave()
                msg += f" Left **{guild.name}**."
            return await reply(inter, msg)

        removed = db.blacklist_remove(kind.value, tid)
        await reply(inter, f"✅ Removed {kind.name} `{tid}` from the blacklist." if removed else "They weren't blacklisted.")

    # ---- bot control ----

    @app_commands.command(name="maintenance", description="Pause the bot for everyone except developers")
    async def maintenance(self, inter: discord.Interaction, enabled: bool):
        db.set_state("maintenance", "1" if enabled else None)
        events = self.bot.get_cog("Events")
        if events:
            await events.apply_status()
        await reply(inter, "🛠️ Maintenance mode **on**. Only developers can use the bot." if enabled
                    else "✅ Maintenance mode **off**.")

    @app_commands.command(name="status", description="Set the bot's status text (leave empty to reset)")
    async def status(self, inter: discord.Interaction, text: Optional[app_commands.Range[str, 1, 120]] = None):
        db.set_state("custom_status", text)
        events = self.bot.get_cog("Events")
        if events:
            await events.apply_status()
        await reply(inter, f"✅ Status set to **{text}**." if text else "✅ Status reset to default.")

    @app_commands.command(name="broadcast", description="Send an announcement to every server's ticket log channel")
    @app_commands.describe(message="The announcement (use \\n for a new line)", title="Optional title")
    async def broadcast(self, inter: discord.Interaction, message: app_commands.Range[str, 1, 2000],
                        title: Optional[app_commands.Range[str, 1, 200]] = None):
        await inter.response.defer(ephemeral=True, thinking=True)
        e = discord.Embed(title=title or f"📢 {config.BOT_NAME} update", description=message.replace("\\n", "\n"),
                          color=config.COLOR, timestamp=utcnow())
        e.set_footer(text=f"Sent by the {config.BOT_NAME} team")
        sent = failed = 0
        for row in db.all("SELECT guild_id, log_channel_id FROM guilds WHERE log_channel_id IS NOT NULL"):
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["log_channel_id"]) if guild else None
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(embed=e)
                sent += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(0.5)  # stay well under rate limits
        await reply(inter, f"📢 Sent to **{sent}** servers" + (f" ({failed} failed)." if failed else "."))

    @app_commands.command(name="reload", description="Reload a feature module without restarting")
    @app_commands.choices(module=EXTENSION_CHOICES)
    async def reload(self, inter: discord.Interaction, module: app_commands.Choice[str]):
        targets = config.EXTENSIONS if module.value == "all" else [module.value]
        await inter.response.defer(ephemeral=True, thinking=True)
        results = []
        for ext in targets:
            try:
                await self.bot.reload_extension(ext)
                results.append(f"✅ {ext}")
            except Exception as e:  # show the error instead of crashing
                log.exception("Reload failed for %s", ext)
                results.append(f"❌ {ext}: `{type(e).__name__}: {str(e)[:150]}`")
        await reply(inter, "\n".join(results) + "\n\nRun `/dev sync` if you changed command names or options.")

    @app_commands.command(name="sync", description="Push slash command changes to Discord")
    async def sync(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        public, dev = await self.bot.sync_commands()
        await reply(inter, f"🔄 Synced **{public}** public commands and **{dev}** dev commands.\n"
                           "-# Public changes can take a minute to show up everywhere.")

    @app_commands.command(name="backup", description="Download a copy of the database")
    async def backup(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(os.path.dirname(db.path) or ".", f"backup-{stamp}.db")
        try:
            db.backup_to(path)
            if os.path.getsize(path) > 9 * 1024 * 1024:
                return await reply(inter, f"Backup saved on the server as `{path}` (too big to upload here).")
            await reply(inter, f"💾 Database backup ({stamp} UTC):", file=discord.File(path, filename=f"midtown-{stamp}.db"))
        finally:
            if os.path.exists(path) and os.path.getsize(path) <= 9 * 1024 * 1024:
                os.remove(path)

    @app_commands.command(name="shutdown", description="Turn the bot off (your host may restart it)")
    async def shutdown(self, inter: discord.Interaction, confirm: bool):
        if not confirm:
            return await reply(inter, "Set `confirm` to True to shut down.")
        await reply(inter, "👋 Shutting down...")
        log.warning("Shutdown requested by %s", inter.user)
        await self.bot.close()


class Dev(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = DevCommands(bot)

    async def cog_load(self):
        if not config.DEV_GUILD_ID:
            log.debug("DEV_GUILD_ID isn't set, so /dev commands are off (optional).")
            return
        if not config.DEV_IDS:
            log.warning("DEV_IDS isn't set, so nobody can use /dev commands.")
        self.bot.tree.add_command(self.group, guild=discord.Object(config.DEV_GUILD_ID), override=True)

    async def cog_unload(self):
        if config.DEV_GUILD_ID:
            self.bot.tree.remove_command("dev", guild=discord.Object(config.DEV_GUILD_ID))


async def setup(bot: commands.Bot):
    await bot.add_cog(Dev(bot))
