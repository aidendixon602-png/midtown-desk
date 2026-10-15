"""Server admin commands: /setup, /panel, /settings, /stats, /category."""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from midtown import config
from midtown.database import db
from midtown.utils import missing_perms, panel_embed, panel_view, refresh_panels, reply, valid_emoji

ADMIN = discord.Permissions(manage_guild=True)


async def category_autocomplete(inter: discord.Interaction, current: str):
    return [app_commands.Choice(name=c["name"], value=c["name"])
            for c in db.categories(inter.guild_id) if current.lower() in c["name"].lower()][:25]


class Admin(commands.Cog):
    category = app_commands.Group(name="category", description="Manage ticket types in the dropdown",
                                  guild_only=True, default_permissions=ADMIN)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not inter.guild or not inter.permissions.manage_guild:
            raise app_commands.MissingPermissions(["manage_guild"])
        return True

    # ---- /setup ----

    @app_commands.command(name="setup", description=f"Set up {config.BOT_NAME}: creates everything it needs")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        staff_role="Your staff role (leave empty and I'll create a 'Ticket Staff' role)",
        panel_channel="Where members open tickets (leave empty and I'll create #open-a-ticket)",
        category="Category for ticket channels (leave empty and I'll create one)",
        log_channel="Channel for logs + transcripts (leave empty and I'll create one)")
    async def setup_cmd(self, inter: discord.Interaction,
                        staff_role: Optional[discord.Role] = None,
                        panel_channel: Optional[discord.TextChannel] = None,
                        category: Optional[discord.CategoryChannel] = None,
                        log_channel: Optional[discord.TextChannel] = None):
        guild = inter.guild
        missing = missing_perms(guild)
        if missing:
            return await reply(inter, embed=discord.Embed(
                title="⚠️ I'm missing permissions",
                description="Give my role these permissions, then run `/setup` again:\n• " + "\n• ".join(missing),
                color=config.RED))
        if staff_role and (staff_role.is_default() or staff_role.managed):
            return await reply(inter, "Pick a real staff role (not @everyone or a bot's role).")

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = db.config(guild.id)
        created = []  # what this run made, for the summary

        # Reuse what a previous /setup made if it still exists, so running it again never makes duplicates
        if staff_role is None:
            staff_role = guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
        if staff_role is None:
            staff_role = await guild.create_role(
                name="Ticket Staff", color=discord.Color(config.COLOR), mentionable=True,
                reason=f"{config.BOT_NAME} setup by {inter.user}")
            created.append(f"Role {staff_role.mention}")
        db.update_config(guild.id, staff_role_id=staff_role.id)

        if category is None:
            existing = guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
            category = existing if isinstance(existing, discord.CategoryChannel) else None
        if category is None:
            category = await guild.create_category("Tickets", overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                staff_role: discord.PermissionOverwrite(view_channel=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True)},
                reason=f"{config.BOT_NAME} setup")
            created.append(f"Category **{category.name}**")
        db.update_config(guild.id, category_id=category.id)

        if log_channel is None:
            existing = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
            log_channel = existing if isinstance(existing, discord.TextChannel) else None
        if log_channel is None:
            log_channel = await guild.create_text_channel("ticket-logs", category=category, overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                      embed_links=True)},
                reason=f"{config.BOT_NAME} setup")
            created.append(f"Channel {log_channel.mention}")

        db.update_config(guild.id, staff_role_id=staff_role.id, category_id=category.id, log_channel_id=log_channel.id)
        if not db.categories(guild.id):
            db.seed_categories(guild.id)

        # Ticket menu: post one unless a panel already exists (or a channel was picked)
        panel_msg = None
        has_panel = await self._has_live_panel(guild)
        if panel_channel is not None or not has_panel:
            if panel_channel is None:
                panel_channel = await guild.create_text_channel("open-a-ticket", position=0, overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                                    add_reactions=False, create_public_threads=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)},
                    reason=f"{config.BOT_NAME} setup")
                created.append(f"Channel {panel_channel.mention}")
            perms = panel_channel.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                panel_msg = await panel_channel.send(
                    embed=panel_embed("Support Tickets", "Need help? Pick a ticket type from the menu below and "
                                                         "a staff member will be with you shortly."),
                    view=panel_view(guild.id))
                db.run("INSERT INTO panels(message_id, guild_id, channel_id) VALUES(?,?,?)",
                       (panel_msg.id, guild.id, panel_channel.id))

        e = discord.Embed(title=f"✅ {config.BOT_NAME} is ready", color=config.GREEN)
        e.add_field(name="Staff role", value=staff_role.mention)
        e.add_field(name="Ticket category", value=category.name)
        e.add_field(name="Log channel", value=log_channel.mention)
        if panel_msg:
            e.add_field(name="Ticket menu", value=f"[Posted in {panel_channel.mention}]({panel_msg.jump_url})")
        elif panel_channel is not None:
            e.add_field(name="Ticket menu", value=f"⚠️ I can't post in {panel_channel.mention}. Check my permissions there.")
        else:
            e.add_field(name="Ticket menu", value="Already posted (use `/panel` to post another)")
        if created:
            e.add_field(name="Created", value="\n".join(created), inline=False)
        e.add_field(name="Next steps", inline=False,
                    value=f"• Give {staff_role.mention} to your staff so they can see and answer tickets\n"
                          "• Customize ticket types with **/category** and limits with **/settings**")
        await reply(inter, embed=e)

    async def _has_live_panel(self, guild: discord.Guild) -> bool:
        for p in db.all("SELECT * FROM panels WHERE guild_id=?", (guild.id,)):
            channel = guild.get_channel(p["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                db.run("DELETE FROM panels WHERE message_id=?", (p["message_id"],))
                continue
            try:
                await channel.fetch_message(p["message_id"])
                return True
            except discord.NotFound:
                db.run("DELETE FROM panels WHERE message_id=?", (p["message_id"],))
            except discord.HTTPException:
                return True  # can't check right now; assume it's there rather than posting a duplicate
        return False

    # ---- /panel ----

    @app_commands.command(name="panel", description="Post the ticket panel members use to open tickets")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Where to post it (defaults to this channel)", title="Panel title",
                           description="Panel text (use \\n for a new line)")
    async def panel_cmd(self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                        title: app_commands.Range[str, 1, 256] = "Support Tickets",
                        description: app_commands.Range[str, 1, 2000] =
                        "Need help? Pick a ticket type from the menu below and a staff member will be with you shortly."):
        if not db.config(inter.guild_id)["staff_role_id"]:
            return await reply(inter, "Run `/setup` first.")
        channel = channel or inter.channel
        if not isinstance(channel, discord.TextChannel):
            return await reply(inter, "Pick a normal text channel.")
        perms = channel.permissions_for(inter.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await reply(inter, f"I can't send messages or embeds in {channel.mention}.")
        await inter.response.defer(ephemeral=True, thinking=True)
        msg = await channel.send(embed=panel_embed(title, description.replace("\\n", "\n")),
                                 view=panel_view(inter.guild_id))
        db.run("INSERT INTO panels(message_id, guild_id, channel_id) VALUES(?,?,?)",
               (msg.id, inter.guild_id, channel.id))
        await reply(inter, f"✅ Panel posted in {channel.mention}.")

    # ---- /settings ----

    @app_commands.command(name="settings", description="View or change ticket settings")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        ticket_limit="How many tickets one person can have open at once",
        autoclose_hours="Close inactive tickets after this many hours (0 = off)",
        dm_transcripts="DM the user their transcript when a ticket closes",
        ask_rating="Ask users to rate the help after a ticket closes",
        weekly_report="Post a weekly stats report in the log channel",
        staff_role="Change the staff role", log_channel="Change the log channel",
        category="Change the category tickets are created in")
    async def settings_cmd(self, inter: discord.Interaction,
                           ticket_limit: Optional[app_commands.Range[int, 1, 10]] = None,
                           autoclose_hours: Optional[app_commands.Range[int, 0, 720]] = None,
                           dm_transcripts: Optional[bool] = None,
                           ask_rating: Optional[bool] = None,
                           weekly_report: Optional[bool] = None,
                           staff_role: Optional[discord.Role] = None,
                           log_channel: Optional[discord.TextChannel] = None,
                           category: Optional[discord.CategoryChannel] = None):
        if staff_role and (staff_role.is_default() or staff_role.managed):
            return await reply(inter, "Pick a real staff role (not @everyone or a bot's role).")
        changed = db.update_config(
            inter.guild_id,
            ticket_limit=ticket_limit,
            autoclose_hours=autoclose_hours,
            dm_transcripts=None if dm_transcripts is None else int(dm_transcripts),
            ask_rating=None if ask_rating is None else int(ask_rating),
            weekly_report=None if weekly_report is None else int(weekly_report),
            staff_role_id=staff_role.id if staff_role else None,
            log_channel_id=log_channel.id if log_channel else None,
            category_id=category.id if category else None)
        cfg = db.config(inter.guild_id)
        on = lambda v: "On" if v else "Off"  # noqa: E731
        e = discord.Embed(title="⚙️ Settings updated" if changed else "⚙️ Current settings", color=config.COLOR)
        e.add_field(name="Staff role", value=f"<@&{cfg['staff_role_id']}>" if cfg["staff_role_id"] else "Not set")
        e.add_field(name="Log channel", value=f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "Not set")
        e.add_field(name="Ticket category", value=f"<#{cfg['category_id']}>" if cfg["category_id"] else "Not set")
        e.add_field(name="Open tickets per user", value=str(cfg["ticket_limit"]))
        e.add_field(name="Auto-close", value=f"{cfg['autoclose_hours']}h" if cfg["autoclose_hours"] else "Off")
        e.add_field(name="DM transcripts", value=on(cfg["dm_transcripts"]))
        e.add_field(name="Ratings", value=on(cfg["ask_rating"]))
        e.add_field(name="Weekly report", value=on(cfg["weekly_report"]))
        await reply(inter, embed=e)

    # ---- /stats ----

    @app_commands.command(name="stats", description="Ticket stats and staff leaderboard for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def stats_cmd(self, inter: discord.Interaction):
        gid = inter.guild_id
        total = db.one("SELECT COUNT(*) c FROM tickets WHERE guild_id=?", (gid,))["c"]
        open_ = db.one("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open'", (gid,))["c"]
        avg = db.one("SELECT AVG(rating) a, COUNT(rating) n FROM tickets WHERE guild_id=? AND rating IS NOT NULL", (gid,))
        top = db.all("SELECT claimed_by, COUNT(*) c, AVG(rating) r FROM tickets WHERE guild_id=? "
                     "AND claimed_by IS NOT NULL GROUP BY claimed_by ORDER BY c DESC LIMIT 10", (gid,))
        e = discord.Embed(title="📊 Ticket stats", color=config.COLOR)
        e.add_field(name="Total tickets", value=str(total))
        e.add_field(name="Open now", value=str(open_))
        e.add_field(name="Closed", value=str(total - open_))
        e.add_field(name="Average rating", inline=False,
                    value=f"{avg['a']:.2f} ⭐ ({avg['n']} ratings)" if avg["n"] else "No ratings yet")
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{r['claimed_by']}> · {r['c']} tickets"
                 + (f" · {r['r']:.1f} ⭐" if r["r"] else "") for i, r in enumerate(top)]
        e.add_field(name="Staff leaderboard", value="\n".join(lines) or "No claimed tickets yet", inline=False)
        await reply(inter, embed=e)

    # ---- /category ----

    @category.command(name="add", description="Add a ticket type to the dropdown")
    @app_commands.describe(name="Name shown in the dropdown", emoji="Emoji for it (optional)",
                           description="Short line under the name (optional)")
    async def category_add(self, inter: discord.Interaction, name: app_commands.Range[str, 1, 50],
                           emoji: Optional[str] = None,
                           description: Optional[app_commands.Range[str, 1, 100]] = None):
        cats = db.categories(inter.guild_id)
        if len(cats) >= 25:
            return await reply(inter, "Discord dropdowns max out at 25 options.")
        if any(c["name"].lower() == name.lower() for c in cats):
            return await reply(inter, "A ticket type with that name already exists.")
        if emoji and not valid_emoji(emoji.strip()):
            return await reply(inter, "That doesn't look like an emoji. Try something like 🎮.")
        position = max((c["position"] for c in cats), default=-1) + 1
        db.run("INSERT INTO categories(guild_id, name, emoji, description, position) VALUES(?,?,?,?,?)",
               (inter.guild_id, name, emoji.strip() if emoji else None, description, position))
        await reply(inter, f"✅ Added **{name}**. Panels updated.")
        await refresh_panels(inter.guild)

    @category.command(name="remove", description="Remove a ticket type from the dropdown")
    @app_commands.autocomplete(name=category_autocomplete)
    async def category_remove(self, inter: discord.Interaction, name: str):
        cur = db.run("DELETE FROM categories WHERE guild_id=? AND lower(name)=lower(?)", (inter.guild_id, name))
        if cur.rowcount == 0:
            return await reply(inter, "No ticket type with that name.")
        await reply(inter, f"🗑️ Removed **{name}**. Panels updated.")
        await refresh_panels(inter.guild)

    @category.command(name="edit", description="Rename a ticket type or change its emoji/description")
    @app_commands.autocomplete(name=category_autocomplete)
    async def category_edit(self, inter: discord.Interaction, name: str,
                            new_name: Optional[app_commands.Range[str, 1, 50]] = None,
                            emoji: Optional[str] = None,
                            description: Optional[app_commands.Range[str, 1, 100]] = None):
        cat = db.one("SELECT * FROM categories WHERE guild_id=? AND lower(name)=lower(?)", (inter.guild_id, name))
        if not cat:
            return await reply(inter, "No ticket type with that name.")
        if emoji and not valid_emoji(emoji.strip()):
            return await reply(inter, "That doesn't look like an emoji.")
        db.run("UPDATE categories SET name=?, emoji=?, description=? WHERE id=?",
               (new_name or cat["name"], emoji.strip() if emoji else cat["emoji"],
                description or cat["description"], cat["id"]))
        await reply(inter, "✅ Updated. Panels refreshed.")
        await refresh_panels(inter.guild)

    @category.command(name="list", description="Show all ticket types")
    async def category_list(self, inter: discord.Interaction):
        cats = db.categories(inter.guild_id)
        text = "\n".join(f"{c['emoji'] or '•'} **{c['name']}** — {c['description'] or ''}" for c in cats) or \
            "No ticket types yet. Add one with `/category add` or reset to defaults with `/category reset`."
        await reply(inter, embed=discord.Embed(title="🎫 Ticket types", description=text, color=config.COLOR))

    @category.command(name="reset", description="Replace all ticket types with the defaults")
    async def category_reset(self, inter: discord.Interaction):
        db.run("DELETE FROM categories WHERE guild_id=?", (inter.guild_id,))
        db.seed_categories(inter.guild_id)
        await reply(inter, "♻️ Ticket types reset to defaults. Panels updated.")
        await refresh_panels(inter.guild)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
