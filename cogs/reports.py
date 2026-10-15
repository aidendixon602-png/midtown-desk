"""Weekly stats report posted to each server's ticket log channel, plus /report to see it anytime."""

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from midtown import config
from midtown.database import db
from midtown.utils import log, reply, utcnow

WEEK = dt.timedelta(days=7)


def _count(sql: str, args) -> int:
    return db.one(sql, args)["c"] or 0


def _trend(now: int, before: int) -> str:
    if before == 0:
        return "" if now == 0 else " (new)"
    change = round((now - before) / before * 100)
    if change == 0:
        return " (same as last week)"
    return f" ({'▲' if change > 0 else '▼'} {abs(change)}% vs last week)"


def build_report(guild: discord.Guild, end: dt.datetime | None = None) -> discord.Embed:
    end = end or utcnow()
    start, prev_start = end - WEEK, end - 2 * WEEK
    gid, s, e, p = guild.id, start.isoformat(), end.isoformat(), prev_start.isoformat()

    opened = _count("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND opened_at>=? AND opened_at<?", (gid, s, e))
    opened_prev = _count("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND opened_at>=? AND opened_at<?", (gid, p, s))
    closed = _count("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='closed' AND closed_at>=? AND closed_at<?",
                    (gid, s, e))
    still_open = _count("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open'", (gid,))
    unclaimed = _count("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open' AND claimed_by IS NULL", (gid,))
    urgent = _count("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open' AND priority IN ('urgent','high')",
                    (gid,))
    ratings = db.one("SELECT AVG(rating) a, COUNT(rating) n FROM tickets WHERE guild_id=? AND rating IS NOT NULL "
                     "AND closed_at>=? AND closed_at<?", (gid, s, e))
    top_type = db.one("SELECT category, COUNT(*) c FROM tickets WHERE guild_id=? AND opened_at>=? AND opened_at<? "
                      "GROUP BY category ORDER BY c DESC LIMIT 1", (gid, s, e))
    hours = db.one("SELECT AVG((julianday(closed_at) - julianday(opened_at)) * 24) h FROM tickets "
                   "WHERE guild_id=? AND status='closed' AND closed_at>=? AND closed_at<?", (gid, s, e))["h"]
    staff = db.all("SELECT claimed_by, COUNT(*) c, AVG(rating) r FROM tickets WHERE guild_id=? AND status='closed' "
                   "AND claimed_by IS NOT NULL AND closed_at>=? AND closed_at<? "
                   "GROUP BY claimed_by ORDER BY c DESC LIMIT 5", (gid, s, e))

    embed = discord.Embed(
        title=f"📊 Weekly ticket report — {guild.name}", color=config.COLOR, timestamp=end,
        description=f"<t:{int(start.timestamp())}:D> → <t:{int(end.timestamp())}:D>")
    embed.add_field(name="📥 Opened", value=f"**{opened}**{_trend(opened, opened_prev)}")
    embed.add_field(name="🔒 Closed", value=f"**{closed}**")
    embed.add_field(name="⏱️ Avg time to close", value=f"**{hours:.1f}h**" if hours else "—")
    embed.add_field(name="⭐ Avg rating",
                    value=f"**{ratings['a']:.2f}** ({ratings['n']} ratings)" if ratings["n"] else "No ratings")
    embed.add_field(name="🏷️ Most common type", value=f"{top_type['category']} ({top_type['c']})" if top_type else "—")
    embed.add_field(name="📂 Still open", value=f"**{still_open}** · {unclaimed} unclaimed · {urgent} high/urgent")

    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    lines = [f"{medals[i]} <@{r['claimed_by']}> · {r['c']} closed" + (f" · {r['r']:.1f}⭐" if r["r"] else "")
             for i, r in enumerate(staff)]
    embed.add_field(name="🏆 Top staff this week", value="\n".join(lines) or "No claimed tickets closed", inline=False)
    if unclaimed:
        embed.add_field(name="👀 Heads up", inline=False,
                        value=f"{unclaimed} open ticket{'s are' if unclaimed != 1 else ' is'} waiting for someone to claim it.")
    embed.set_footer(text=f"{config.BOT_NAME} • Turn this off with /settings weekly_report:False")
    return embed


class Reports(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly.start()

    def cog_unload(self):
        self.weekly.cancel()

    @tasks.loop(hours=1)
    async def weekly(self):
        now = utcnow()
        rows = db.all("SELECT guild_id, log_channel_id, last_report_at FROM guilds "
                      "WHERE weekly_report=1 AND log_channel_id IS NOT NULL AND staff_role_id IS NOT NULL")
        for row in rows:
            if row["last_report_at"] is None:
                # First week starts now, so new servers don't get an empty report right away
                db.run("UPDATE guilds SET last_report_at=? WHERE guild_id=?", (now.isoformat(), row["guild_id"]))
                continue
            if now - dt.datetime.fromisoformat(row["last_report_at"]) < WEEK:
                continue
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["log_channel_id"]) if guild else None
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(embed=build_report(guild, now))
            except discord.HTTPException as e:
                log.warning("Weekly report failed in %s: %s", guild.id, e)
            db.run("UPDATE guilds SET last_report_at=? WHERE guild_id=?", (now.isoformat(), row["guild_id"]))

    @weekly.before_loop
    async def before_weekly(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="report", description="See the last 7 days of ticket stats")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(public="Post it in this channel instead of just showing you")
    async def report_cmd(self, inter: discord.Interaction, public: bool = False):
        await reply(inter, embed=build_report(inter.guild), ephemeral=not public)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reports(bot))
