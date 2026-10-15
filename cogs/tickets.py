"""Ticket panel clicks, ticket buttons, /ticket commands and auto-close."""

import asyncio
import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from midtown import config
from midtown.database import db
from midtown.tickets import (close_ticket, cooldown_left, create_ticket, mark_channel_gone, open_channels,
                             set_claim_button)
from midtown.utils import (gatekeep, is_staff, log, panel_view, report_error, reply, send_guild_log, slugify,
                           strip_priority, with_priority,
                           static_view, unix, utcnow)


RENAME_LIMIT_MSG = ("⚠️ Discord only allows 2 channel renames every 10 minutes, so the channel name "
                    "couldn't be updated right now. Try again in a few minutes.")


class OpenTicketModal(discord.ui.Modal):
    def __init__(self, cat, panel_message: discord.Message | None):
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
        cfg = db.config(inter.guild_id)
        rows = db.open_tickets_for(inter.guild_id, inter.user.id)
        if len(rows) >= cfg["ticket_limit"]:
            links = ", ".join(f"<#{r['channel_id']}>" for r in rows)
            return await reply(inter, f"You already have an open ticket: {links}")

        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await create_ticket(inter, self.cat, str(self.subject.value), str(self.details.value))
        except discord.Forbidden:
            return await reply(inter, "I couldn't create your ticket because I'm missing permissions. "
                                      "Please tell a staff member to run `/setup` again.")
        except discord.HTTPException as e:
            return await reply(inter, f"Couldn't create your ticket: {e.text or e}")

        await reply(inter, f"✅ Your ticket is open: {channel.mention}")
        if self.panel_message:  # reset the dropdown so the same option can be picked again
            try:
                await self.panel_message.edit(view=panel_view(inter.guild_id))
            except discord.HTTPException:
                pass

    async def on_error(self, inter: discord.Interaction, error: Exception):
        code = await report_error(inter.client, error, f"open ticket modal in {inter.guild_id}")
        await reply(inter, f"Something went wrong (error `{code}`).")


class CloseModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=300,
                                  placeholder="e.g. Issue resolved")

    async def on_submit(self, inter: discord.Interaction):
        ticket = db.open_ticket(inter.channel_id)
        if not ticket:
            return await reply(inter, "This ticket is already closed.")
        await reply(inter, "Closing ticket...")
        await close_ticket(inter.client, inter.guild, inter.channel, ticket, inter.user,
                           str(self.reason.value) or "No reason given")


