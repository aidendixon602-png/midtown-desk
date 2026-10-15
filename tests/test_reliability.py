"""Offline regression tests: real command loading, isolated SQLite and mocked Discord I/O."""
import asyncio
from contextlib import ExitStack
import datetime as dt
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DB_PATH"] = ":memory:"

import discord
from discord import app_commands
import main
from cogs import admin, auth, owner, reports, tickets
from midtown import config, tickets as core, utils
from midtown.database import DB


def http_error(cls=discord.Forbidden):
    return cls(SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions")


def interaction():
    inter = MagicMock()
    inter.guild_id, inter.channel_id, inter.user.id = 1, 10, 20
    inter.guild.id, inter.guild.name = 1, "Test server"
    inter.guild.unavailable = False
    inter.guild.get_member.return_value = None
    inter.guild.get_role.return_value.is_default.return_value = False
    inter.guild.get_role.return_value.managed = False
    inter.user.guild_permissions.manage_guild = False
    inter.user.roles = []
    inter.permissions = discord.Permissions.none()
    inter.type = discord.InteractionType.component
    inter.command = None
    inter.response.is_done.return_value = False
    for attr in ("send_message", "defer", "edit_message", "send_modal"):
        setattr(inter.response, attr, AsyncMock())
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.channel.id = 10
    inter.channel.send, inter.channel.delete = AsyncMock(), AsyncMock()
    inter.channel.set_permissions = AsyncMock()
    inter.client.fetch_user = AsyncMock(side_effect=http_error())
    return inter


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.db.conn.close)
        for module in (core, utils, admin, auth, owner, reports, tickets):
            self.stack.enter_context(patch.object(module, "db", self.db))
        self.stack.enter_context(patch.object(config, "DEV_IDS", set()))
        self.stack.enter_context(patch.object(utils, "OWNER_IDS", set()))
        self.db.authorize(1, 99)
        self.db.update_config(1, staff_role_id=30, log_channel_id=40)
        self.db.seed_categories(1)
        core.open_channels.clear()
        core._last_opened.clear()

    def ticket(self, status="open"):
        now = utils.utcnow().isoformat()
        self.db.run("INSERT INTO tickets(guild_id,number,channel_id,user_id,category,subject,status,opened_at,last_activity) "
                    "VALUES(1,1,10,20,'Support','Help',?,?,?)", (status, now, now))
        core.open_channels.add(10)
        return self.db.one("SELECT * FROM tickets WHERE channel_id=10")

    def cog(self):
        cog = object.__new__(tickets.Tickets)
        cog.bot = MagicMock()
        return cog

    async def test_all_extensions_and_command_schemas_load(self):
        async with main.MidtownBot() as bot:
            bot.tree.sync = AsyncMock(return_value=[])
            await bot.setup_hook()
            self.assertEqual(set(bot.extensions), set(config.EXTENSIONS))
            for command in bot.tree.get_commands():
                self.assertTrue(command.to_dict(bot.tree)["name"])
            self.assertEqual(len(bot.tree.get_commands()), 11)

    async def test_sync_failure_does_not_prevent_connecting(self):
        async with main.MidtownBot() as bot:
            bot.tree.sync = AsyncMock(side_effect=http_error())
            with self.assertLogs("midtown", level="ERROR"):
                await bot.setup_hook()
            self.assertEqual(len(bot.extensions), len(config.EXTENSIONS))
            self.assertEqual(bot.synced, (0, 0))

    async def test_startup_explains_disabled_privileged_intent(self):
        with patch.object(config, "TOKEN", "dummy"), patch.object(main, "banner"), \
             patch.object(main, "setup_logging"), patch.object(main, "MidtownBot") as bot:
            bot.return_value.run.side_effect = discord.PrivilegedIntentsRequired(0)
            with self.assertRaisesRegex(SystemExit, "Message Content Intent"):
                main.main()

    async def test_startup_explains_invalid_token(self):
        with patch.object(config, "TOKEN", "dummy"), patch.object(main, "banner"), \
             patch.object(main, "setup_logging"), patch.object(main, "MidtownBot") as bot:
            bot.return_value.run.side_effect = discord.LoginFailure("bad token")
            with self.assertRaisesRegex(SystemExit, "Discord rejected DISCORD_TOKEN"):
                main.main()

    async def test_reload_cancels_owner_discovery_and_registration(self):
        bot = MagicMock()
        # Keep discovery and registration waiting until their cogs are unloaded.
        bot.wait_until_ready = asyncio.Event().wait
        auth_cog, owner_cog = auth.Auth(bot), owner.Owner(bot)
        await auth_cog.cog_load()
        await owner_cog.cog_load()
        await asyncio.sleep(0)
        auth_cog.cog_unload()
        await owner_cog.cog_unload()
        await asyncio.gather(auth_cog.owner_task, owner_cog.registration_task, return_exceptions=True)
        self.assertTrue(auth_cog.owner_task.cancelled())
        self.assertTrue(owner_cog.registration_task.cancelled())

    async def test_permission_error_gets_a_response(self):
        inter = interaction()
        await main.MidtownTree.on_error(None, inter, app_commands.MissingPermissions(["manage_guild"]))
        inter.response.send_message.assert_awaited_once()

    async def test_cooldown_error_gets_a_response(self):
        inter = interaction()
        await main.MidtownTree.on_error(None, inter, app_commands.CommandOnCooldown(app_commands.Cooldown(1, 60), 25))
        self.assertIn("25", inter.response.send_message.call_args.args[0])

    async def test_ticket_listener_does_not_handle_owner_approval(self):
        inter = interaction()
        inter.data = {"custom_id": "md:auth:1:yes"}
        with patch.object(tickets, "gatekeep", new=AsyncMock(return_value=False)) as gate:
            await self.cog().on_interaction(inter)
            gate.assert_not_awaited()
        inter.response.send_message.assert_not_awaited()

    async def test_approval_acknowledged_before_network_work(self):
        inter = interaction()
        inter.data = {"custom_id": "md:auth:1:yes"}
        cog = auth.Auth(MagicMock())
        async def approve(*args):
            inter.response.defer.assert_awaited_once()
        cog.approve = AsyncMock(side_effect=approve)
        with patch.object(auth, "is_dev", return_value=True):
            await cog.on_interaction(inter)
        inter.edit_original_response.assert_awaited_once()

    async def test_admin_permission_checked_at_runtime(self):
        inter = interaction()
        with self.assertRaises(app_commands.MissingPermissions):
            await admin.Admin(MagicMock()).interaction_check(inter)
        inter.permissions.manage_guild = True
        self.assertTrue(await admin.Admin(MagicMock()).interaction_check(inter))

    async def test_settings_rejects_everyone_as_staff(self):
        inter = interaction()
        role = MagicMock()
        role.is_default.return_value = True
        await admin.Admin.settings_cmd.callback(admin.Admin(MagicMock()), inter, staff_role=role)
        self.assertEqual(self.db.config(1)["staff_role_id"], 30)

    async def test_panel_acknowledged_before_sending(self):
        inter = interaction()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 40
        async def send(**kwargs):
            inter.response.defer.assert_awaited_once()
            return SimpleNamespace(id=123)
        channel.send = AsyncMock(side_effect=send)
        await admin.Admin.panel_cmd.callback(admin.Admin(MagicMock()), inter, channel=channel)
        self.assertIsNotNone(self.db.one("SELECT * FROM panels WHERE message_id=123"))

    async def test_setup_retry_reuses_resources_after_partial_failure(self):
        inter = interaction()
        self.db.update_config(1, staff_role_id=999)
        role = MagicMock()
        role.id = 30
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = 50
        inter.guild.get_role.side_effect = lambda rid: role if rid == 30 else None
        inter.guild.get_channel.side_effect = lambda cid: category if cid == 50 else None
        inter.guild.create_role = AsyncMock(return_value=role)
        inter.guild.create_category = AsyncMock(return_value=category)
        inter.guild.create_text_channel = AsyncMock(side_effect=http_error())
        cog = admin.Admin(MagicMock())
        with patch.object(admin, "missing_perms", return_value=[]):
            for _ in range(2):
                with self.assertRaises(discord.Forbidden):
                    await admin.Admin.setup_cmd.callback(cog, inter)
        inter.guild.create_role.assert_awaited_once()
        inter.guild.create_category.assert_awaited_once()

    async def test_deferred_gatekeep_explains_denial(self):
        inter = interaction()
        inter.response.is_done.return_value = True
        self.db.deauthorize(1)
        self.assertFalse(await utils.gatekeep(inter))
        inter.followup.send.assert_awaited_once()

    async def test_open_form_rechecks_server_authorization(self):
        inter = interaction()
        modal = tickets.OpenTicketModal(self.db.categories(1)[0], None)
        self.db.deauthorize(1)
        with patch.object(tickets, "create_ticket", new=AsyncMock()) as create:
            await modal.on_submit(inter)
            create.assert_not_awaited()

    async def test_open_form_rechecks_ticket_block(self):
        inter = interaction()
        modal = tickets.OpenTicketModal(self.db.categories(1)[0], None)
        self.db.guild_block_add(1, 20, "blocked", 99)
        with patch.object(tickets, "create_ticket", new=AsyncMock()) as create:
            await modal.on_submit(inter)
            create.assert_not_awaited()

    async def test_open_form_rechecks_removed_category(self):
        inter = interaction()
        modal = tickets.OpenTicketModal(self.db.categories(1)[0], None)
        self.db.run("DELETE FROM categories")
        with patch.object(tickets, "create_ticket", new=AsyncMock()) as create:
            await modal.on_submit(inter)
            create.assert_not_awaited()

    async def test_open_form_rechecks_cooldown(self):
        inter = interaction()
        modal = tickets.OpenTicketModal(self.db.categories(1)[0], None)
        core.start_cooldown(1, 20)
        with patch.object(tickets, "create_ticket", new=AsyncMock()) as create:
            await modal.on_submit(inter)
            create.assert_not_awaited()

    async def test_concurrent_submissions_create_one_ticket(self):
        first, second = interaction(), interaction()
        cat = self.db.categories(1)[0]
        async def create(*args):
            await asyncio.sleep(0)
            self.ticket()
            return first.channel
        with patch.object(tickets, "create_ticket", new=AsyncMock(side_effect=create)) as create_mock:
            await asyncio.gather(tickets.OpenTicketModal(cat, None).on_submit(first),
                                 tickets.OpenTicketModal(cat, None).on_submit(second))
        self.assertEqual(create_mock.await_count, 1)

    async def test_closing_ticket_still_counts_towards_limit(self):
        self.ticket("closing")
        self.assertEqual(len(self.db.open_tickets_for(1, 20)), 1)

    async def test_failed_welcome_rolls_back_channel_and_record(self):
        inter = interaction()
        inter.guild.create_text_channel = AsyncMock(return_value=inter.channel)
        inter.channel.send.side_effect = http_error()
        with self.assertRaises(discord.Forbidden):
            await core.create_ticket(inter, self.db.categories(1)[0], "Help", "Details")
        inter.channel.delete.assert_awaited_once()
        self.assertEqual(self.db.all("SELECT * FROM tickets"), [])
        self.assertNotIn(10, core.open_channels)

    async def test_failed_welcome_and_cleanup_keeps_channel_manageable(self):
        inter = interaction()
        inter.guild.create_text_channel = AsyncMock(return_value=inter.channel)
        inter.channel.send.side_effect = http_error()
        inter.channel.delete.side_effect = http_error()
        with self.assertLogs("midtown", level="ERROR"):
            result = await core.create_ticket(inter, self.db.categories(1)[0], "Help", "Details")
        self.assertIs(result, inter.channel)
        self.assertIsNotNone(self.db.open_ticket(10))
        self.assertIn(10, core.open_channels)
        self.assertGreater(core.cooldown_left(1, 20), 0)

    async def test_close_modal_rechecks_permissions(self):
        inter = interaction()
        inter.user.id = 999
        self.ticket()
        with patch.object(tickets, "close_ticket", new=AsyncMock()) as close:
            await tickets.CloseModal().on_submit(inter)
            close.assert_not_awaited()

    async def test_stale_claim_cannot_overwrite_another_staff_member(self):
        inter = interaction()
        ticket = self.ticket()
        self.db.run("UPDATE tickets SET claimed_by=55")
        self.assertFalse(await self.cog().set_claim(inter, ticket, inter.user))
        self.assertEqual(self.db.open_ticket(10)["claimed_by"], 55)
        inter.channel.send.assert_not_awaited()

    async def test_rating_rejects_out_of_range(self):
        inter = interaction()
        ticket = self.ticket("closed")
        await self.cog().handle_rate(inter, f"md:rate:{ticket['id']}:99")
        self.assertIsNone(self.db.one("SELECT rating FROM tickets")["rating"])

    async def test_rating_rejects_open_ticket(self):
        inter = interaction()
        ticket = self.ticket()
        await self.cog().handle_rate(inter, f"md:rate:{ticket['id']}:5")
        self.assertIsNone(self.db.open_ticket(10)["rating"])

    async def test_rating_cannot_be_changed_after_submission(self):
        inter = interaction()
        ticket = self.ticket("closed")
        cog = self.cog()
        cog.bot.get_guild.return_value = None
        await cog.handle_rate(inter, f"md:rate:{ticket['id']}:5")
        await cog.handle_rate(inter, f"md:rate:{ticket['id']}:1")
        self.assertEqual(self.db.one("SELECT rating FROM tickets")["rating"], 5)

    async def close_with(self, inter, ticket, transcript=None, archived=True):
        with patch.object(core, "build_transcript", new=AsyncMock(return_value=(b"html", 1), side_effect=transcript)), \
             patch.object(core, "send_guild_log", new=AsyncMock(return_value=archived)), \
             patch.object(core.asyncio, "sleep", new=AsyncMock()):
            return await core.close_ticket(inter.client, inter.guild, inter.channel, ticket, inter.user, "Solved")

    async def test_delete_failure_restores_open_ticket(self):
        inter = interaction()
        ticket = self.ticket()
        inter.channel.delete.side_effect = http_error()
        with self.assertRaises(discord.Forbidden):
            await self.close_with(inter, ticket)
        self.assertIsNotNone(self.db.open_ticket(10))
        self.assertIn(10, core.open_channels)
        self.assertIsNone(self.db.open_ticket(10)["closed_at"])

    async def test_transcript_failure_preserves_channel(self):
        inter = interaction()
        ticket = self.ticket()
        with self.assertRaises(discord.Forbidden):
            await self.close_with(inter, ticket, transcript=http_error())
        inter.channel.delete.assert_not_awaited()
        self.assertIsNotNone(self.db.open_ticket(10))

    async def test_cancelled_close_restores_open_state(self):
        inter = interaction()
        ticket = self.ticket()
        with self.assertRaises(asyncio.CancelledError):
            await self.close_with(inter, ticket, transcript=asyncio.CancelledError())
        self.assertIsNotNone(self.db.open_ticket(10))
        self.assertIn(10, core.open_channels)

    async def test_already_deleted_channel_finishes_close(self):
        inter = interaction()
        ticket = self.ticket()
        inter.channel.delete.side_effect = http_error(discord.NotFound)
        self.assertTrue(await self.close_with(inter, ticket))
        self.assertIsNone(self.db.open_ticket(10))

    async def test_dm_archive_allows_close_when_log_is_unavailable(self):
        inter = interaction()
        ticket = self.ticket()
        user = MagicMock()
        user.send = AsyncMock()
        user.send.return_value.edit = AsyncMock()
        inter.guild.get_member.return_value = user
        self.assertTrue(await self.close_with(inter, ticket, archived=False))
        self.assertIn("file", user.send.call_args.kwargs)
        self.assertIn("view", user.send.return_value.edit.call_args.kwargs)

    async def test_archive_failure_preserves_channel(self):
        inter = interaction()
        ticket = self.ticket()
        with self.assertRaises(RuntimeError):
            await self.close_with(inter, ticket, archived=False)
        inter.channel.delete.assert_not_awaited()
        self.assertIsNotNone(self.db.open_ticket(10))

    async def test_successful_close_commits_after_deletion(self):
        inter = interaction()
        ticket = self.ticket()
        async def delete(**kwargs):
            self.assertEqual(self.db.one("SELECT status FROM tickets")["status"], "closing")
        inter.channel.delete.side_effect = delete
        self.assertTrue(await self.close_with(inter, ticket))
        self.assertEqual(self.db.one("SELECT status FROM tickets")["status"], "closed")
        self.assertNotIn(10, core.open_channels)

    async def test_weekly_report_failure_is_retried(self):
        old = (utils.utcnow() - dt.timedelta(days=8)).isoformat()
        self.db.run("UPDATE guilds SET last_report_at=?", (old,))
        cog = object.__new__(reports.Reports)
        cog.bot = MagicMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock(side_effect=http_error())
        cog.bot.get_guild.return_value.id = 1
        cog.bot.get_guild.return_value.get_channel.return_value = channel
        await reports.Reports.weekly.coro(cog)
        self.assertEqual(self.db.config(1)["last_report_at"], old)

    async def test_cache_miss_does_not_close_existing_ticket(self):
        self.ticket()
        cog = self.cog()
        guild = cog.bot.get_guild.return_value
        guild.unavailable = False
        guild.get_channel.return_value = None
        guild.fetch_channel = AsyncMock(side_effect=http_error())
        await tickets.Tickets.autoclose.coro(cog)
        self.assertIsNotNone(self.db.open_ticket(10))


if __name__ == "__main__":
    unittest.main()
