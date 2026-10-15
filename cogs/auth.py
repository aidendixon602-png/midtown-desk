"""
Server authorization: the bot only works in servers the owner approves.

- Servers you own are approved automatically.
- When someone else adds the bot, their server sees a "waiting for approval" message and you get
  a DM with Authorize / Deny buttons. You can also use /owner authorize.
"""

import asyncio

import discord
from discord.ext import commands

from midtown import config
from midtown.database import db
from midtown.utils import (OWNER_IDS, all_owner_ids, first_writable_channel, is_dev, log, needs_auth,
                           send_bot_log, send_welcome, static_view, utcnow)


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
        self.owners_loaded = asyncio.Event()

    async def cog_load(self):
        asyncio.create_task(self.load_owners())

    async def load_owners(self):
        await self.bot.wait_until_ready()
        try:
            info = await self.bot.application_info()
            if info.team:
                OWNER_IDS.update(m.id for m in info.team.members)
            else:
                OWNER_IDS.add(info.owner.id)
        except discord.HTTPException as e:
            log.warning("Couldn't look up the bot owner: %s", e)
        # Servers you own never need approval
        for guild in self.bot.guilds:
            if guild.owner_id in all_owner_ids() and needs_auth(guild.id):
                db.authorize(guild.id, guild.owner_id, "Owned by the bot owner")
        self.owners_loaded.set()

    async def owner_users(self) -> list[discord.User]:
        users = []
        for uid in all_owner_ids():
            try:
                users.append(self.bot.get_user(uid) or await self.bot.fetch_user(uid))
            except discord.HTTPException:
                pass
        return users

    # ---- new server joins ----

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if guild.owner_id in all_owner_ids():
            db.authorize(guild.id, guild.owner_id, "Owned by the bot owner")
            return
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
        card.set_footer(text="Or later with /owner authorize")
        for owner in await self.owner_users():
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
            await self.deny(guild)
            await inter.response.edit_message(content=f"⛔ Denied and left **{guild.name}**.", view=None)

    async def approve(self, guild: discord.Guild, by: discord.abc.User, note: str | None = None):
        db.blacklist_remove("guild", guild.id)
        db.authorize(guild.id, by.id, note)
        await send_welcome(self.bot, guild)
        await send_bot_log(self.bot, guild_card(guild, "✅ Server authorized", config.GREEN))

    async def deny(self, guild: discord.Guild):
        db.deauthorize(guild.id)
        channel = first_writable_channel(guild)
        if channel:
            try:
                await channel.send(f"⛔ This server wasn't approved to use {config.BOT_NAME}, so I'm leaving. 👋")
            except discord.HTTPException:
                pass
        await send_bot_log(self.bot, guild_card(guild, "⛔ Server denied (left)", config.RED))
        await guild.leave()


async def setup(bot: commands.Bot):
    await bot.add_cog(Auth(bot))