class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Manage the ticket you're in", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.autoclose.start()

    def cog_unload(self):
        self.autoclose.cancel()

    # ---- buttons & dropdown ----

    @commands.Cog.listener()
    async def on_interaction(self, inter: discord.Interaction):
        if inter.type != discord.InteractionType.component:
            return
        cid = (inter.data or {}).get("custom_id", "")
        if not cid.startswith("md:") or not await gatekeep(inter):
            return
        try:
            if cid == "md:open":
                await self.handle_open(inter)
            elif cid == "md:claim":
                await self.handle_claim(inter)
            elif cid == "md:close":
                await self.handle_close(inter)
            elif cid.startswith("md:rate:"):
                await self.handle_rate(inter, cid)
            elif cid in ("md:creq:yes", "md:creq:no"):
                await self.handle_close_request(inter, accepted=cid.endswith("yes"))
        except Exception as error:
            code = await report_error(self.bot, error, f"button `{cid}` in {inter.guild_id}")
            try:
                await reply(inter, f"Something went wrong (error `{code}`). Please try again.")
            except discord.HTTPException:
                pass

    async def handle_open(self, inter: discord.Interaction):
        if inter.guild is None:
            return
        cfg = db.config(inter.guild.id)
        if not cfg["staff_role_id"]:
            return await reply(inter, "Tickets aren't set up yet. An admin needs to run `/setup`.")
        values = inter.data.get("values") or []
        if not values:
            return
        cat = db.one("SELECT * FROM categories WHERE id=? AND guild_id=?", (int(values[0]), inter.guild.id))
        if not cat:
            return await reply(inter, "That ticket type was removed. Please pick another.")
        block = db.guild_block(inter.guild.id, inter.user.id)
        if block:
            return await reply(inter, "⛔ You've been blocked from opening tickets in this server."
                               + (f"\n**Reason:** {block['reason']}" if block["reason"] else ""))
        wait = cooldown_left(inter.guild.id, inter.user.id)
        if wait:
            return await reply(inter, f"You just opened a ticket. Please wait {wait}s before opening another.")
        rows = db.open_tickets_for(inter.guild.id, inter.user.id)
        if len(rows) >= cfg["ticket_limit"]:
            links = ", ".join(f"<#{r['channel_id']}>" for r in rows)
            return await reply(inter, f"You already have an open ticket: {links}\nClose it before opening another.")
        await inter.response.send_modal(OpenTicketModal(cat, inter.message))

    async def handle_claim(self, inter: discord.Interaction):
        ticket = db.open_ticket(inter.channel_id)
        if not ticket:
            return await reply(inter, "This isn't an open ticket.")
        if not is_staff(inter.user, db.config(inter.guild_id)):
            return await reply(inter, "Only staff can claim tickets.")
        if ticket["claimed_by"]:
            return await reply(inter, f"Already claimed by <@{ticket['claimed_by']}>.")
        await inter.response.defer()
        await self.set_claim(inter, ticket, inter.user)

    async def set_claim(self, inter: discord.Interaction, ticket, member: discord.Member | None):
        """Claim (member), transfer (member, already claimed) or unclaim (None) a ticket."""
        previous = ticket["claimed_by"]
        db.run("UPDATE tickets SET claimed_by=? WHERE id=?", (member.id if member else None, ticket["id"]))
        await set_claim_button(inter.channel, ticket, member.display_name if member else None)

        if member is None:
            text, title, color = f"↩️ {inter.user.mention} unclaimed this ticket. Any staff member can pick it up.", \
                "unclaimed", config.GREY
        elif previous:
            text, title, color = f"🔁 This ticket was transferred to {member.mention}.", "transferred", config.YELLOW
        else:
            text, title, color = f"🙋 {member.mention} claimed this ticket and will be helping you.", "claimed", \
                config.GREEN
        await inter.channel.send(embed=discord.Embed(description=text, color=color))

        le = discord.Embed(title=f"🙋 Ticket #{ticket['number']:04d} {title}", color=config.YELLOW, timestamp=utcnow())
        if previous:
            le.add_field(name="From", value=f"<@{previous}>")
        le.add_field(name="To" if previous else "Staff", value=member.mention if member else "Nobody")
        le.add_field(name="By", value=inter.user.mention)
        le.add_field(name="Channel", value=inter.channel.mention)
        await send_guild_log(inter.guild, le)

    async def handle_close_request(self, inter: discord.Interaction, accepted: bool):
        ticket = db.open_ticket(inter.channel_id)
        if not ticket:
            return await inter.response.edit_message(view=None)
        if inter.user.id != ticket["user_id"] and not is_staff(inter.user, db.config(inter.guild_id)):
            return await reply(inter, "Only the person who opened this ticket can answer this.")
        if not accepted:
            await inter.response.edit_message(
                embed=discord.Embed(description=f"🔓 {inter.user.mention} wants to keep this ticket open.",
                                    color=config.GREEN), view=None)
            return
        await inter.response.edit_message(
            embed=discord.Embed(description=f"✅ {inter.user.mention} accepted the close request.",
                                color=config.GREEN), view=None)
        await close_ticket(self.bot, inter.guild, inter.channel, ticket, inter.user, "Accepted a close request")

    async def handle_close(self, inter: discord.Interaction):
        ticket = db.open_ticket(inter.channel_id)
        if not ticket:
            return await reply(inter, "This ticket is already closed.")
        if not (is_staff(inter.user, db.config(inter.guild_id)) or inter.user.id == ticket["user_id"]):
            return await reply(inter, "Only staff or the ticket owner can close this.")
        await inter.response.send_modal(CloseModal())

    async def handle_rate(self, inter: discord.Interaction, cid: str):
        _, _, tid, n = cid.split(":")
        ticket = db.one("SELECT * FROM tickets WHERE id=?", (int(tid),))
        if not ticket or ticket["user_id"] != inter.user.id:
            return await reply(inter, "This rating isn't yours to give.")
        if ticket["rating"]:
            return await inter.response.edit_message(view=None)
        stars = int(n)
        db.run("UPDATE tickets SET rating=? WHERE id=?", (stars, ticket["id"]))
        await inter.response.edit_message(
            content=f"Thanks for your feedback! You rated us {'⭐' * stars} ({stars}/5).", view=None)
        guild = self.bot.get_guild(ticket["guild_id"])
        if guild:
            le = discord.Embed(title=f"⭐ Ticket #{ticket['number']:04d} rated {stars}/5", color=config.YELLOW,
                               timestamp=utcnow())
            le.add_field(name="User", value=f"<@{ticket['user_id']}>")
            le.add_field(name="Staff", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "Nobody claimed it")
            await send_guild_log(guild, le)

    # ---- activity tracking ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if message.channel.id in open_channels:
            db.run("UPDATE tickets SET last_activity=?, warned=0 WHERE channel_id=? AND status='open'",
                   (utcnow().isoformat(), message.channel.id))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if channel.id in open_channels:
            mark_channel_gone(channel.id)

    # ---- auto-close ----

    @tasks.loop(minutes=10)
    async def autoclose(self):
        rows = db.all("SELECT t.*, g.autoclose_hours FROM tickets t JOIN guilds g ON g.guild_id=t.guild_id "
                      "WHERE t.status='open' AND g.autoclose_hours > 0")
        for t in rows:
            guild = self.bot.get_guild(t["guild_id"])
            if guild is None:
                continue  # bot left that server or it's unavailable
            channel = guild.get_channel(t["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                mark_channel_gone(t["channel_id"])
                continue
            idle = utcnow() - dt.datetime.fromisoformat(t["last_activity"])
            limit = dt.timedelta(hours=t["autoclose_hours"])
            try:
                if not t["warned"] and idle >= limit:
                    await channel.send(content=f"<@{t['user_id']}>", embed=discord.Embed(
                        description="⏰ This ticket has been quiet for a while. It will close automatically in "
                                    "**1 hour** unless someone sends a message.", color=config.YELLOW))
                    db.run("UPDATE tickets SET warned=1 WHERE id=?", (t["id"],))
                elif t["warned"] and idle >= limit + dt.timedelta(hours=1):
                    await close_ticket(self.bot, guild, channel, t, None, "Closed automatically for inactivity")
            except discord.HTTPException as e:
                log.warning("Auto-close error on ticket %s: %s", t["id"], e)

    @autoclose.before_loop
    async def before_autoclose(self):
        await self.bot.wait_until_ready()

    # ---- /ticket ----

    async def _require_ticket(self, inter: discord.Interaction, staff_only: bool = True):
        ticket = db.open_ticket(inter.channel_id)
        if not ticket:
            await reply(inter, "Use this inside an open ticket channel.")
            return None
        allowed = is_staff(inter.user, db.config(inter.guild_id)) or \
            (not staff_only and inter.user.id == ticket["user_id"])
        if not allowed:
            await reply(inter, "You don't have permission to do that.")
            return None
        return ticket

    @ticket.command(name="add", description="Add someone to this ticket")
    async def ticket_add(self, inter: discord.Interaction, user: discord.Member):
        if not await self._require_ticket(inter):
            return
        await inter.channel.set_permissions(user, view_channel=True, send_messages=True, read_message_history=True,
                                            attach_files=True, embed_links=True)
        await reply(inter, embed=discord.Embed(description=f"➕ Added {user.mention} to the ticket.",
                                               color=config.GREEN), ephemeral=False)

    @ticket.command(name="remove", description="Remove someone from this ticket")
    async def ticket_remove(self, inter: discord.Interaction, user: discord.Member):
        ticket = await self._require_ticket(inter)
        if not ticket:
            return
        if user.id == ticket["user_id"]:
            return await reply(inter, "You can't remove the person who opened the ticket.")
        await inter.channel.set_permissions(user, overwrite=None)
        await reply(inter, embed=discord.Embed(description=f"➖ Removed {user.mention} from the ticket.",
                                               color=config.RED), ephemeral=False)

    @ticket.command(name="rename", description="Rename this ticket channel")
    async def ticket_rename(self, inter: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        if not await self._require_ticket(inter):
            return
        ticket = db.open_ticket(inter.channel_id)
        await inter.response.defer(ephemeral=True)
        if not await self._rename(inter.channel, with_priority(slugify(name), ticket["priority"] or "normal")):
            return await reply(inter, RENAME_LIMIT_MSG)
        await reply(inter, "✅ Renamed.")

    async def _rename(self, channel: discord.TextChannel, name: str) -> bool:
        """Rename without hanging: Discord only allows 2 channel renames per 10 minutes."""
        if channel.name == name:
            return True
        try:
            await asyncio.wait_for(channel.edit(name=name), timeout=8)
            return True
        except (asyncio.TimeoutError, discord.HTTPException):
            return False

    @ticket.command(name="priority", description="Set how urgent this ticket is")
    @app_commands.describe(level="Urgent pings the staff role")
    @app_commands.choices(level=[app_commands.Choice(name=label, value=key)
                                 for key, (label, _, _) in config.PRIORITIES.items()])
    async def ticket_priority(self, inter: discord.Interaction, level: app_commands.Choice[str]):
        ticket = await self._require_ticket(inter)
        if not ticket:
            return
        if (ticket["priority"] or "normal") == level.value:
            return await reply(inter, f"This ticket is already **{level.name}**.")
        await inter.response.defer(ephemeral=True)
        db.run("UPDATE tickets SET priority=? WHERE id=?", (level.value, ticket["id"]))
        renamed = await self._rename(inter.channel, with_priority(strip_priority(inter.channel.name), level.value))

        label, _, color = config.PRIORITIES[level.value]
        cfg = db.config(inter.guild_id)
        staff = inter.guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
        ping = staff.mention if level.value == "urgent" and staff else None
        await inter.channel.send(content=ping, embed=discord.Embed(
            description=f"Priority set to **{label}** by {inter.user.mention}.", color=color),
            allowed_mentions=discord.AllowedMentions(roles=True))
        await reply(inter, "✅ Priority updated." + ("" if renamed else f"\n{RENAME_LIMIT_MSG}"))

        le = discord.Embed(title=f"Ticket #{ticket['number']:04d} priority: {label}", color=color, timestamp=utcnow())
        le.add_field(name="Channel", value=inter.channel.mention)
        le.add_field(name="By", value=inter.user.mention)
        await send_guild_log(inter.guild, le)

    @ticket.command(name="close", description="Close this ticket")
    async def ticket_close(self, inter: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 300]] = None):
        ticket = await self._require_ticket(inter, staff_only=False)
        if not ticket:
            return
        await reply(inter, "Closing ticket...")
        await close_ticket(self.bot, inter.guild, inter.channel, ticket, inter.user, reason or "No reason given")

    @ticket.command(name="claim", description="Claim this ticket")
    async def ticket_claim(self, inter: discord.Interaction):
        ticket = await self._require_ticket(inter)
        if not ticket:
            return
        if ticket["claimed_by"]:
            return await reply(inter, f"Already claimed by <@{ticket['claimed_by']}>. Use `/ticket transfer` instead.")
        await reply(inter, "✅ Claimed.")
        await self.set_claim(inter, ticket, inter.user)

    @ticket.command(name="unclaim", description="Release this ticket so another staff member can take it")
    async def ticket_unclaim(self, inter: discord.Interaction):
        ticket = await self._require_ticket(inter)
        if not ticket:
            return
        if not ticket["claimed_by"]:
            return await reply(inter, "Nobody has claimed this ticket.")
        if ticket["claimed_by"] != inter.user.id and not inter.user.guild_permissions.manage_guild:
            return await reply(inter, "Only the staff member who claimed it (or an admin) can unclaim it.")
        await reply(inter, "✅ Unclaimed.")
        await self.set_claim(inter, ticket, None)

    @ticket.command(name="transfer", description="Hand this ticket to another staff member")
    @app_commands.describe(staff="Who should take over")
    async def ticket_transfer(self, inter: discord.Interaction, staff: discord.Member):
        ticket = await self._require_ticket(inter)
        if not ticket:
            return
        if staff.bot or not is_staff(staff, db.config(inter.guild_id)):
            return await reply(inter, f"{staff.mention} isn't on the staff team.")
        if ticket["claimed_by"] == staff.id:
            return await reply(inter, f"{staff.mention} already has this ticket.")
        await reply(inter, f"✅ Transferred to {staff.mention}.")
        await self.set_claim(inter, ticket, staff)

    @ticket.command(name="closerequest", description="Ask the member if their issue is solved and the ticket can close")
    @app_commands.describe(note="Optional message to the member")
    async def ticket_closerequest(self, inter: discord.Interaction,
                                  note: Optional[app_commands.Range[str, 1, 500]] = None):
        ticket = await self._require_ticket(inter)
        if not ticket:
            return
        e = discord.Embed(title="❓ Can we close this ticket?", color=config.YELLOW,
                          description=(f"{note}\n\n" if note else "")
                          + f"{inter.user.mention} thinks your issue is solved. Can we close this ticket?")
        view = static_view(
            discord.ui.Button(label="Yes, close it", style=discord.ButtonStyle.success, emoji="✅",
                              custom_id="md:creq:yes"),
            discord.ui.Button(label="No, I still need help", style=discord.ButtonStyle.secondary, emoji="❌",
                              custom_id="md:creq:no"))
        await reply(inter, "Close request sent.")
        await inter.channel.send(content=f"<@{ticket['user_id']}>", embed=e, view=view)

    @ticket.command(name="history", description="See a member's past tickets in this server")
    async def ticket_history(self, inter: discord.Interaction, member: discord.Member):
        if not is_staff(inter.user, db.config(inter.guild_id)):
            return await reply(inter, "Only staff can see ticket history.")
        rows = db.all("SELECT * FROM tickets WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
                      (inter.guild_id, member.id))
        total = db.one("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND user_id=?",
                       (inter.guild_id, member.id))["c"]
        status_icon = {"open": "🟢", "closing": "🟡", "closed": "⚫"}
        lines = []
        for t in rows:
            where = f"<#{t['channel_id']}>" if t["status"] == "open" else f"#{t['number']:04d}"
            line = f"{status_icon.get(t['status'], '⚫')} {where} · {t['category']} · <t:{unix(t['opened_at'])}:R>"
            prefix = config.PRIORITIES.get(t["priority"] or "normal", config.PRIORITIES["normal"])[1]
            if prefix:
                line += f" · {prefix}"
            if t["rating"]:
                line += f" · {t['rating']}⭐"
            if t["subject"]:
                line += f"\n   {discord.utils.escape_markdown(t['subject'][:80])}"
            lines.append(line)
        e = discord.Embed(title=f"🗂️ Tickets from {member.display_name} ({total})", color=config.COLOR,
                          description="\n".join(lines) or "No tickets yet.")
        block = db.guild_block(inter.guild_id, member.id)
        if block:
            e.add_field(name="⛔ Blocked from tickets", value=block["reason"] or "No reason given")
        e.set_thumbnail(url=member.display_avatar.url)
        await reply(inter, embed=e)

    @ticket.command(name="block", description="Stop a member from opening tickets in this server")
    async def ticket_block(self, inter: discord.Interaction, member: discord.Member,
                           reason: Optional[app_commands.Range[str, 1, 300]] = None):
        cfg = db.config(inter.guild_id)
        if not is_staff(inter.user, cfg):
            return await reply(inter, "Only staff can block members.")
        if member.bot or is_staff(member, cfg):
            return await reply(inter, "You can't block bots or staff members.")
        db.guild_block_add(inter.guild_id, member.id, reason, inter.user.id)
        await reply(inter, f"⛔ {member.mention} can no longer open tickets here.")
        le = discord.Embed(title="⛔ Member blocked from tickets", color=config.RED, timestamp=utcnow())
        le.add_field(name="Member", value=member.mention)
        le.add_field(name="By", value=inter.user.mention)
        le.add_field(name="Reason", value=reason or "No reason given", inline=False)
        await send_guild_log(inter.guild, le)

    @ticket.command(name="unblock", description="Let a blocked member open tickets again")
    async def ticket_unblock(self, inter: discord.Interaction, member: discord.User):
        if not is_staff(inter.user, db.config(inter.guild_id)):
            return await reply(inter, "Only staff can unblock members.")
        if not db.guild_block_remove(inter.guild_id, member.id):
            return await reply(inter, f"{member.mention} isn't blocked.")
        await reply(inter, f"✅ {member.mention} can open tickets again.")
        le = discord.Embed(title="✅ Member unblocked from tickets", color=config.GREEN, timestamp=utcnow())
        le.add_field(name="Member", value=member.mention)
        le.add_field(name="By", value=inter.user.mention)
        await send_guild_log(inter.guild, le)

    @ticket.command(name="blocked", description="List members blocked from opening tickets")
    async def ticket_blocked(self, inter: discord.Interaction):
        if not is_staff(inter.user, db.config(inter.guild_id)):
            return await reply(inter, "Only staff can see this.")
        rows = db.guild_blocks(inter.guild_id)
        lines = [f"<@{r['user_id']}> — {r['reason'] or 'no reason'} (by <@{r['added_by']}>)" for r in rows[:30]]
        await reply(inter, embed=discord.Embed(title=f"⛔ Blocked from tickets ({len(rows)})",
                                               description="\n".join(lines) or "Nobody is blocked.",
                                               color=config.RED))


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
