"""Core ticket actions: opening and closing."""

import asyncio
import io
import time

import discord

from . import config
from .database import db
from .transcripts import build_transcript
from .utils import log, rating_view, send_guild_log, slugify, ticket_controls, unix, utcnow

# Channel IDs of open tickets, so activity tracking doesn't hit the database for every message
open_channels: set[int] = set()

# Anti-spam: seconds a member must wait between opening tickets in the same server
TICKET_COOLDOWN = 60
_last_opened: dict[tuple[int, int], float] = {}


def cooldown_left(guild_id: int, user_id: int) -> int:
    left = TICKET_COOLDOWN - (time.monotonic() - _last_opened.get((guild_id, user_id), -TICKET_COOLDOWN))
    return int(left) + 1 if left > 0 else 0


def start_cooldown(guild_id: int, user_id: int):
    _last_opened[(guild_id, user_id)] = time.monotonic()


async def set_claim_button(channel: discord.TextChannel, ticket, claimed_name: str | None):
    """Update the Claim button on the ticket's welcome message."""
    if not ticket["welcome_message_id"]:
        return
    try:
        await channel.get_partial_message(ticket["welcome_message_id"]).edit(view=ticket_controls(claimed_name))
    except discord.HTTPException:
        pass


def load_open_channels():
    for row in db.all("SELECT channel_id FROM tickets WHERE status IN ('open','closing')"):
        open_channels.add(row["channel_id"])
    db.run("UPDATE tickets SET status='open' WHERE status='closing'")  # recover from a crash mid-close


def mark_channel_gone(channel_id: int):
    if channel_id in open_channels:
        open_channels.discard(channel_id)
    db.run("UPDATE tickets SET status='closed', closed_at=?, close_reason='Channel deleted manually' "
           "WHERE channel_id=? AND status='open'", (utcnow().isoformat(), channel_id))


async def create_ticket(inter: discord.Interaction, cat, subject: str, details: str) -> discord.TextChannel:
    guild, user = inter.guild, inter.user
    cfg = db.config(guild.id)
    number = db.next_ticket_number(guild.id)

    category = guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
    staff = guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
    perms = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(**perms),
        guild.me: discord.PermissionOverwrite(**perms, manage_channels=True, manage_messages=True),
    }
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(**perms, manage_messages=True)

    channel = await guild.create_text_channel(
        f"{slugify(cat['name'])}-{number:04d}",
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        topic=f"Ticket #{number:04d} • {user} • {cat['name']}",
        reason=f"Ticket opened by {user}",
    )

    now = utcnow().isoformat()
    db.run("INSERT INTO tickets(guild_id, number, channel_id, user_id, category, subject, opened_at, last_activity) "
           "VALUES(?,?,?,?,?,?,?,?)", (guild.id, number, channel.id, user.id, cat["name"], subject, now, now))
    open_channels.add(channel.id)

    e = discord.Embed(
        title=f"{cat['emoji'] or '🎫'} {cat['name']} · Ticket #{number:04d}",
        description=f"Thanks for reaching out, {user.mention}. A staff member will be with you soon.\n"
                    f"While you wait, add any screenshots or extra info that might help.",
        color=config.COLOR, timestamp=utcnow())
    e.add_field(name="Subject", value=subject[:1024], inline=False)
    if details:
        e.add_field(name="Details", value=details[:1024], inline=False)
    e.set_footer(text=config.BOT_NAME)
    welcome = await channel.send(content=user.mention + (f" {staff.mention}" if staff else ""), embed=e,
                                 view=ticket_controls(),
                                 allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    db.run("UPDATE tickets SET welcome_message_id=? WHERE channel_id=?", (welcome.id, channel.id))
    start_cooldown(guild.id, user.id)
    try:
        await welcome.pin(reason="Ticket info")
    except discord.HTTPException:
        pass

    le = discord.Embed(title=f"📥 Ticket #{number:04d} opened", color=config.GREEN, timestamp=utcnow())
    le.add_field(name="User", value=user.mention)
    le.add_field(name="Type", value=cat["name"])
    le.add_field(name="Channel", value=channel.mention)
    le.add_field(name="Subject", value=subject[:1024], inline=False)
    await send_guild_log(guild, le)
    return channel


async def close_ticket(bot: discord.Client, guild: discord.Guild, channel: discord.TextChannel, ticket,
                       closer: discord.abc.User | None, reason: str) -> bool:
    # Lock so it can't be closed twice at the same time
    if db.run("UPDATE tickets SET status='closing' WHERE id=? AND status='open'", (ticket["id"],)).rowcount == 0:
        return False
    open_channels.discard(channel.id)
    num = f"{ticket['number']:04d}"

    try:
        await channel.send(embed=discord.Embed(
            description=f"🔒 **Ticket closed** by {closer.mention if closer else 'auto-close'}\n"
                        f"**Reason:** {reason}\n\nSaving the transcript and deleting this channel in 5 seconds...",
            color=config.RED))
    except discord.HTTPException:
        pass

    data, count = None, 0
    try:
        data, count = await build_transcript(channel, ticket, guild)
    except discord.HTTPException as e:
        log.warning("Transcript failed for ticket %s in %s: %s", num, guild.id, e)

    db.run("UPDATE tickets SET status='closed', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
           (utcnow().isoformat(), closer.id if closer else None, reason, ticket["id"]))

    le = discord.Embed(title=f"🔒 Ticket #{num} closed", color=config.RED, timestamp=utcnow())
    le.add_field(name="Opened by", value=f"<@{ticket['user_id']}>")
    le.add_field(name="Type", value=ticket["category"] or "—")
    le.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Nobody")
    le.add_field(name="Closed by", value=closer.mention if closer else "Auto-close")
    le.add_field(name="Opened", value=f"<t:{unix(ticket['opened_at'])}:R>")
    le.add_field(name="Messages", value=str(count))
    le.add_field(name="Reason", value=reason[:1024], inline=False)
    await send_guild_log(guild, le, data, f"ticket-{num}.html")

    cfg = db.config(guild.id)
    user = guild.get_member(ticket["user_id"])
    if user is None:
        try:
            user = await bot.fetch_user(ticket["user_id"])
        except discord.HTTPException:
            user = None
    if user:
        dm = discord.Embed(title=f"Your ticket in {guild.name} was closed",
                           description=f"**Ticket:** #{num} ({ticket['category']})\n**Reason:** {reason}",
                           color=config.COLOR)
        kwargs = {"embed": dm}
        if data and cfg["dm_transcripts"]:
            dm.description += "\n\nYour transcript is attached."
            kwargs["file"] = discord.File(io.BytesIO(data), filename=f"ticket-{num}.html")
        if cfg["ask_rating"]:
            dm.add_field(name="How did we do?", value="Rate the help you got below.", inline=False)
            kwargs["view"] = rating_view(ticket["id"])
        dm.set_footer(text=config.BOT_NAME)
        try:
            await user.send(**kwargs)
        except discord.HTTPException:
            pass  # DMs closed

    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket #{num} closed")
    except discord.HTTPException:
        pass
    return True
