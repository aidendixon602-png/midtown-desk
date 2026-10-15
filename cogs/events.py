"""Bot-wide events: joining/leaving servers, blacklisted servers, status rotation."""

import discord
from discord.ext import commands, tasks

from midtown import config
from midtown.database import db
from midtown.console import ready_box
from midtown.utils import log, needs_auth, send_bot_log, send_welcome, utcnow


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.status_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.bot.announced_ready:
            self.bot.announced_ready = True
            await self.print_ready_summary()
        # Leave any servers that were blacklisted while the bot was offline
        for guild in self.bot.guilds:
            if db.blacklisted("guild", guild.id):
                await guild.leave()

    async def print_ready_summary(self):
        guilds = self.bot.guilds
        try:
            info = await self.bot.application_info()
            owner = ", ".join(m.name for m in info.team.members) if info.team else info.owner.name
        except discord.HTTPException:
            owner = "unknown"
        public, dev = self.bot.synced
        rows = [
            ("Bot", f"{self.bot.user}"),
            ("Owner", owner),
            ("Servers", f"{len(guilds):,} · {sum(g.member_count or 0 for g in guilds):,} members"),
            ("Commands", f"{public} slash" + (f" · {dev} dev" if dev else "") + f" · owner prefix {config.OWNER_PREFIX}"),
            ("Open tickets", str(db.one("SELECT COUNT(*) c FROM tickets WHERE status='open'")["c"])),
            ("Latency", f"{self.bot.latency * 1000:.0f}ms"),
        ]
        warnings = []
        if config.REQUIRE_AUTH:
            waiting = [g for g in guilds if needs_auth(g.id)]
            rows.insert(3, ("Authorized", f"{len(guilds) - len(waiting)} of {len(guilds)} servers"))
            if waiting:
                warnings.append(f"{len(waiting)} server(s) waiting for approval: type "
                                f"{config.OWNER_PREFIX}authorize in them, or {config.OWNER_PREFIX}servers")
        ready_box(rows, warnings)

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

        if needs_auth(guild.id):
            return  # the Auth cog asks the owner to approve this server first
        await send_welcome(self.bot, guild)

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
