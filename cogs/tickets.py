"""Ticket panel clicks, ticket buttons, /ticket commands and auto-close."""

import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from midtown import config
from midtown.database import db
from midtown.tickets import close_ticket, create_ticket, mark_channel_gone, open_channels
from midtown.utils import (gatekeep, is_staff, log, panel_view, report_error, reply, send_guild_log, slugify,
                           ticket_controls, utcnow)


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

        db.run("UPDATE tickets SET claimed_by=? WHERE id=?", (inter.user.id, ticket["id"]))
        await inter.response.edit_message(view=ticket_controls(inter.user.display_name))
        await inter.channel.send(embed=discord.Embed(
            description=f"🙋 {inter.user.mention} claimed this ticket and will be helping you.", color=config.GREEN))
        le = discord.Embed(title=f"🙋 Ticket #{ticket['number']:04d} claimed", color=config.YELLOW, timestamp=utcnow())
        le.add_field(name="Staff", value=inter.user.mention)
        le.add_field(name="Channel", value=inter.channel.mention)
        await send_guild_log(inter.guild, le)

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
        await inter.response.defer(ephemeral=True)
        await inter.channel.edit(name=slugify(name))
        await reply(inter, "✅ Renamed. (Discord allows 2 renames per 10 minutes.)")

    @ticket.command(name="close", description="Close this ticket")
    async def ticket_close(self, inter: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 300]] = None):
        ticket = await self._require_ticket(inter, staff_only=False)
        if not ticket:
            return
        await reply(inter, "Closing ticket...")
        await close_ticket(self.bot, inter.guild, inter.channel, ticket, inter.user, reason or "No reason given")


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
