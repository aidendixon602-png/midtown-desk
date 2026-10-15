"""Bot-wide events: joining/leaving servers, blacklisted servers, status rotation."""

import discord
from discord.ext import commands, tasks

from midtown import config
from midtown.database import db
from midtown.utils import link_buttons, log, send_bot_log, utcnow


def first_writable_channel(guild: discord.Guild) -> discord.TextChannel | None:
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if perms.send_messages and perms.embed_links:
            return channel
    return None


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.status_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Ready as %s in %d servers", self.bot.user, len(self.bot.guilds))
        # Leave any servers that were blacklisted while the bot was offline
        for guild in self.bot.guilds:
            if db.blacklisted("guild", guild.id):
                await guild.leave()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        banned = db.blacklisted("guild", guild.id)
        e = discord.Embed(title="📥 Joined a server" if not banned else "⛔ Joined a blacklisted server (leaving)",
                          color=config.GREEN if not banned else config.RED, timestamp=utcnow())
        e.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`")
        e.add_field(name="Members", value=f"{guild.member_count or 0:,}")
        e.add_field(name="Owner", value=f"<@{guild.owner_id}>\n`{guild.owner_id}`")
        e.set_footer(text=f"Now in {len(self.bot.guilds):,} servers")
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        await send_bot_log(self.bot, e)

        if banned:
            await guild.leave()
            return

        channel = first_writable_channel(guild)
        if channel:
            welcome = discord.Embed(
                title=f"👋 Thanks for adding {config.BOT_NAME}!", color=config.COLOR,
                description="Get tickets running in under a minute:\n\n"
                            "**1.** `/setup` — choose your staff role\n"
                            "**2.** `/panel` — post the ticket menu where members can see it\n"
                            "**3.** `/category` — customize the ticket types (optional)\n\n"
                            "Type `/help` any time to see every command.")
            try:
                await channel.send(embed=welcome, view=link_buttons(self.bot))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        # Settings and ticket history are kept, so everything still works if they add the bot back.
        e = discord.Embed(title="📤 Left a server", color=config.RED, timestamp=utcnow())
        e.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`")
        e.add_field(name="Members", value=f"{guild.member_count or 0:,}")
        e.set_footer(text=f"Now in {len(self.bot.guilds):,} servers")
        await send_bot_log(self.bot, e)

    @tasks.loop(minutes=10)
    async def status_loop(self):
        await self.apply_status()

    @status_loop.before_loop
    async def before_status(self):
        await self.bot.wait_until_ready()

    async def apply_status(self):
        if db.get_state("maintenance") == "1":
            activity = discord.Game(name="🛠️ Under maintenance")
            status = discord.Status.idle
        else:
            text = db.get_state("custom_status") or f"/help • {len(self.bot.guilds):,} servers"
            activity = discord.Activity(type=discord.ActivityType.watching, name=text)
            status = discord.Status.online
        await self.bot.change_presence(activity=activity, status=status)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
