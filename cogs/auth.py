"""
Server authorization: the bot only works in servers the owner approves.

- When someone adds the bot, the server sees a "waiting for approval" message and the
  owner gets a DM with Authorize / Deny buttons.
- Hidden owner-only text commands (prefix , by default, or @mention the bot):
    ,authorize [server_id] [note]   approve a server (no ID = the server you're in)
    ,revoke <server_id> [leave]     remove approval (add "leave" to also make the bot leave)
    ,servers                        every server the bot is in, with its status
    ,authorized                     approved servers
Only you (the Discord application owner, or anyone in DEV_IDS) can use them. Everyone else gets no reply.
"""

import asyncio

import discord
from discord.ext import commands

from midtown import config
from midtown.database import db
from midtown.utils import (OWNER_IDS, first_writable_channel, is_dev, log, needs_auth, send_bot_log,
                           send_welcome, static_view, utcnow)


def approval_buttons(guild_id: int) -> discord.ui.View:
    return static_view(
        discord.ui.Button(label="Authorize", style=discord.ButtonStyle.success, emoji="✅",
                          custom_id=f"md:auth:{guild_id}:yes"),
        discord.ui.Button(label="Deny & leave", style=discord.ButtonStyle.danger, emoji="⛔",
                          custom_id=f"md:auth:{guild_id}:no"))


