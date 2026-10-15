"""Upgrade persisted menus without resetting customized servers or ticket history."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["DB_PATH"] = ":memory:"

import discord
from cogs import events
from midtown import config, utils
from midtown.database import DB, LEGACY_CATEGORIES


def failure(cls=discord.Forbidden):
    return cls(SimpleNamespace(status=403, reason="Forbidden"), "Missing permissions")


class SupportMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.addCleanup(self.db.conn.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(utils, "db", self.db))
        self.stack.enter_context(patch.object(events, "db", self.db))

    def legacy_menu(self, gid=1):
        for i, values in enumerate(LEGACY_CATEGORIES):
            self.db.run("INSERT INTO categories(guild_id,name,emoji,description,position) VALUES(?,?,?,?,?)",
                        (gid, *values, i))

    def panel(self):
        self.db.run("INSERT INTO panels(message_id,guild_id,channel_id) VALUES(10,1,20)")
        guild = MagicMock()
        guild.id, guild.unavailable = 1, False
        channel = MagicMock(spec=discord.TextChannel)
        channel.get_partial_message.return_value.edit = AsyncMock()
        guild.get_channel.return_value = channel
        return guild, channel

    async def test_new_menus_have_support_categories(self):
        self.db.seed_categories(1)
        names = [r["name"] for r in self.db.categories(1)]
        self.assertEqual(names, ["General Support", "Bug Report", "Feature Request", "Partnership", "Other"])

    async def test_legacy_menu_migrates_without_changing_option_ids(self):
        self.legacy_menu()
        before = [r["id"] for r in self.db.categories(1)]
        self.db.migrate_default_categories()
        after = self.db.categories(1)
        self.assertEqual([r["id"] for r in after], before)
        self.assertEqual([r["name"] for r in after], [v[0] for v in config.DEFAULT_CATEGORIES])
        self.assertEqual(self.db.pending_category_refreshes(), [1])

    async def test_customized_menu_is_preserved(self):
        self.legacy_menu()
        self.db.run("UPDATE categories SET description='Contact our moderation team' WHERE name='Appeal'")
        before = [tuple(r) for r in self.db.categories(1)]
        self.db.migrate_default_categories()
        self.assertEqual([tuple(r) for r in self.db.categories(1)], before)
        self.assertEqual(self.db.pending_category_refreshes(), [])

    async def test_menu_with_an_added_category_is_preserved(self):
        self.legacy_menu()
        self.db.run("INSERT INTO categories(guild_id,name,position) VALUES(1,'Billing',5)")
        self.db.migrate_default_categories()
        self.assertIn("Report a Player", [r["name"] for r in self.db.categories(1)])
        self.assertEqual(self.db.pending_category_refreshes(), [])

    async def test_upgrade_does_not_rewrite_ticket_history(self):
        self.legacy_menu()
        self.db.run("INSERT INTO tickets(guild_id,channel_id,category,status) VALUES(1,30,'Appeal','closed')")
        self.db.migrate_default_categories()
        self.assertEqual(self.db.one("SELECT category FROM tickets")["category"], "Appeal")

    async def test_migration_is_idempotent(self):
        self.legacy_menu()
        self.db.migrate_default_categories()
        self.db.finish_category_refresh(1)
        self.db.migrate_default_categories()
        self.assertEqual(len(self.db.categories(1)), 5)
        self.assertEqual(self.db.pending_category_refreshes(), [])

    async def test_upgrade_and_pending_refresh_survive_database_reopen(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            path = str(Path(folder) / "test.db")
            old = DB(path)
            for i, values in enumerate(LEGACY_CATEGORIES):
                old.run("INSERT INTO categories(guild_id,name,emoji,description,position) VALUES(?,?,?,?,?)",
                        (1, *values, i))
            old.conn.close()
            upgraded = DB(path)
            self.assertIn("Bug Report", [r["name"] for r in upgraded.categories(1)])
            upgraded.conn.close()
            reopened = DB(path)
            try:
                self.assertEqual(reopened.pending_category_refreshes(), [1])
            finally:
                reopened.conn.close()

    async def test_worker_retries_failed_panel_updates(self):
        self.legacy_menu()
        self.db.migrate_default_categories()
        cog = object.__new__(events.Events)
        cog.bot = MagicMock()
        cog.bot.get_guild.return_value.unavailable = False
        with patch.object(events, "refresh_panels", new=AsyncMock(return_value=False)):
            await events.Events.panel_updates.coro(cog)
        self.assertEqual(self.db.pending_category_refreshes(), [1])
        with patch.object(events, "refresh_panels", new=AsyncMock(return_value=True)):
            await events.Events.panel_updates.coro(cog)
        self.assertEqual(self.db.pending_category_refreshes(), [])

    async def test_existing_message_receives_new_dropdown(self):
        self.legacy_menu()
        self.db.migrate_default_categories()
        guild, channel = self.panel()
        self.assertTrue(await utils.refresh_panels(guild))
        view = channel.get_partial_message.return_value.edit.call_args.kwargs["view"]
        labels = [o.label for o in view.children[0].options]
        self.assertIn("Bug Report", labels)
        self.assertIn("Feature Request", labels)
        self.assertNotIn("Report a Player", labels)

    async def test_uncached_panel_channel_is_fetched(self):
        self.db.seed_categories(1)
        guild, channel = self.panel()
        guild.get_channel.return_value = None
        guild.fetch_channel = AsyncMock(return_value=channel)
        self.assertTrue(await utils.refresh_panels(guild))
        guild.fetch_channel.assert_awaited_once_with(20)
        channel.get_partial_message.return_value.edit.assert_awaited_once()

    async def test_permission_failure_preserves_panel_for_retry(self):
        guild, channel = self.panel()
        channel.get_partial_message.return_value.edit.side_effect = failure()
        self.assertFalse(await utils.refresh_panels(guild))
        self.assertIsNotNone(self.db.one("SELECT * FROM panels WHERE message_id=10"))

    async def test_fetch_failure_preserves_uncached_panel(self):
        guild, _ = self.panel()
        guild.get_channel.return_value = None
        guild.fetch_channel = AsyncMock(side_effect=failure())
        self.assertFalse(await utils.refresh_panels(guild))
        self.assertIsNotNone(self.db.one("SELECT * FROM panels WHERE message_id=10"))

    async def test_confirmed_deleted_message_is_removed_from_panel_registry(self):
        guild, channel = self.panel()
        channel.get_partial_message.return_value.edit.side_effect = failure(discord.NotFound)
        self.assertTrue(await utils.refresh_panels(guild))
        self.assertIsNone(self.db.one("SELECT * FROM panels WHERE message_id=10"))


if __name__ == "__main__":
    unittest.main()
