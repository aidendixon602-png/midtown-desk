"""Shared helpers: embeds, components, permission checks, logging."""

import datetime as dt
import io
import logging
import re
import secrets
import traceback

import discord

from . import config
from .database import db

log = logging.getLogger("midtown")

# Permissions the bot asks for in its invite link (no Administrator needed)
REQUIRED_PERMS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, attach_files=True,
    read_message_history=True, manage_channels=True, manage_roles=True, manage_messages=True,
    use_external_emojis=True,
)


# ---- small helpers ----------------------------------------------------------------------

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def unix(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp())


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "ticket"


def strip_priority(channel_name: str) -> str:
    """Remove a priority emoji prefix from a channel name."""
    for _, prefix, _ in config.PRIORITIES.values():
        if prefix and channel_name.startswith(prefix):
            return channel_name[len(prefix):].lstrip("-")
    return channel_name


def with_priority(base_name: str, priority: str) -> str:
    prefix = config.PRIORITIES.get(priority, config.PRIORITIES["normal"])[1]
    return f"{prefix}-{base_name}"[:100] if prefix else base_name


def valid_emoji(text: str) -> bool:
    if re.fullmatch(r"<a?:\w+:\d+>", text):
        return True
    return len(text) <= 10 and not any(ch.isascii() and ch.isalnum() for ch in text)


def is_dev(user: discord.abc.User) -> bool:
    return user.id in config.DEV_IDS


def is_staff(member: discord.Member, cfg) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    return bool(cfg["staff_role_id"]) and any(r.id == cfg["staff_role_id"] for r in member.roles)


def human_delta(delta: dt.timedelta) -> str:
    secs = int(delta.total_seconds())
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, _ = divmod(secs, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m"]
    return " ".join(p for p in parts if p)


def invite_url(bot: discord.Client) -> str:
    return discord.utils.oauth_url(bot.user.id, permissions=REQUIRED_PERMS,
                                   scopes=("bot", "applications.commands"))


def missing_perms(guild: discord.Guild) -> list[str]:
    have = guild.me.guild_permissions
    if have.administrator:
        return []
    return [name.replace("_", " ").title() for name, wanted in REQUIRED_PERMS if wanted and not getattr(have, name)]


def embed(description: str | None = None, *, title: str | None = None, color: int = config.COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def reply(inter: discord.Interaction, content: str | None = None, *, embed: discord.Embed | None = None,
                ephemeral: bool = True, **kwargs):
    """Respond whether or not the interaction was already deferred."""
    if embed is not None:
        kwargs["embed"] = embed
    if inter.response.is_done():
        return await inter.followup.send(content, ephemeral=ephemeral, **kwargs)
    return await inter.response.send_message(content, ephemeral=ephemeral, **kwargs)


# ---- components (handled by custom_id so they survive restarts) ---------------------------

def static_view(*items) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    view.stop()  # the Tickets cog handles clicks by custom_id
    return view


def link_buttons(bot: discord.Client) -> discord.ui.View:
    items = [discord.ui.Button(label="Invite", url=invite_url(bot), emoji="➕")]
    if config.SUPPORT_URL:
        items.append(discord.ui.Button(label="Support server", url=config.SUPPORT_URL, emoji="🛟"))
    return static_view(*items)


def panel_embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=config.COLOR)
    e.set_footer(text=f"{config.BOT_NAME} • Pick a ticket type below")
    return e


def panel_view(guild_id: int) -> discord.ui.View:
    cats = db.categories(guild_id)
    if not cats:
        return static_view()
    options = [
        discord.SelectOption(label=c["name"][:100], value=str(c["id"]),
                             description=(c["description"] or "")[:100] or None, emoji=c["emoji"] or None)
        for c in cats[:25]
    ]
    return static_view(discord.ui.Select(custom_id="md:open", placeholder="🎫 Choose a ticket type...", options=options))


def ticket_controls(claimed_name: str | None = None) -> discord.ui.View:
    claim = discord.ui.Button(
        label=f"Claimed by {claimed_name}"[:80] if claimed_name else "Claim",
        style=discord.ButtonStyle.secondary if claimed_name else discord.ButtonStyle.success,
        emoji="🙋", custom_id="md:claim", disabled=bool(claimed_name))
    close = discord.ui.Button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="md:close")
    return static_view(claim, close)


def rating_view(ticket_id: int) -> discord.ui.View:
    return static_view(*[
        discord.ui.Button(label=str(n), emoji="⭐", style=discord.ButtonStyle.secondary,
                          custom_id=f"md:rate:{ticket_id}:{n}")
        for n in range(1, 6)
    ])


async def refresh_panels(guild: discord.Guild):
    for p in db.all("SELECT * FROM panels WHERE guild_id=?", (guild.id,)):
        channel = guild.get_channel(p["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            db.run("DELETE FROM panels WHERE message_id=?", (p["message_id"],))
            continue
        try:
            await channel.get_partial_message(p["message_id"]).edit(view=panel_view(guild.id))
        except discord.NotFound:
            db.run("DELETE FROM panels WHERE message_id=?", (p["message_id"],))
        except discord.HTTPException:
            pass


# ---- logging ----------------------------------------------------------------------------

async def send_guild_log(guild: discord.Guild, e: discord.Embed, data: bytes | None = None,
                         filename: str = "transcript.html"):
    """Send to a server's own ticket log channel."""
    cfg = db.config(guild.id)
    channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
    if not isinstance(channel, discord.TextChannel):
        return
    kwargs = {"embed": e}
    if data is not None:
        kwargs["file"] = discord.File(io.BytesIO(data), filename=filename)
    try:
        await channel.send(**kwargs)
    except discord.HTTPException as err:
        log.warning("Couldn't send guild log in %s: %s", guild.id, err)


async def send_bot_log(bot: discord.Client, e: discord.Embed):
    """Send to the developer's bot log channel (joins, leaves, errors)."""
    if not config.BOT_LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(config.BOT_LOG_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.send(embed=e)
    except discord.HTTPException as err:
        log.warning("Couldn't send bot log: %s", err)


async def report_error(bot: discord.Client, error: BaseException, where: str) -> str:
    """Log an error with a short code the user can give you, and post it to the bot log channel."""
    code = secrets.token_hex(3)
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log.error("Error %s in %s\n%s", code, where, tb)
    e = discord.Embed(title=f"⚠️ Error `{code}`", color=config.RED, timestamp=utcnow(),
                      description=f"**Where:** {where[:300]}\n```py\n{tb[-3500:]}\n```")
    await send_bot_log(bot, e)
    return code


# ---- global gate: blacklist + maintenance mode ------------------------------------------

async def gatekeep(inter: discord.Interaction) -> bool:
    """Returns False (and tells the user why) if they shouldn't use the bot right now."""
    if is_dev(inter.user):
        return True
    reason = None
    if db.blacklisted("user", inter.user.id):
        reason = f"⛔ You're blocked from using {config.BOT_NAME}."
    elif inter.guild_id and db.blacklisted("guild", inter.guild_id):
        reason = f"⛔ {config.BOT_NAME} is disabled in this server."
    elif db.get_state("maintenance") == "1":
        reason = f"🛠️ {config.BOT_NAME} is under maintenance. Please try again soon."
    if reason is None:
        return True
    if inter.type != discord.InteractionType.autocomplete and not inter.response.is_done():
        try:
            await inter.response.send_message(reason, ephemeral=True)
        except discord.HTTPException:
            pass
    return False