def guild_card(guild: discord.Guild, title: str, color: int) -> discord.Embed:
    e = discord.Embed(title=title, color=color, timestamp=utcnow())
    e.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`")
    e.add_field(name="Members", value=f"{guild.member_count or 0:,}")
    e.add_field(name="Owner", value=f"<@{guild.owner_id}>\n`{guild.owner_id}`")
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    return e


class Auth(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        asyncio.create_task(self.load_owners())

    async def load_owners(self):
        await self.bot.wait_until_ready()
        info = await self.bot.application_info()
        if info.team:
            OWNER_IDS.update(m.id for m in info.team.members)
        else:
            OWNER_IDS.add(info.owner.id)
        log.debug("Bot owners: %s", ", ".join(map(str, OWNER_IDS)))

    async def owners(self) -> list[discord.User]:
        users = []
        for uid in OWNER_IDS | config.DEV_IDS:
            try:
                users.append(self.bot.get_user(uid) or await self.bot.fetch_user(uid))
            except discord.HTTPException:
                pass
        return users

    # ---- new server joins ----

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if not needs_auth(guild.id) or db.blacklisted("guild", guild.id):
            return

        channel = first_writable_channel(guild)
        if channel:
            try:
                await channel.send(embed=discord.Embed(
                    title=f"🔒 {config.BOT_NAME} is waiting for approval", color=config.YELLOW,
                    description=f"Thanks for adding {config.BOT_NAME}! This bot only works in approved servers.\n\n"
                                f"The bot owner has been notified. Once this server is approved, you'll get a "
                                f"message here and can run `/setup`.\n\n**Server ID:** `{guild.id}`"))
            except discord.HTTPException:
                pass

        card = guild_card(guild, "🔔 New server wants to use the bot", config.YELLOW)
        card.set_footer(text=f"Or reply later with {config.OWNER_PREFIX}authorize {guild.id}")
        for owner in await self.owners():
            try:
                await owner.send(embed=card, view=approval_buttons(guild.id))
            except discord.HTTPException:
                pass  # owner has DMs closed; the bot log channel still gets it
        await send_bot_log(self.bot, card)

    # ---- approve / deny buttons in the owner's DMs ----

    @commands.Cog.listener()
    async def on_interaction(self, inter: discord.Interaction):
        if inter.type != discord.InteractionType.component:
            return
        cid = (inter.data or {}).get("custom_id", "")
        if not cid.startswith("md:auth:"):
            return
        if not is_dev(inter.user):
            return await inter.response.send_message("Only the bot owner can do that.", ephemeral=True)
        _, _, gid, choice = cid.split(":")
        guild = self.bot.get_guild(int(gid))
        if guild is None:
            return await inter.response.edit_message(content="I'm no longer in that server.", view=None)
        if choice == "yes":
            await self.approve(guild, inter.user)
            await inter.response.edit_message(content=f"✅ Authorized **{guild.name}**.", view=None)
        else:
            await self.deny(guild, inter.user)
            await inter.response.edit_message(content=f"⛔ Denied and left **{guild.name}**.", view=None)

    async def approve(self, guild: discord.Guild, by: discord.abc.User, note: str | None = None):
        db.authorize(guild.id, by.id, note)
        await send_welcome(self.bot, guild)
        await send_bot_log(self.bot, guild_card(guild, "✅ Server authorized", config.GREEN))

    async def deny(self, guild: discord.Guild, by: discord.abc.User):
        db.deauthorize(guild.id)
        channel = first_writable_channel(guild)
        if channel:
            try:
                await channel.send(f"⛔ This server wasn't approved to use {config.BOT_NAME}, so I'm leaving. 👋")
            except discord.HTTPException:
                pass
        await send_bot_log(self.bot, guild_card(guild, "⛔ Server denied (left)", config.RED))
        await guild.leave()

    # ---- hidden owner-only text commands ----

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_dev(ctx.author)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            return  # stay silent for everyone except the owner
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(f"Usage: `{config.OWNER_PREFIX}{ctx.command.qualified_name} {ctx.command.signature}`")
            return
        log.exception("Owner command error", exc_info=error)
        await ctx.reply(f"Error: `{error}`")

    @commands.command(name="authorize", aliases=["auth", "approve"])
    async def authorize_cmd(self, ctx: commands.Context, server_id: int | None = None, *, note: str | None = None):
        gid = server_id or (ctx.guild.id if ctx.guild else None)
        if gid is None:
            return await ctx.reply("Give a server ID, or run this inside the server.")
        db.blacklist_remove("guild", gid)
        guild = self.bot.get_guild(gid)
        if guild:
            await self.approve(guild, ctx.author, note)
            await ctx.reply(f"✅ Authorized **{guild.name}** (`{gid}`).")
        else:
            db.authorize(gid, ctx.author.id, note)
            await ctx.reply(f"✅ Pre-authorized `{gid}`. It'll work as soon as they add the bot.")

    @commands.command(name="revoke", aliases=["deauthorize", "unauth"])
    async def revoke_cmd(self, ctx: commands.Context, server_id: int, leave: str | None = None):
        removed = db.deauthorize(server_id)
        guild = self.bot.get_guild(server_id)
        msg = f"🔒 Removed approval for `{server_id}`." if removed else f"`{server_id}` wasn't authorized."
        if guild and leave and leave.lower() == "leave":
            await guild.leave()
            msg += f" Left **{guild.name}**."
        await ctx.reply(msg)

    @commands.command(name="servers")
    async def servers_cmd(self, ctx: commands.Context):
        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        lines = [f"{'✅' if db.is_authorized(g.id) else '🔒'} **{discord.utils.escape_markdown(g.name[:40])}** "
                 f"`{g.id}` · {g.member_count or 0:,} members" for g in guilds[:40]]
        e = discord.Embed(title=f"Servers ({len(guilds)})", color=config.COLOR,
                          description="\n".join(lines) or "None")
        e.set_footer(text=f"✅ authorized · 🔒 waiting · {config.OWNER_PREFIX}authorize <id> to approve")
        await ctx.reply(embed=e)

    @commands.command(name="authorized")
    async def authorized_cmd(self, ctx: commands.Context):
        rows = db.authorized_all()
        lines = []
        for r in rows[:40]:
            guild = self.bot.get_guild(r["guild_id"])
            name = discord.utils.escape_markdown(guild.name[:40]) if guild else "*(bot not in server)*"
            lines.append(f"✅ {name} `{r['guild_id']}`" + (f" — {r['note']}" if r["note"] else ""))
        await ctx.reply(embed=discord.Embed(title=f"Authorized servers ({len(rows)})", color=config.GREEN,
                                            description="\n".join(lines) or "None yet."))


async def setup(bot: commands.Bot):
    await bot.add_cog(Auth(bot))
