"""
Midtown Desk - a ticket bot for Discord.

Features
- Ticket panel with a dropdown of ticket types (defaults included, fully customizable with /category)
- Pop-up form when opening a ticket so users explain their issue up front
- Private ticket channels (only the user + staff can see them)
- Claim button for staff, close button with a reason
- HTML transcripts sent to the log channel and the user's DMs
- Star ratings after a ticket closes
- One-open-ticket limit (configurable)
- Auto-close for inactive tickets with a warning first
- Logs for every open / claim / close / rating
- /stats with staff leaderboard
- AI (Claude + ChatGPT, with automatic fallback): instant first replies, close summaries,
  staff "Suggest reply" button, /ask command, and server info the AI can use
"""

import asyncio
import datetime as dt
import html
import io
import logging
import os
import re
import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
import ai  # noqa: E402  (must load after .env so it can see the API keys)

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: makes slash commands show up instantly in one server
DB_PATH = os.getenv("DB_PATH", "midtown_desk.db")

BRAND = "Midtown Desk"
COLOR = 0x4F7CFF
GREEN = 0x3BA55C
RED = 0xED4245
YELLOW = 0xFAA61A

DEFAULT_CATEGORIES = [
    ("General Support", "🎫", "Questions or help with anything"),
    ("Report a Player", "🚨", "Report someone breaking the rules"),
    ("Appeal", "⚖️", "Appeal a ban, mute or warning"),
    ("Partnership", "🤝", "Partner or collab with the server"),
    ("Other", "❓", "Anything that doesn't fit above"),
]

log = logging.getLogger("midtown-desk")

# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id        INTEGER PRIMARY KEY,
    staff_role_id   INTEGER,
    category_id     INTEGER,
    log_channel_id  INTEGER,
    ticket_limit    INTEGER DEFAULT 1,
    autoclose_hours INTEGER DEFAULT 48,
    dm_transcripts  INTEGER DEFAULT 1,
    ask_rating      INTEGER DEFAULT 1,
    counter         INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    name        TEXT,
    emoji       TEXT,
    description TEXT,
    position    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS panels (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER,
    channel_id  INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER,
    number        INTEGER,
    channel_id    INTEGER,
    user_id       INTEGER,
    category      TEXT,
    subject       TEXT,
    claimed_by    INTEGER,
    status        TEXT DEFAULT 'open',
    opened_at     TEXT,
    closed_at     TEXT,
    closed_by     INTEGER,
    close_reason  TEXT,
    last_activity TEXT,
    warned        INTEGER DEFAULT 0,
    rating        INTEGER
);
"""

# Columns added after the first version. Existing databases get them automatically.
GUILD_MIGRATIONS = {
    "ai_provider": "TEXT DEFAULT 'claude'",
    "ai_first_reply": "INTEGER DEFAULT 1",
    "ai_summaries": "INTEGER DEFAULT 1",
    "ai_suggest": "INTEGER DEFAULT 1",
    "ai_ask": "INTEGER DEFAULT 1",
    "ai_cooldown": "INTEGER DEFAULT 30",
    "ai_info": "TEXT",
}


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(guilds)")}
        for col, decl in GUILD_MIGRATIONS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE guilds ADD COLUMN {col} {decl}")
        self.conn.commit()

    def all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur


db = DB(DB_PATH)
open_channels: set[int] = set()  # channel ids of open tickets, for fast activity tracking


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def unix(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).timestamp())


def get_config(guild_id: int) -> sqlite3.Row:
    row = db.one("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
    if row is None:
        db.run("INSERT INTO guilds(guild_id) VALUES(?)", (guild_id,))
        row = db.one("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
    return row


def get_categories(guild_id: int):
    return db.all("SELECT * FROM categories WHERE guild_id=? ORDER BY position, id", (guild_id,))


def seed_categories(guild_id: int):
    for i, (name, emoji, desc) in enumerate(DEFAULT_CATEGORIES):
        db.run(
            "INSERT INTO categories(guild_id, name, emoji, description, position) VALUES(?,?,?,?,?)",
            (guild_id, name, emoji, desc, i),
        )


def get_ticket(channel_id: int):
    return db.one("SELECT * FROM tickets WHERE channel_id=? AND status='open'", (channel_id,))


def is_staff(member: discord.Member, cfg) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    return bool(cfg["staff_role_id"]) and any(r.id == cfg["staff_role_id"] for r in member.roles)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "ticket"


def valid_emoji(text: str) -> bool:
    if re.fullmatch(r"<a?:\w+:\d+>", text):
        return True
    return len(text) <= 10 and not any(ch.isascii() and ch.isalnum() for ch in text)


# --------------------------------------------------------------------------------------
# Components (all buttons/menus are handled by custom_id, so they keep working after restarts)
# --------------------------------------------------------------------------------------

def static_view(*items) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for item in items:
        view.add_item(item)
    view.stop()  # handled by the global interaction listener, not stored per-message
    return view


def panel_embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=COLOR)
    e.set_footer(text=f"{BRAND} • Pick a ticket type below")
    return e


def panel_view(guild_id: int) -> discord.ui.View:
    cats = get_categories(guild_id)
    if not cats:
        return static_view()
    options = [
        discord.SelectOption(
            label=c["name"][:100],
            value=str(c["id"]),
            description=(c["description"] or "")[:100] or None,
            emoji=c["emoji"] or None,
        )
        for c in cats[:25]
    ]
    return static_view(
        discord.ui.Select(custom_id="md:open", placeholder="🎫 Choose a ticket type...", options=options)
    )


def ticket_controls(claimed_name: Optional[str] = None) -> discord.ui.View:
    claim = discord.ui.Button(
        label=f"Claimed by {claimed_name}"[:80] if claimed_name else "Claim",
        style=discord.ButtonStyle.secondary if claimed_name else discord.ButtonStyle.success,
        emoji="🙋",
        custom_id="md:claim",
        disabled=bool(claimed_name),
    )
    close = discord.ui.Button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="md:close")
    items = [claim, close]
    if ai.available():
        items.append(discord.ui.Button(label="Suggest reply", style=discord.ButtonStyle.primary, emoji="💡",
                                       custom_id="md:suggest"))
    return static_view(*items)


# --------------------------------------------------------------------------------------
# AI helpers
# --------------------------------------------------------------------------------------

def ai_system(guild: discord.Guild, cfg, role: str) -> str:
    return ai.system_prompt(guild.name, cfg["ai_info"], role)


async def conversation_text(channel: discord.TextChannel, limit: int = 60, max_chars: int = 12000) -> str:
    """Recent ticket messages as plain text for the AI (newest kept if it's too long)."""
    lines = []
    async for m in channel.history(limit=limit):
        text = m.clean_content
        for e in m.embeds:
            text += " " + " ".join(filter(None, [e.title, e.description] + [f"{f.name}: {f.value}" for f in e.fields]))
        if m.attachments:
            text += " [attachments: " + ", ".join(a.filename for a in m.attachments) + "]"
        who = "Midtown Desk" if m.author.id == channel.guild.me.id else m.author.display_name
        lines.append(f"{who}: {text.strip()}")
    out = "\n".join(reversed(lines))
    return out[-max_chars:]


def ai_footer(provider: str) -> str:
    return f"🤖 AI answer by {ai.PROVIDERS[provider]} • A staff member will still review your ticket"


async def send_ai_first_reply(channel: discord.TextChannel, cfg, cat, subject: str, details: str):
    try:
        async with channel.typing():
            text, provider = await ai.ask(
                ai.first_reply_prompt(cat["name"], subject, details),
                ai_system(channel.guild, cfg, "You are writing the first reply in a new support ticket."),
                preferred=cfg["ai_provider"],
            )
        e = discord.Embed(title="💬 Quick answer while you wait", description=text[:4000], color=COLOR)
        e.set_footer(text=ai_footer(provider))
        await channel.send(embed=e)
    except ai.AIError as e:
        log.warning("AI first reply skipped: %s", e)
    except discord.HTTPException:
        pass


def rating_view(ticket_id: int) -> discord.ui.View:
    return static_view(*[
        discord.ui.Button(label=str(n), emoji="⭐", style=discord.ButtonStyle.secondary, custom_id=f"md:rate:{ticket_id}:{n}")
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


async def send_log(guild: discord.Guild, embed: discord.Embed, data: Optional[bytes] = None, filename: str = "transcript.html"):
    cfg = get_config(guild.id)
    channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
    if not isinstance(channel, discord.TextChannel):
        return
    kwargs = {"embed": embed}
    if data is not None:
        kwargs["file"] = discord.File(io.BytesIO(data), filename=filename)
    try:
        await channel.send(**kwargs)
    except discord.HTTPException as e:
        log.warning("Couldn't send log message: %s", e)


# --------------------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------------------

TRANSCRIPT_CSS = """
body{margin:0;background:#313338;color:#dbdee1;font-family:'gg sans','Segoe UI',Helvetica,Arial,sans-serif;font-size:15px}
.top{background:#2b2d31;padding:20px 24px;border-bottom:1px solid #1e1f22}
.top h1{margin:0 0 8px;font-size:20px;color:#f2f3f5}
.meta{display:flex;flex-wrap:wrap;gap:6px 22px;color:#b5bac1;font-size:13px}
.meta b{color:#f2f3f5}
.log{padding:12px 0}
.msg{display:flex;gap:14px;padding:6px 24px}
.msg:hover{background:#2e3035}
.av{width:40px;height:40px;border-radius:50%;flex-shrink:0}
.name{font-weight:600;color:#f2f3f5}
.tag{background:#5865f2;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;margin-left:6px;vertical-align:middle}
.ts{color:#949ba4;font-size:12px;margin-left:8px}
.ct{line-height:1.4;word-wrap:break-word}
.embed{border-left:4px solid #4f7cff;background:#2b2d31;border-radius:4px;padding:8px 12px;margin-top:6px;max-width:520px}
.et{font-weight:600;color:#f2f3f5;margin-bottom:4px}
.f{margin-top:6px}
img.att{max-width:400px;max-height:300px;border-radius:6px;margin-top:6px;display:block}
a{color:#00a8fc}
.foot{color:#949ba4;font-size:12px;padding:16px 24px;border-top:1px solid #1e1f22}
"""


def _br(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


async def build_transcript(channel: discord.TextChannel, ticket, guild: discord.Guild) -> tuple[bytes, int]:
    rows = []
    count = 0
    async for m in channel.history(limit=None, oldest_first=True):
        count += 1
        extras = []
        for e in m.embeds:
            fields = "".join(f'<div class="f"><b>{html.escape(f.name)}</b><br>{_br(f.value)}</div>' for f in e.fields)
            extras.append(
                f'<div class="embed"><div class="et">{html.escape(e.title or "")}</div>'
                f'<div>{_br(e.description or "")}</div>{fields}</div>'
            )
        for a in m.attachments:
            url = html.escape(a.url)
            if (a.content_type or "").startswith("image/"):
                extras.append(f'<a href="{url}"><img class="att" src="{url}" alt=""></a>')
            else:
                extras.append(f'<div><a href="{url}">📎 {html.escape(a.filename)}</a></div>')
        rows.append(
            f'<div class="msg"><img class="av" src="{html.escape(m.author.display_avatar.url)}" alt="">'
            f'<div><div><span class="name">{html.escape(m.author.display_name)}</span>'
            f'{"<span class=tag>BOT</span>" if m.author.bot else ""}'
            f'<span class="ts">{m.created_at.strftime("%b %d, %Y %I:%M %p UTC")}</span></div>'
            f'<div class="ct">{_br(m.clean_content)}</div>{"".join(extras)}</div></div>'
        )

    opener = guild.get_member(ticket["user_id"])
    claimer = guild.get_member(ticket["claimed_by"]) if ticket["claimed_by"] else None
    num = f"{ticket['number']:04d}"
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket #{num} - {html.escape(guild.name)}</title><style>{TRANSCRIPT_CSS}</style></head>
<body>
<div class="top"><h1>🎫 Ticket #{num} · {html.escape(ticket['category'] or '')}</h1>
<div class="meta">
<span><b>Server:</b> {html.escape(guild.name)}</span>
<span><b>Opened by:</b> {html.escape(str(opener) if opener else str(ticket['user_id']))}</span>
<span><b>Claimed by:</b> {html.escape(str(claimer)) if claimer else 'Nobody'}</span>
<span><b>Subject:</b> {html.escape(ticket['subject'] or '')}</span>
<span><b>Messages:</b> {count}</span>
</div></div>
<div class="log">{''.join(rows)}</div>
<div class="foot">Transcript generated by {BRAND} on {utcnow().strftime('%b %d, %Y %I:%M %p UTC')}</div>
</body></html>"""
    return doc.encode("utf-8"), count


# --------------------------------------------------------------------------------------
# Core ticket actions
# --------------------------------------------------------------------------------------

async def create_ticket(inter: discord.Interaction, cat, subject: str, details: str) -> discord.TextChannel:
    guild = inter.guild
    user = inter.user
    cfg = get_config(guild.id)

    db.run("UPDATE guilds SET counter=counter+1 WHERE guild_id=?", (guild.id,))
    number = get_config(guild.id)["counter"]

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
    db.run(
        "INSERT INTO tickets(guild_id, number, channel_id, user_id, category, subject, opened_at, last_activity) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (guild.id, number, channel.id, user.id, cat["name"], subject, now, now),
    )
    open_channels.add(channel.id)

    embed = discord.Embed(
        title=f"{cat['emoji'] or '🎫'} {cat['name']} · Ticket #{number:04d}",
        description=f"Thanks for reaching out, {user.mention}. A staff member will be with you soon.\n"
                    f"While you wait, add any screenshots or extra info that might help.",
        color=COLOR,
        timestamp=utcnow(),
    )
    embed.add_field(name="Subject", value=subject[:1024], inline=False)
    if details:
        embed.add_field(name="Details", value=details[:1024], inline=False)
    embed.set_footer(text=BRAND)

    await channel.send(
        content=f"{user.mention}" + (f" {staff.mention}" if staff else ""),
        embed=embed,
        view=ticket_controls(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )

    le = discord.Embed(title=f"📥 Ticket #{number:04d} opened", color=GREEN, timestamp=utcnow())
    le.add_field(name="User", value=user.mention)
    le.add_field(name="Type", value=cat["name"])
    le.add_field(name="Channel", value=channel.mention)
    le.add_field(name="Subject", value=subject[:1024], inline=False)
    await send_log(guild, le)

    if cfg["ai_first_reply"] and ai.available():
        asyncio.create_task(send_ai_first_reply(channel, cfg, cat, subject, details))
    return channel


async def close_ticket(guild: discord.Guild, channel: discord.TextChannel, ticket,
                       closer: Optional[discord.abc.User], reason: str) -> bool:
    # Lock the ticket so it can't be closed twice at once
    if db.run("UPDATE tickets SET status='closing' WHERE id=? AND status='open'", (ticket["id"],)).rowcount == 0:
        return False
    open_channels.discard(channel.id)
    num = f"{ticket['number']:04d}"

    try:
        await channel.send(embed=discord.Embed(
            description=f"🔒 **Ticket closed** by {closer.mention if closer else 'auto-close'}\n"
                        f"**Reason:** {reason}\n\nSaving the transcript and deleting this channel in 5 seconds...",
            color=RED,
        ))
    except discord.HTTPException:
        pass

    data, count = None, 0
    try:
        data, count = await build_transcript(channel, ticket, guild)
    except discord.HTTPException as e:
        log.warning("Transcript failed for ticket %s: %s", num, e)

    cfg = get_config(guild.id)
    summary = None
    if cfg["ai_summaries"] and ai.available():
        try:
            summary, provider = await ai.ask(
                ai.summary_prompt(await conversation_text(channel)),
                ai_system(guild, cfg, "You are summarizing a closed ticket for staff."),
                preferred=cfg["ai_provider"], max_tokens=400)
            summary = f"{summary[:980]}\n-# by {ai.PROVIDERS[provider]}"
        except (ai.AIError, discord.HTTPException) as e:
            log.warning("AI summary skipped: %s", e)

    db.run(
        "UPDATE tickets SET status='closed', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
        (utcnow().isoformat(), closer.id if closer else None, reason, ticket["id"]),
    )

    le = discord.Embed(title=f"🔒 Ticket #{num} closed", color=RED, timestamp=utcnow())
    le.add_field(name="Opened by", value=f"<@{ticket['user_id']}>")
    le.add_field(name="Type", value=ticket["category"] or "—")
    le.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Nobody")
    le.add_field(name="Closed by", value=closer.mention if closer else "Auto-close")
    le.add_field(name="Opened", value=f"<t:{unix(ticket['opened_at'])}:R>")
    le.add_field(name="Messages", value=str(count))
    le.add_field(name="Reason", value=reason[:1024], inline=False)
    if summary:
        le.add_field(name="🤖 AI summary", value=summary[:1024], inline=False)
    await send_log(guild, le, data, f"ticket-{num}.html")

    user = guild.get_member(ticket["user_id"])
    if user is None:
        try:
            user = await bot.fetch_user(ticket["user_id"])
        except discord.HTTPException:
            user = None
    if user:
        dm = discord.Embed(
            title=f"Your ticket in {guild.name} was closed",
            description=f"**Ticket:** #{num} ({ticket['category']})\n**Reason:** {reason}",
            color=COLOR,
        )
        kwargs = {"embed": dm}
        if data and cfg["dm_transcripts"]:
            dm.description += "\n\nYour transcript is attached."
            kwargs["file"] = discord.File(io.BytesIO(data), filename=f"ticket-{num}.html")
        if cfg["ask_rating"]:
            dm.add_field(name="How did we do?", value="Rate the help you got below.", inline=False)
            kwargs["view"] = rating_view(ticket["id"])
        dm.set_footer(text=BRAND)
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


# --------------------------------------------------------------------------------------
# Modals
# --------------------------------------------------------------------------------------

class OpenTicketModal(discord.ui.Modal):
    def __init__(self, cat, panel_message: Optional[discord.Message]):
        super().__init__(title=f"{cat['name']} ticket"[:45])
        self.cat = cat
        self.panel_message = panel_message
        self.subject = discord.ui.TextInput(label="What do you need help with?", max_length=100,
                                            placeholder="Short summary of your issue")
        self.details = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False,
                                            max_length=1000, placeholder="Usernames, links, what happened, etc.")
        self.add_item(self.subject)
        self.add_item(self.details)

    async def on_submit(self, inter: discord.Interaction):
        cfg = get_config(inter.guild_id)
        rows = db.all("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
                      (inter.guild_id, inter.user.id))
        if len(rows) >= cfg["ticket_limit"]:
            links = ", ".join(f"<#{r['channel_id']}>" for r in rows)
            return await inter.response.send_message(f"You already have an open ticket: {links}", ephemeral=True)

        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await create_ticket(inter, self.cat, str(self.subject.value), str(self.details.value))
        except discord.Forbidden:
            return await inter.followup.send(
                "I couldn't create your ticket because I'm missing permissions. Please tell a staff member.", ephemeral=True)
        except discord.HTTPException as e:
            return await inter.followup.send(f"Couldn't create your ticket: {e.text or e}", ephemeral=True)

        await inter.followup.send(f"✅ Your ticket is open: {channel.mention}", ephemeral=True)
        if self.panel_message:  # reset the dropdown so the same option can be picked again
            try:
                await self.panel_message.edit(view=panel_view(inter.guild_id))
            except discord.HTTPException:
                pass


class CloseModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=300,
                                  placeholder="e.g. Issue resolved")

    async def on_submit(self, inter: discord.Interaction):
        ticket = get_ticket(inter.channel_id)
        if not ticket:
            return await inter.response.send_message("This ticket is already closed.", ephemeral=True)
        await inter.response.send_message("Closing ticket...", ephemeral=True)
        await close_ticket(inter.guild, inter.channel, ticket, inter.user, str(self.reason.value) or "No reason given")


# --------------------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------------------

class MidtownDesk(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for transcripts
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        for row in db.all("SELECT channel_id FROM tickets WHERE status IN ('open','closing')"):
            open_channels.add(row["channel_id"])
        db.run("UPDATE tickets SET status='open' WHERE status='closing'")  # recover from a crash mid-close

        self.tree.add_command(category_group)
        self.tree.add_command(ticket_group)
        self.tree.add_command(ai_group)
        log.info("AI providers available: %s", ", ".join(ai.PROVIDERS[p] for p in ai.available()) or "none")
        if GUILD_ID:
            guild = discord.Object(int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        autoclose_loop.start()


bot = MidtownDesk()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="for tickets 🎫"))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    if message.channel.id in open_channels:
        db.run("UPDATE tickets SET last_activity=?, warned=0 WHERE channel_id=? AND status='open'",
               (utcnow().isoformat(), message.channel.id))


@bot.event
async def on_guild_channel_delete(channel):
    if channel.id in open_channels:
        open_channels.discard(channel.id)
        db.run("UPDATE tickets SET status='closed', closed_at=?, close_reason='Channel deleted manually' "
               "WHERE channel_id=? AND status='open'", (utcnow().isoformat(), channel.id))


@bot.event
async def on_interaction(inter: discord.Interaction):
    if inter.type != discord.InteractionType.component:
        return
    cid = (inter.data or {}).get("custom_id", "")
    if not cid.startswith("md:"):
        return
    try:
        if cid == "md:open":
            await handle_open(inter)
        elif cid == "md:claim":
            await handle_claim(inter)
        elif cid == "md:close":
            await handle_close(inter)
        elif cid == "md:suggest":
            await handle_suggest(inter)
        elif cid.startswith("md:rate:"):
            await handle_rate(inter, cid)
    except Exception:
        log.exception("Error handling %s", cid)
        try:
            if not inter.response.is_done():
                await inter.response.send_message("Something went wrong. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


async def handle_open(inter: discord.Interaction):
    if inter.guild is None:
        return
    cfg = get_config(inter.guild.id)
    if not cfg["staff_role_id"]:
        return await inter.response.send_message("Tickets aren't set up yet. An admin needs to run `/setup`.", ephemeral=True)
    values = inter.data.get("values") or []
    if not values:
        return
    cat = db.one("SELECT * FROM categories WHERE id=? AND guild_id=?", (int(values[0]), inter.guild.id))
    if not cat:
        return await inter.response.send_message("That ticket type was removed. Please pick another.", ephemeral=True)
    rows = db.all("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
                  (inter.guild.id, inter.user.id))
    if len(rows) >= cfg["ticket_limit"]:
        links = ", ".join(f"<#{r['channel_id']}>" for r in rows)
        return await inter.response.send_message(
            f"You already have an open ticket: {links}\nClose it before opening another.", ephemeral=True)
    await inter.response.send_modal(OpenTicketModal(cat, inter.message))


async def handle_claim(inter: discord.Interaction):
    ticket = get_ticket(inter.channel_id)
    if not ticket:
        return await inter.response.send_message("This isn't an open ticket.", ephemeral=True)
    if not is_staff(inter.user, get_config(inter.guild_id)):
        return await inter.response.send_message("Only staff can claim tickets.", ephemeral=True)
    if ticket["claimed_by"]:
        return await inter.response.send_message(f"Already claimed by <@{ticket['claimed_by']}>.", ephemeral=True)

    db.run("UPDATE tickets SET claimed_by=? WHERE id=?", (inter.user.id, ticket["id"]))
    await inter.response.edit_message(view=ticket_controls(inter.user.display_name))
    await inter.channel.send(embed=discord.Embed(
        description=f"🙋 {inter.user.mention} claimed this ticket and will be helping you.", color=GREEN))
    le = discord.Embed(title=f"🙋 Ticket #{ticket['number']:04d} claimed", color=YELLOW, timestamp=utcnow())
    le.add_field(name="Staff", value=inter.user.mention)
    le.add_field(name="Channel", value=inter.channel.mention)
    await send_log(inter.guild, le)


async def handle_close(inter: discord.Interaction):
    ticket = get_ticket(inter.channel_id)
    if not ticket:
        return await inter.response.send_message("This ticket is already closed.", ephemeral=True)
    if not (is_staff(inter.user, get_config(inter.guild_id)) or inter.user.id == ticket["user_id"]):
        return await inter.response.send_message("Only staff or the ticket owner can close this.", ephemeral=True)
    await inter.response.send_modal(CloseModal())


async def handle_suggest(inter: discord.Interaction):
    ticket = get_ticket(inter.channel_id)
    if not ticket:
        return await inter.response.send_message("This isn't an open ticket.", ephemeral=True)
    cfg = get_config(inter.guild_id)
    if not is_staff(inter.user, cfg):
        return await inter.response.send_message("Only staff can use reply suggestions.", ephemeral=True)
    if not cfg["ai_suggest"]:
        return await inter.response.send_message("Reply suggestions are turned off. An admin can enable them with `/ai settings`.", ephemeral=True)
    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        text, provider = await ai.ask(
            ai.suggest_prompt(await conversation_text(inter.channel)),
            ai_system(inter.guild, cfg, "You are drafting a reply for a staff member to send."),
            preferred=cfg["ai_provider"])
    except ai.AIError as e:
        return await inter.followup.send(str(e), ephemeral=True)
    await inter.followup.send(
        f"💡 **Suggested reply** ({ai.PROVIDERS[provider]}) — copy, edit and send it yourself:\n\n{text[:1800]}",
        ephemeral=True)


async def handle_rate(inter: discord.Interaction, cid: str):
    _, _, tid, n = cid.split(":")
    ticket = db.one("SELECT * FROM tickets WHERE id=?", (int(tid),))
    if not ticket or ticket["user_id"] != inter.user.id:
        return await inter.response.send_message("This rating isn't yours to give.", ephemeral=True)
    if ticket["rating"]:
        return await inter.response.edit_message(view=None)
    stars = int(n)
    db.run("UPDATE tickets SET rating=? WHERE id=?", (stars, ticket["id"]))
    await inter.response.edit_message(content=f"Thanks for your feedback! You rated us {'⭐' * stars} ({stars}/5).", view=None)
    guild = bot.get_guild(ticket["guild_id"])
    if guild:
        le = discord.Embed(title=f"⭐ Ticket #{ticket['number']:04d} rated {stars}/5", color=YELLOW, timestamp=utcnow())
        le.add_field(name="User", value=f"<@{ticket['user_id']}>")
        le.add_field(name="Staff", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Nobody claimed it")
        await send_log(guild, le)


# --------------------------------------------------------------------------------------
# Auto-close
# --------------------------------------------------------------------------------------

@tasks.loop(minutes=10)
async def autoclose_loop():
    rows = db.all(
        "SELECT t.*, g.autoclose_hours FROM tickets t JOIN guilds g ON g.guild_id=t.guild_id "
        "WHERE t.status='open' AND g.autoclose_hours > 0"
    )
    for t in rows:
        guild = bot.get_guild(t["guild_id"])
        if guild is None:
            continue
        channel = guild.get_channel(t["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await on_guild_channel_delete(discord.Object(t["channel_id"]))
            continue
        idle = utcnow() - dt.datetime.fromisoformat(t["last_activity"])
        limit = dt.timedelta(hours=t["autoclose_hours"])
        try:
            if not t["warned"] and idle >= limit:
                await channel.send(
                    content=f"<@{t['user_id']}>",
                    embed=discord.Embed(
                        description="⏰ This ticket has been quiet for a while. It will close automatically in "
                                    "**1 hour** unless someone sends a message.",
                        color=YELLOW))
                db.run("UPDATE tickets SET warned=1 WHERE id=?", (t["id"],))
            elif t["warned"] and idle >= limit + dt.timedelta(hours=1):
                await close_ticket(guild, channel, t, None, "Closed automatically for inactivity")
        except discord.HTTPException as e:
            log.warning("Auto-close error on ticket %s: %s", t["id"], e)


@autoclose_loop.before_loop
async def before_autoclose():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------------------
# Slash commands
# --------------------------------------------------------------------------------------

def needs_setup(guild_id: int) -> bool:
    return not get_config(guild_id)["staff_role_id"]


@bot.tree.command(name="setup", description="Set up Midtown Desk for this server")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    staff_role="Role that can see and handle tickets",
    category="Category for ticket channels (leave empty and I'll create one)",
    log_channel="Channel for logs + transcripts (leave empty and I'll create one)",
)
async def setup_cmd(inter: discord.Interaction, staff_role: discord.Role,
                    category: Optional[discord.CategoryChannel] = None,
                    log_channel: Optional[discord.TextChannel] = None):
    await inter.response.defer(ephemeral=True, thinking=True)
    guild = inter.guild
    get_config(guild.id)
    hidden = discord.PermissionOverwrite(view_channel=False)

    if category is None:
        category = await guild.create_category("Tickets", overwrites={
            guild.default_role: hidden,
            staff_role: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
        })
    if log_channel is None:
        log_channel = await guild.create_text_channel("ticket-logs", overwrites={
            guild.default_role: hidden,
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
        })

    db.run("UPDATE guilds SET staff_role_id=?, category_id=?, log_channel_id=? WHERE guild_id=?",
           (staff_role.id, category.id, log_channel.id, guild.id))
    if not get_categories(guild.id):
        seed_categories(guild.id)

    e = discord.Embed(title=f"✅ {BRAND} is set up", color=GREEN)
    e.add_field(name="Staff role", value=staff_role.mention)
    e.add_field(name="Ticket category", value=category.name)
    e.add_field(name="Log channel", value=log_channel.mention)
    e.add_field(name="Next step",
                value="Run **/panel** in the channel where members should open tickets.\n"
                      "Customize ticket types with **/category add**, **/category remove** and **/category list**.\n"
                      "Tweak limits and auto-close with **/settings**.\n"
                      "Set up the AI with **/ai settings** and teach it about your server with **/ai info**.",
                inline=False)
    await inter.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="panel", description="Post the ticket panel members use to open tickets")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="Where to post it (defaults to this channel)", title="Panel title",
                       description="Panel text (use \\n for a new line)")
async def panel_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                    title: app_commands.Range[str, 1, 256] = "Midtown Desk",
                    description: app_commands.Range[str, 1, 2000] =
                    "Need help? Pick a ticket type from the menu below and a staff member will be with you shortly."):
    if needs_setup(inter.guild_id):
        return await inter.response.send_message("Run `/setup` first.", ephemeral=True)
    channel = channel or inter.channel
    if not isinstance(channel, discord.TextChannel):
        return await inter.response.send_message("Pick a normal text channel.", ephemeral=True)
    msg = await channel.send(embed=panel_embed(title, description.replace("\\n", "\n")), view=panel_view(inter.guild_id))
    db.run("INSERT INTO panels(message_id, guild_id, channel_id) VALUES(?,?,?)", (msg.id, inter.guild_id, channel.id))
    await inter.response.send_message(f"✅ Panel posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="settings", description="View or change ticket settings")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    ticket_limit="How many tickets one person can have open at once",
    autoclose_hours="Close inactive tickets after this many hours (0 = off)",
    dm_transcripts="DM the user their transcript when a ticket closes",
    ask_rating="Ask users to rate the help after a ticket closes",
    staff_role="Change the staff role",
    log_channel="Change the log channel",
    category="Change the category tickets are created in",
)
async def settings_cmd(inter: discord.Interaction,
                       ticket_limit: Optional[app_commands.Range[int, 1, 10]] = None,
                       autoclose_hours: Optional[app_commands.Range[int, 0, 720]] = None,
                       dm_transcripts: Optional[bool] = None,
                       ask_rating: Optional[bool] = None,
                       staff_role: Optional[discord.Role] = None,
                       log_channel: Optional[discord.TextChannel] = None,
                       category: Optional[discord.CategoryChannel] = None):
    updates = {
        "ticket_limit": ticket_limit,
        "autoclose_hours": autoclose_hours,
        "dm_transcripts": None if dm_transcripts is None else int(dm_transcripts),
        "ask_rating": None if ask_rating is None else int(ask_rating),
        "staff_role_id": staff_role.id if staff_role else None,
        "log_channel_id": log_channel.id if log_channel else None,
        "category_id": category.id if category else None,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    get_config(inter.guild_id)
    if updates:
        db.run(f"UPDATE guilds SET {', '.join(f'{k}=?' for k in updates)} WHERE guild_id=?",
               (*updates.values(), inter.guild_id))

    cfg = get_config(inter.guild_id)
    e = discord.Embed(title="⚙️ Settings updated" if updates else "⚙️ Current settings", color=COLOR)
    e.add_field(name="Staff role", value=f"<@&{cfg['staff_role_id']}>" if cfg["staff_role_id"] else "Not set")
    e.add_field(name="Log channel", value=f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "Not set")
    e.add_field(name="Ticket category", value=f"<#{cfg['category_id']}>" if cfg["category_id"] else "Not set")
    e.add_field(name="Open tickets per user", value=str(cfg["ticket_limit"]))
    e.add_field(name="Auto-close", value=f"{cfg['autoclose_hours']}h" if cfg["autoclose_hours"] else "Off")
    e.add_field(name="DM transcripts", value="On" if cfg["dm_transcripts"] else "Off")
    e.add_field(name="Ratings", value="On" if cfg["ask_rating"] else "Off")
    await inter.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="stats", description="Ticket stats and staff leaderboard")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def stats_cmd(inter: discord.Interaction):
    gid = inter.guild_id
    total = db.one("SELECT COUNT(*) c FROM tickets WHERE guild_id=?", (gid,))["c"]
    open_ = db.one("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open'", (gid,))["c"]
    avg = db.one("SELECT AVG(rating) a, COUNT(rating) n FROM tickets WHERE guild_id=? AND rating IS NOT NULL", (gid,))
    top = db.all(
        "SELECT claimed_by, COUNT(*) c, AVG(rating) r FROM tickets WHERE guild_id=? AND claimed_by IS NOT NULL "
        "GROUP BY claimed_by ORDER BY c DESC LIMIT 10", (gid,))

    e = discord.Embed(title="📊 Ticket stats", color=COLOR)
    e.add_field(name="Total tickets", value=str(total))
    e.add_field(name="Open now", value=str(open_))
    e.add_field(name="Closed", value=str(total - open_))
    e.add_field(name="Average rating",
                value=f"{avg['a']:.2f} ⭐ ({avg['n']} ratings)" if avg["n"] else "No ratings yet", inline=False)
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{r['claimed_by']}> · {r['c']} tickets"
        + (f" · {r['r']:.1f} ⭐" if r["r"] else "")
        for i, r in enumerate(top)
    ]
    e.add_field(name="Staff leaderboard", value="\n".join(lines) or "No claimed tickets yet", inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)


# ---- AI: /ask and /ai ----

PROVIDER_CHOICES = [app_commands.Choice(name="Claude", value="claude"), app_commands.Choice(name="ChatGPT", value="gpt")]


@bot.tree.command(name="ask", description="Ask the AI a question")
@app_commands.guild_only()
@app_commands.describe(question="What do you want to know?", model="Which AI to use (defaults to the server's choice)")
@app_commands.choices(model=PROVIDER_CHOICES)
async def ask_cmd(inter: discord.Interaction, question: app_commands.Range[str, 1, 1000],
                  model: Optional[app_commands.Choice[str]] = None):
    cfg = get_config(inter.guild_id)
    staff = is_staff(inter.user, cfg)
    if not cfg["ai_ask"] and not staff:
        return await inter.response.send_message("`/ask` is turned off in this server.", ephemeral=True)
    if not staff:
        wait = ai.cooldown_left(inter.guild_id, inter.user.id, cfg["ai_cooldown"])
        if wait:
            return await inter.response.send_message(f"Slow down! Try again in {wait}s.", ephemeral=True)
    await inter.response.defer(thinking=True)
    try:
        text, provider = await ai.ask(
            question,
            ai_system(inter.guild, cfg, "A member is asking you a question. Answer in under 200 words."),
            preferred=model.value if model else cfg["ai_provider"])
    except ai.AIError as e:
        return await inter.followup.send(str(e))
    e = discord.Embed(description=text[:4000], color=COLOR)
    e.set_author(name=f"{inter.user.display_name} asked: {question}"[:256], icon_url=inter.user.display_avatar.url)
    footer = f"🤖 Answered by {ai.PROVIDERS[provider]}"
    if model and provider != model.value:
        footer += f" ({model.name} was unavailable)"
    e.set_footer(text=footer)
    await inter.followup.send(embed=e)


ai_group = app_commands.Group(name="ai", description="Configure the AI (Claude + ChatGPT)",
                              guild_only=True, default_permissions=discord.Permissions(manage_guild=True))


def ai_status_embed(cfg, title: str) -> discord.Embed:
    onoff = lambda v: "✅ On" if v else "❌ Off"  # noqa: E731
    keys = "\n".join(
        f"{'🟢' if p in ai.available() else '⚪'} **{name}** — "
        + (f"`{ai.model_name(p)}`" if p in ai.available() else "no API key")
        for p, name in ai.PROVIDERS.items())
    e = discord.Embed(title=title, color=COLOR)
    e.add_field(name="Providers", value=keys, inline=False)
    e.add_field(name="Preferred", value=ai.PROVIDERS.get(cfg["ai_provider"], "Claude"))
    e.add_field(name="First replies", value=onoff(cfg["ai_first_reply"]))
    e.add_field(name="Close summaries", value=onoff(cfg["ai_summaries"]))
    e.add_field(name="Suggest reply button", value=onoff(cfg["ai_suggest"]))
    e.add_field(name="/ask for members", value=onoff(cfg["ai_ask"]))
    e.add_field(name="/ask cooldown", value=f"{cfg['ai_cooldown']}s")
    e.add_field(name="Server info", value=f"{len(cfg['ai_info'] or '')} characters (edit with `/ai info`)", inline=False)
    if not ai.available():
        e.description = "⚠️ No API keys found. Add `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` to the bot's `.env` and restart."
    else:
        e.description = "If the preferred AI fails, the other one is used automatically."
    return e


@ai_group.command(name="settings", description="Turn AI features on/off and pick the preferred AI")
@app_commands.describe(
    preferred="Which AI to try first",
    first_replies="AI posts a quick answer when a ticket opens",
    summaries="AI summary added to the log when a ticket closes",
    suggest_button="Staff get a 'Suggest reply' button in tickets",
    ask="Let members use /ask (staff can always use it)",
    ask_cooldown="Seconds between /ask uses per member",
)
@app_commands.choices(preferred=PROVIDER_CHOICES)
async def ai_settings(inter: discord.Interaction,
                      preferred: Optional[app_commands.Choice[str]] = None,
                      first_replies: Optional[bool] = None,
                      summaries: Optional[bool] = None,
                      suggest_button: Optional[bool] = None,
                      ask: Optional[bool] = None,
                      ask_cooldown: Optional[app_commands.Range[int, 0, 3600]] = None):
    updates = {
        "ai_provider": preferred.value if preferred else None,
        "ai_first_reply": None if first_replies is None else int(first_replies),
        "ai_summaries": None if summaries is None else int(summaries),
        "ai_suggest": None if suggest_button is None else int(suggest_button),
        "ai_ask": None if ask is None else int(ask),
        "ai_cooldown": ask_cooldown,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    get_config(inter.guild_id)
    if updates:
        db.run(f"UPDATE guilds SET {', '.join(f'{k}=?' for k in updates)} WHERE guild_id=?",
               (*updates.values(), inter.guild_id))
    cfg = get_config(inter.guild_id)
    await inter.response.send_message(
        embed=ai_status_embed(cfg, "🤖 AI settings updated" if updates else "🤖 AI settings"), ephemeral=True)


class AIInfoModal(discord.ui.Modal, title="Server info for the AI"):
    def __init__(self, current: Optional[str]):
        super().__init__()
        self.info = discord.ui.TextInput(
            label="Rules, FAQs, links, how things work",
            style=discord.TextStyle.paragraph, required=False, max_length=4000, default=current or None,
            placeholder="e.g. Appeals take 48h. Staff applications open on the 1st. Our rules are in #rules...")
        self.add_item(self.info)

    async def on_submit(self, inter: discord.Interaction):
        get_config(inter.guild_id)
        db.run("UPDATE guilds SET ai_info=? WHERE guild_id=?", (str(self.info.value).strip() or None, inter.guild_id))
        await inter.response.send_message("✅ Saved. The AI will use this info in its answers.", ephemeral=True)


@ai_group.command(name="info", description="Teach the AI about your server (rules, FAQs, links)")
async def ai_info_cmd(inter: discord.Interaction):
    await inter.response.send_modal(AIInfoModal(get_config(inter.guild_id)["ai_info"]))


# ---- /category ----

category_group = app_commands.Group(name="category", description="Manage ticket types in the dropdown",
                                    guild_only=True, default_permissions=discord.Permissions(manage_guild=True))


async def category_autocomplete(inter: discord.Interaction, current: str):
    return [app_commands.Choice(name=c["name"], value=c["name"])
            for c in get_categories(inter.guild_id) if current.lower() in c["name"].lower()][:25]


@category_group.command(name="add", description="Add a ticket type to the dropdown")
@app_commands.describe(name="Name shown in the dropdown", emoji="Emoji for it (optional)",
                       description="Short line under the name (optional)")
async def category_add(inter: discord.Interaction, name: app_commands.Range[str, 1, 50],
                       emoji: Optional[str] = None, description: Optional[app_commands.Range[str, 1, 100]] = None):
    cats = get_categories(inter.guild_id)
    if len(cats) >= 25:
        return await inter.response.send_message("Discord dropdowns max out at 25 options.", ephemeral=True)
    if any(c["name"].lower() == name.lower() for c in cats):
        return await inter.response.send_message("A ticket type with that name already exists.", ephemeral=True)
    if emoji and not valid_emoji(emoji.strip()):
        return await inter.response.send_message("That doesn't look like an emoji. Try something like 🎮.", ephemeral=True)
    position = max((c["position"] for c in cats), default=-1) + 1
    db.run("INSERT INTO categories(guild_id, name, emoji, description, position) VALUES(?,?,?,?,?)",
           (inter.guild_id, name, emoji.strip() if emoji else None, description, position))
    await inter.response.send_message(f"✅ Added **{name}**. Panels updated.", ephemeral=True)
    await refresh_panels(inter.guild)


@category_group.command(name="remove", description="Remove a ticket type from the dropdown")
@app_commands.autocomplete(name=category_autocomplete)
async def category_remove(inter: discord.Interaction, name: str):
    cur = db.run("DELETE FROM categories WHERE guild_id=? AND lower(name)=lower(?)", (inter.guild_id, name))
    if cur.rowcount == 0:
        return await inter.response.send_message("No ticket type with that name.", ephemeral=True)
    await inter.response.send_message(f"🗑️ Removed **{name}**. Panels updated.", ephemeral=True)
    await refresh_panels(inter.guild)


@category_group.command(name="edit", description="Rename a ticket type or change its emoji/description")
@app_commands.autocomplete(name=category_autocomplete)
async def category_edit(inter: discord.Interaction, name: str,
                        new_name: Optional[app_commands.Range[str, 1, 50]] = None,
                        emoji: Optional[str] = None,
                        description: Optional[app_commands.Range[str, 1, 100]] = None):
    cat = db.one("SELECT * FROM categories WHERE guild_id=? AND lower(name)=lower(?)", (inter.guild_id, name))
    if not cat:
        return await inter.response.send_message("No ticket type with that name.", ephemeral=True)
    if emoji and not valid_emoji(emoji.strip()):
        return await inter.response.send_message("That doesn't look like an emoji.", ephemeral=True)
    db.run("UPDATE categories SET name=?, emoji=?, description=? WHERE id=?",
           (new_name or cat["name"], emoji.strip() if emoji else cat["emoji"], description or cat["description"], cat["id"]))
    await inter.response.send_message("✅ Updated. Panels refreshed.", ephemeral=True)
    await refresh_panels(inter.guild)


@category_group.command(name="list", description="Show all ticket types")
async def category_list(inter: discord.Interaction):
    cats = get_categories(inter.guild_id)
    text = "\n".join(f"{c['emoji'] or '•'} **{c['name']}** — {c['description'] or ''}" for c in cats) or \
        "No ticket types yet. Add one with `/category add` or reset to defaults with `/category reset`."
    await inter.response.send_message(embed=discord.Embed(title="🎫 Ticket types", description=text, color=COLOR),
                                      ephemeral=True)


@category_group.command(name="reset", description="Replace all ticket types with the defaults")
async def category_reset(inter: discord.Interaction):
    db.run("DELETE FROM categories WHERE guild_id=?", (inter.guild_id,))
    seed_categories(inter.guild_id)
    await inter.response.send_message("♻️ Ticket types reset to defaults. Panels updated.", ephemeral=True)
    await refresh_panels(inter.guild)


# ---- /ticket ----

ticket_group = app_commands.Group(name="ticket", description="Manage the ticket you're in", guild_only=True)


async def require_ticket(inter: discord.Interaction, staff_only: bool = True):
    ticket = get_ticket(inter.channel_id)
    if not ticket:
        await inter.response.send_message("Use this inside an open ticket channel.", ephemeral=True)
        return None
    cfg = get_config(inter.guild_id)
    allowed = is_staff(inter.user, cfg) or (not staff_only and inter.user.id == ticket["user_id"])
    if not allowed:
        await inter.response.send_message("You don't have permission to do that.", ephemeral=True)
        return None
    return ticket


@ticket_group.command(name="add", description="Add someone to this ticket")
async def ticket_add(inter: discord.Interaction, user: discord.Member):
    if not await require_ticket(inter):
        return
    await inter.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True,
                                        attach_files=True, embed_links=True)
    await inter.response.send_message(embed=discord.Embed(description=f"➕ Added {user.mention} to the ticket.", color=GREEN))


@ticket_group.command(name="remove", description="Remove someone from this ticket")
async def ticket_remove(inter: discord.Interaction, user: discord.Member):
    ticket = await require_ticket(inter)
    if not ticket:
        return
    if user.id == ticket["user_id"]:
        return await inter.response.send_message("You can't remove the person who opened the ticket.", ephemeral=True)
    await inter.channel.set_permissions(user, overwrite=None)
    await inter.response.send_message(embed=discord.Embed(description=f"➖ Removed {user.mention} from the ticket.", color=RED))


@ticket_group.command(name="rename", description="Rename this ticket channel")
async def ticket_rename(inter: discord.Interaction, name: app_commands.Range[str, 1, 90]):
    if not await require_ticket(inter):
        return
    await inter.response.defer(ephemeral=True)
    await inter.channel.edit(name=slugify(name))
    await inter.followup.send("✅ Renamed. (Discord allows 2 renames per 10 minutes.)", ephemeral=True)


@ticket_group.command(name="close", description="Close this ticket")
async def ticket_close(inter: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 300]] = None):
    ticket = await require_ticket(inter, staff_only=False)
    if not ticket:
        return
    await inter.response.send_message("Closing ticket...", ephemeral=True)
    await close_ticket(inter.guild, inter.channel, ticket, inter.user, reason or "No reason given")


# ---- error handling ----

@bot.tree.error
async def on_app_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use that."
    elif isinstance(original, discord.Forbidden):
        msg = "I'm missing permissions. Make sure my role has **Manage Channels** and **Manage Roles** and sits above the staff role."
    else:
        log.exception("Command error", exc_info=original)
        msg = "Something went wrong running that command."
    try:
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and paste your bot token in it.")
    bot.run(TOKEN)
