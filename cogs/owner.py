"""
Owner-only commands.

/owner ...   Slash commands. They only appear in servers YOU own (detected automatically),
             plus DEV_GUILD_ID if you set one, so nobody else ever sees them.
,help ...    The same commands as hidden text commands (prefix from OWNER_PREFIX), handy in DMs.
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
from midtown.utils import all_owner_ids, human_delta, is_dev, log, needs_auth, reply, utcnow

try:
    import resource  # not available on Windows
except ImportError:
    resource = None

P = config.OWNER_PREFIX


# ======================================================================================
# Shared actions (used by both /owner and the , commands)
# ======================================================================================

def stats_embed(bot: commands.Bot) -> discord.Embed:
    guilds = bot.guilds
    t = db.one("SELECT COUNT(*) c, SUM(status='open') o FROM tickets")
    size = os.path.getsize(db.path) / 1024 / 1024 if os.path.exists(db.path) else 0
    memory = f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB" if resource else "n/a"
    waiting = sum(1 for g in guilds if needs_auth(g.id))
    e = discord.Embed(title=f"📊 {config.BOT_NAME} stats", color=config.COLOR, timestamp=utcnow())
    e.add_field(name="Servers", value=f"{len(guilds):,}" + (f" ({waiting} waiting)" if waiting else ""))
    e.add_field(name="Members", value=f"{sum(g.member_count or 0 for g in guilds):,}")
    e.add_field(name="Tickets", value=f"{t['c']:,} total · {t['o'] or 0:,} open")
    e.add_field(name="Uptime", value=human_delta(utcnow() - bot.started_at))
    e.add_field(name="Latency", value=f"{bot.latency * 1000:.0f}ms")
    e.add_field(name="Memory", value=memory)
    e.add_field(name="Database", value=f"{size:.2f} MB")
    e.add_field(name="Maintenance", value="🛠️ On" if db.get_state("maintenance") == "1" else "Off")
    e.set_footer(text=f"v{config.VERSION} · Python {platform.python_version()} · discord.py {discord.__version__}")
    return e


def servers_embed(bot: commands.Bot, page: int = 1) -> discord.Embed:
    guilds = sorted(bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
    per = 20
    pages = max(1, math.ceil(len(guilds) / per))
    page = max(1, min(page, pages))
    start = (page - 1) * per
    lines = [f"{'🔒' if needs_auth(g.id) else '✅'} **{discord.utils.escape_markdown(g.name[:40])}** "
             f"`{g.id}` · {g.member_count or 0:,} members" for g in guilds[start:start + per]]
    e = discord.Embed(title=f"🌐 Servers ({len(guilds):,})", color=config.COLOR, description="\n".join(lines) or "None")
    e.set_footer(text=f"✅ approved · 🔒 waiting · page {page}/{pages}")
    return e


def authorized_embed(bot: commands.Bot) -> discord.Embed:
    rows = db.authorized_all()
    lines = []
    for r in rows[:40]:
        guild = bot.get_guild(r["guild_id"])
        name = discord.utils.escape_markdown(guild.name[:40]) if guild else "*(bot not in server)*"
        lines.append(f"✅ {name} `{r['guild_id']}`" + (f" — {r['note']}" if r["note"] else ""))
    return discord.Embed(title=f"✅ Approved servers ({len(rows)})", color=config.GREEN,
                         description="\n".join(lines) or "None yet.")


def server_embed(bot: commands.Bot, gid: int) -> discord.Embed:
    guild = bot.get_guild(gid)
    c = db.one("SELECT COUNT(*) c, SUM(status='open') o FROM tickets WHERE guild_id=?", (gid,))
    cfg = db.one("SELECT staff_role_id FROM guilds WHERE guild_id=?", (gid,))
    e = discord.Embed(title=guild.name if guild else f"Server {gid}", color=config.COLOR)
    e.add_field(name="Bot in server", value="✅ Yes" if guild else "❌ No")
    e.add_field(name="Approved", value="🔒 No" if needs_auth(gid) else "✅ Yes")
    e.add_field(name="Set up", value="✅ Yes" if cfg and cfg["staff_role_id"] else "❌ No")
    if guild:
        e.add_field(name="Members", value=f"{guild.member_count or 0:,}")
        e.add_field(name="Owner", value=f"<@{guild.owner_id}> `{guild.owner_id}`")
        if guild.me.joined_at:
            e.add_field(name="Added", value=f"<t:{int(guild.me.joined_at.timestamp())}:R>")
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
    e.add_field(name="Tickets", value=f"{c['c']:,} total · {c['o'] or 0:,} open")
    return e


async def do_authorize(bot: commands.Bot, gid: int, by: discord.abc.User, note: str | None) -> str:
    guild = bot.get_guild(gid)
    if guild:
        await bot.get_cog("Auth").approve(guild, by, note)
        return f"✅ Approved **{guild.name}** (`{gid}`)."
    db.blacklist_remove("guild", gid)
    db.authorize(gid, by.id, note)
    return f"✅ Pre-approved `{gid}`. It'll work as soon as they add the bot."


async def do_revoke(bot: commands.Bot, gid: int, leave: bool) -> str:
    removed = db.deauthorize(gid)
    guild = bot.get_guild(gid)
    msg = f"🔒 Removed approval for `{gid}`." if removed else f"`{gid}` wasn't approved."
    if guild and leave:
        await guild.leave()
        msg += f" Left **{guild.name}**."
    return msg


async def do_leave(bot: commands.Bot, gid: int, block: bool) -> str:
    guild = bot.get_guild(gid)
    if block:
        db.blacklist_add("guild", gid, "Blocked by owner", 0)
        db.deauthorize(gid)
    if guild is None:
        return f"⛔ Blocked server `{gid}`." if block else "I'm not in a server with that ID."
    await guild.leave()
    return f"👋 Left **{guild.name}**" + (" and blocked it from adding the bot again." if block else ".")


async def set_maintenance(bot: commands.Bot, on: bool) -> str:
    db.set_state("maintenance", "1" if on else None)
    if events := bot.get_cog("Events"):
        await events.apply_status()
    return "🛠️ Maintenance **on**. Only you can use the bot." if on else "✅ Maintenance **off**."


async def set_status(bot: commands.Bot, text: str | None) -> str:
    db.set_state("custom_status", text[:120] if text else None)
    if events := bot.get_cog("Events"):
        await events.apply_status()
    return f"✅ Status set to **{text[:120]}**." if text else "✅ Status reset."


def do_block(user_id: int, reason: str | None, by: int) -> str:
    if user_id in all_owner_ids():
        return "You can't block an owner."
    db.blacklist_add("user", user_id, reason, by)
    return f"⛔ <@{user_id}> can no longer use the bot anywhere."


def do_unblock(user_id: int) -> str:
    return f"✅ Unblocked <@{user_id}>." if db.blacklist_remove("user", user_id) else "They weren't blocked."


def blocklist_embed() -> discord.Embed:
    rows = db.blacklist_all()
    lines = [f"{'👤' if r['kind'] == 'user' else '🌐'} `{r['target_id']}` — {r['reason'] or 'no reason'}" for r in rows[:40]]
    return discord.Embed(title=f"⛔ Blocked ({len(rows)})", color=config.RED, description="\n".join(lines) or "Nobody.")


async def send_backup(user: discord.abc.User) -> str:
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(os.path.dirname(db.path) or ".", f"backup-{stamp}.db")
    db.backup_to(path)
    try:
        if os.path.getsize(path) > 9 * 1024 * 1024:
            return f"Backup is too big to send. Saved on the host as `{path}`."
        await user.send(f"💾 Database backup ({stamp} UTC)", file=discord.File(path, f"midtown-{stamp}.db"))
        return "💾 Sent the backup to your DMs."
    except discord.Forbidden:
        return "I couldn't DM you. Open your DMs and try again."
    finally:
        if os.path.exists(path) and os.path.getsize(path) <= 9 * 1024 * 1024:
            os.remove(path)


async def reload_all(bot: commands.Bot) -> str:
    results = []
    for ext in config.EXTENSIONS:
        try:
            await bot.reload_extension(ext)
            results.append(f"✅ {ext.split('.')[-1]}")
        except Exception as e:
            log.exception("Reload failed for %s", ext)
            results.append(f"❌ {ext.split('.')[-1]}: `{type(e).__name__}: {str(e)[:120]}`")
    return "\n".join(results) + "\n-# Run sync if you changed slash command names or options."


async def do_sync(bot: commands.Bot) -> str:
    public, owner = await bot.sync_commands()
    return f"🔄 Synced **{public}** public commands and **{owner}** owner command sets."


async def do_broadcast(bot: commands.Bot, message: str, title: str | None) -> str:
    e = discord.Embed(title=title or f"📢 {config.BOT_NAME} update", description=message.replace("\\n", "\n"),
                      color=config.COLOR, timestamp=utcnow())
    e.set_footer(text=f"From the {config.BOT_NAME} team")
    sent = failed = 0
    for row in db.all("SELECT guild_id, log_channel_id FROM guilds WHERE log_channel_id IS NOT NULL"):
        guild = bot.get_guild(row["guild_id"])
        channel = guild.get_channel(row["log_channel_id"]) if guild else None
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            await channel.send(embed=e)
            sent += 1
        except discord.HTTPException:
            failed += 1
        await asyncio.sleep(0.5)  # stay under rate limits
    return f"📢 Sent to **{sent}** servers" + (f" ({failed} failed)." if failed else ".")


def parse_id(value: str) -> int | None:
    value = value.strip().strip("<@!#&>")
    return int(value) if value.isdigit() else None


# ======================================================================================
# /owner slash commands
# ======================================================================================

class OwnerCommands(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(name="owner", description="Bot owner commands", guild_only=True,
                         default_permissions=discord.Permissions(administrator=True))
        self.bot = bot

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if is_dev(inter.user):
            return True
        await reply(inter, "⛔ Only the bot owner can use these.")
        return False

    async def _id(self, inter: discord.Interaction, value: str | None, default_here: bool = False) -> int | None:
        if value is None and default_here:
            return inter.guild_id
        gid = parse_id(value or "")
        if gid is None:
            await reply(inter, "That's not a valid ID. Right-click a server or user → Copy ID.")
        return gid

    # -- approval --

    @app_commands.command(name="authorize", description="Approve a server to use the bot")
    @app_commands.describe(server_id="Server ID (leave empty for this server)", note="Optional note to remember why")
    async def authorize(self, inter: discord.Interaction, server_id: Optional[str] = None,
                        note: Optional[str] = None):
        gid = await self._id(inter, server_id, default_here=True)
        if gid:
            await reply(inter, await do_authorize(self.bot, gid, inter.user, note))

    @app_commands.command(name="revoke", description="Remove a server's approval")
    @app_commands.describe(server_id="Server ID", leave="Also make the bot leave that server")
    async def revoke(self, inter: discord.Interaction, server_id: str, leave: bool = False):
        gid = await self._id(inter, server_id)
        if gid:
            await reply(inter, await do_revoke(self.bot, gid, leave))

    @app_commands.command(name="servers", description="Every server the bot is in (✅ approved, 🔒 waiting)")
    async def servers(self, inter: discord.Interaction, page: app_commands.Range[int, 1, 500] = 1):
        await reply(inter, embed=servers_embed(self.bot, page))

    @app_commands.command(name="approved", description="List approved servers")
    async def approved(self, inter: discord.Interaction):
        await reply(inter, embed=authorized_embed(self.bot))

    # -- info --

    @app_commands.command(name="stats", description="Bot-wide stats")
    async def stats(self, inter: discord.Interaction):
        await reply(inter, embed=stats_embed(self.bot))

    @app_commands.command(name="server", description="Details about one server")
    async def server(self, inter: discord.Interaction, server_id: str):
        gid = await self._id(inter, server_id)
        if gid:
            await reply(inter, embed=server_embed(self.bot, gid))

    # -- control --

    @app_commands.command(name="leave", description="Make the bot leave a server")
    @app_commands.describe(server_id="Server ID", block="Stop them from adding the bot again")
    async def leave(self, inter: discord.Interaction, server_id: str, block: bool = False):
        gid = await self._id(inter, server_id)
        if gid:
            await reply(inter, await do_leave(self.bot, gid, block))

    @app_commands.command(name="maintenance", description="Pause the bot for everyone except you")
    async def maintenance(self, inter: discord.Interaction, enabled: bool):
        await reply(inter, await set_maintenance(self.bot, enabled))

    @app_commands.command(name="status", description="Set the bot's status text (empty = reset)")
    async def status(self, inter: discord.Interaction, text: Optional[app_commands.Range[str, 1, 120]] = None):
        await reply(inter, await set_status(self.bot, text))

    @app_commands.command(name="block", description="Block a user from using the bot everywhere")
    async def block(self, inter: discord.Interaction, user: discord.User, reason: Optional[str] = None):
        await reply(inter, do_block(user.id, reason, inter.user.id))

    @app_commands.command(name="unblock", description="Unblock a user")
    async def unblock(self, inter: discord.Interaction, user: discord.User):
        await reply(inter, do_unblock(user.id))

    @app_commands.command(name="blocked", description="Blocked users and servers")
    async def blocked(self, inter: discord.Interaction):
        await reply(inter, embed=blocklist_embed())

    @app_commands.command(name="broadcast", description="Post an announcement in every server's ticket log channel")
    @app_commands.describe(message="Use \\n for a new line")
    async def broadcast(self, inter: discord.Interaction, message: app_commands.Range[str, 1, 2000],
                        title: Optional[app_commands.Range[str, 1, 200]] = None):
        await inter.response.defer(ephemeral=True, thinking=True)
        await reply(inter, await do_broadcast(self.bot, message, title))

    # -- bot --

    @app_commands.command(name="backup", description="DM yourself a copy of the database")
    async def backup(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        await reply(inter, await send_backup(inter.user))

    @app_commands.command(name="reload", description="Reload all code without restarting")
    async def reload(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        await reply(inter, await reload_all(self.bot))

    @app_commands.command(name="sync", description="Push slash command changes to Discord")
    async def sync(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        await reply(inter, await do_sync(self.bot))


# ======================================================================================
# Cog: registers /owner in your servers + the hidden , text commands
# ======================================================================================

class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = OwnerCommands(bot)

    async def cog_load(self):
        asyncio.create_task(self.register_owner_servers())

    async def cog_unload(self):
        for gid in list(self.bot.owner_guild_ids):
            self.bot.tree.remove_command("owner", guild=discord.Object(gid))

    def owner_server_ids(self) -> set[int]:
        ids = {g.id for g in self.bot.guilds if g.owner_id in all_owner_ids()}
        if config.DEV_GUILD_ID:
            ids.add(config.DEV_GUILD_ID)
        return ids

    async def register_owner_servers(self):
        await self.bot.wait_until_ready()
        auth = self.bot.get_cog("Auth")
        if auth:
            await auth.owners_loaded.wait()
        for gid in self.owner_server_ids():
            await self.add_to(gid)

    async def add_to(self, gid: int):
        guild = discord.Object(gid)
        self.bot.tree.add_command(self.group, guild=guild, override=True)
        self.bot.owner_guild_ids.add(gid)
        try:
            await self.bot.tree.sync(guild=guild)
        except discord.HTTPException as e:
            log.warning("Couldn't add /owner to server %s: %s", gid, e)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if guild.owner_id in all_owner_ids():
            await self.add_to(guild.id)

    # ---- hidden , text commands ----

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_dev(ctx.author)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return  # silent for everyone except the owner
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            return await ctx.reply(f"Usage: `{P}{ctx.command.qualified_name} {ctx.command.signature}`")
        log.exception("Owner command error", exc_info=error)
        await ctx.reply(f"Error: `{error}`")

    @commands.command(name="help", aliases=["h", "commands"])
    async def help_cmd(self, ctx: commands.Context):
        e = discord.Embed(title=f"🛡️ {config.BOT_NAME} — Owner commands", color=config.COLOR,
                          description="Use **`/owner`** in your own servers, or these text commands anywhere "
                                      f"(including DMs). Only you can use them.")
        e.add_field(name="🔐 Approval", inline=False, value=(
            f"`{P}authorize [id]` · `{P}revoke <id> [leave]` · `{P}servers` · `{P}approved`"))
        e.add_field(name="📊 Info", inline=False, value=f"`{P}stats` · `{P}server <id>`")
        e.add_field(name="🛠️ Control", inline=False, value=(
            f"`{P}leave <id> [block]` · `{P}maintenance on/off` · `{P}status [text]`\n"
            f"`{P}block <user_id>` · `{P}unblock <user_id>` · `{P}blocked`"))
        e.add_field(name="⚙️ Bot", inline=False, value=f"`{P}backup` · `{P}reload` · `{P}sync`")
        e.set_footer(text=f"v{config.VERSION} · {len(self.bot.guilds)} servers")
        await ctx.reply(embed=e, mention_author=False)

    @commands.command(name="authorize", aliases=["auth", "approve"])
    async def authorize_cmd(self, ctx: commands.Context, server_id: int | None = None, *, note: str | None = None):
        gid = server_id or (ctx.guild.id if ctx.guild else None)
        if gid is None:
            return await ctx.reply("Give a server ID, or run this inside the server.")
        await ctx.reply(await do_authorize(self.bot, gid, ctx.author, note), mention_author=False)

    @commands.command(name="revoke", aliases=["unauth"])
    async def revoke_cmd(self, ctx: commands.Context, server_id: int, leave: str | None = None):
        await ctx.reply(await do_revoke(self.bot, server_id, (leave or "").lower() == "leave"), mention_author=False)

    @commands.command(name="servers", aliases=["list"])
    async def servers_cmd(self, ctx: commands.Context, page: int = 1):
        await ctx.reply(embed=servers_embed(self.bot, page), mention_author=False)

    @commands.command(name="approved", aliases=["authorized"])
    async def approved_cmd(self, ctx: commands.Context):
        await ctx.reply(embed=authorized_embed(self.bot), mention_author=False)

    @commands.command(name="stats")
    async def stats_cmd(self, ctx: commands.Context):
        await ctx.reply(embed=stats_embed(self.bot), mention_author=False)

    @commands.command(name="server", aliases=["guild"])
    async def server_cmd(self, ctx: commands.Context, server_id: int):
        await ctx.reply(embed=server_embed(self.bot, server_id), mention_author=False)

    @commands.command(name="leave")
    async def leave_cmd(self, ctx: commands.Context, server_id: int, block: str | None = None):
        msg = await do_leave(self.bot, server_id, (block or "").lower() == "block")
        if ctx.guild is None or ctx.guild.id != server_id:
            await ctx.reply(msg, mention_author=False)

    @commands.command(name="maintenance", aliases=["maint"])
    async def maintenance_cmd(self, ctx: commands.Context, mode: str):
        await ctx.reply(await set_maintenance(self.bot, mode.lower() in ("on", "true", "yes", "1")),
                        mention_author=False)

    @commands.command(name="status")
    async def status_cmd(self, ctx: commands.Context, *, text: str | None = None):
        await ctx.reply(await set_status(self.bot, text), mention_author=False)

    @commands.command(name="block")
    async def block_cmd(self, ctx: commands.Context, user_id: int, *, reason: str | None = None):
        await ctx.reply(do_block(user_id, reason, ctx.author.id), mention_author=False)

    @commands.command(name="unblock")
    async def unblock_cmd(self, ctx: commands.Context, user_id: int):
        await ctx.reply(do_unblock(user_id), mention_author=False)

    @commands.command(name="blocked")
    async def blocked_cmd(self, ctx: commands.Context):
        await ctx.reply(embed=blocklist_embed(), mention_author=False)

    @commands.command(name="backup")
    async def backup_cmd(self, ctx: commands.Context):
        await ctx.reply(await send_backup(ctx.author), mention_author=False)

    @commands.command(name="reload")
    async def reload_cmd(self, ctx: commands.Context):
        await ctx.reply(await reload_all(self.bot), mention_author=False)

    @commands.command(name="sync")
    async def sync_cmd(self, ctx: commands.Context):
        await ctx.reply(await do_sync(self.bot), mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
