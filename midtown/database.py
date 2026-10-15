"""SQLite storage. Every server's settings, ticket types, panels and tickets are kept separate by guild_id."""

import datetime as dt
import os
import sqlite3

from . import config

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
CREATE TABLE IF NOT EXISTS blacklist (
    kind       TEXT,            -- 'user' or 'guild'
    target_id  INTEGER,
    reason     TEXT,
    added_by   INTEGER,
    added_at   TEXT,
    PRIMARY KEY (kind, target_id)
);
CREATE TABLE IF NOT EXISTS guild_blocks (
    guild_id   INTEGER,
    user_id    INTEGER,
    reason     TEXT,
    added_by   INTEGER,
    added_at   TEXT,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS authorized_guilds (
    guild_id       INTEGER PRIMARY KEY,
    authorized_by  INTEGER,
    authorized_at  TEXT,
    note           TEXT
);
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets(guild_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_categories_guild ON categories(guild_id);
"""

# Columns added after v2.0. Existing databases get them automatically.
MIGRATIONS = {
    "tickets": {"welcome_message_id": "INTEGER", "priority": "TEXT DEFAULT 'normal'"},
    "guilds": {"weekly_report": "INTEGER DEFAULT 1", "last_report_at": "TEXT"},
}

CONFIG_COLUMNS = {"staff_role_id", "category_id", "log_channel_id", "ticket_limit",
                  "autoclose_hours", "dm_transcripts", "ask_rating", "weekly_report"}


class DB:
    def __init__(self, path: str):
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in columns.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()

    # ---- raw helpers ----
    def all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def backup_to(self, dest: str):
        target = sqlite3.connect(dest)
        with target:
            self.conn.backup(target)
        target.close()

    # ---- server settings ----
    def config(self, guild_id: int) -> sqlite3.Row:
        row = self.one("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
        if row is None:
            self.run("INSERT INTO guilds(guild_id) VALUES(?)", (guild_id,))
            row = self.one("SELECT * FROM guilds WHERE guild_id=?", (guild_id,))
        return row

    def update_config(self, guild_id: int, **values) -> bool:
        values = {k: v for k, v in values.items() if v is not None and k in CONFIG_COLUMNS}
        self.config(guild_id)
        if values:
            self.run(f"UPDATE guilds SET {', '.join(f'{k}=?' for k in values)} WHERE guild_id=?",
                     (*values.values(), guild_id))
        return bool(values)

    def next_ticket_number(self, guild_id: int) -> int:
        self.config(guild_id)
        self.run("UPDATE guilds SET counter=counter+1 WHERE guild_id=?", (guild_id,))
        return self.config(guild_id)["counter"]

    def delete_guild_data(self, guild_id: int):
        for table in ("guilds", "categories", "panels", "tickets", "guild_blocks"):
            self.conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
        self.conn.commit()

    # ---- ticket types ----
    def categories(self, guild_id: int):
        return self.all("SELECT * FROM categories WHERE guild_id=? ORDER BY position, id", (guild_id,))

    def seed_categories(self, guild_id: int):
        for i, (name, emoji, desc) in enumerate(config.DEFAULT_CATEGORIES):
            self.conn.execute(
                "INSERT INTO categories(guild_id, name, emoji, description, position) VALUES(?,?,?,?,?)",
                (guild_id, name, emoji, desc, i))
        self.conn.commit()

    # ---- tickets ----
    def open_ticket(self, channel_id: int):
        return self.one("SELECT * FROM tickets WHERE channel_id=? AND status='open'", (channel_id,))

    def open_tickets_for(self, guild_id: int, user_id: int):
        return self.all("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status IN ('open','closing')",
                        (guild_id, user_id))

    # ---- blacklist ----
    def blacklisted(self, kind: str, target_id: int):
        return self.one("SELECT * FROM blacklist WHERE kind=? AND target_id=?", (kind, target_id))

    def blacklist_add(self, kind: str, target_id: int, reason: str | None, added_by: int):
        self.run("INSERT OR REPLACE INTO blacklist(kind, target_id, reason, added_by, added_at) VALUES(?,?,?,?,?)",
                 (kind, target_id, reason, added_by, dt.datetime.now(dt.timezone.utc).isoformat()))

    def blacklist_remove(self, kind: str, target_id: int) -> bool:
        return self.run("DELETE FROM blacklist WHERE kind=? AND target_id=?", (kind, target_id)).rowcount > 0

    def blacklist_all(self):
        return self.all("SELECT * FROM blacklist ORDER BY added_at DESC")

    # ---- per-server ticket blocks (set by that server's staff) ----
    def guild_block(self, guild_id: int, user_id: int):
        return self.one("SELECT * FROM guild_blocks WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def guild_block_add(self, guild_id: int, user_id: int, reason: str | None, added_by: int):
        self.run("INSERT OR REPLACE INTO guild_blocks(guild_id, user_id, reason, added_by, added_at) VALUES(?,?,?,?,?)",
                 (guild_id, user_id, reason, added_by, dt.datetime.now(dt.timezone.utc).isoformat()))

    def guild_block_remove(self, guild_id: int, user_id: int) -> bool:
        return self.run("DELETE FROM guild_blocks WHERE guild_id=? AND user_id=?", (guild_id, user_id)).rowcount > 0

    def guild_blocks(self, guild_id: int):
        return self.all("SELECT * FROM guild_blocks WHERE guild_id=? ORDER BY added_at DESC", (guild_id,))

    # ---- server authorization ----
    def is_authorized(self, guild_id: int) -> bool:
        return self.one("SELECT 1 FROM authorized_guilds WHERE guild_id=?", (guild_id,)) is not None

    def authorize(self, guild_id: int, by: int, note: str | None = None):
        self.run("INSERT OR REPLACE INTO authorized_guilds(guild_id, authorized_by, authorized_at, note) VALUES(?,?,?,?)",
                 (guild_id, by, dt.datetime.now(dt.timezone.utc).isoformat(), note))

    def deauthorize(self, guild_id: int) -> bool:
        return self.run("DELETE FROM authorized_guilds WHERE guild_id=?", (guild_id,)).rowcount > 0

    def authorized_all(self):
        return self.all("SELECT * FROM authorized_guilds ORDER BY authorized_at DESC")

    # ---- bot-wide state (maintenance mode, custom status) ----
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM bot_state WHERE key=?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None):
        if value is None:
            self.run("DELETE FROM bot_state WHERE key=?", (key,))
        else:
            self.run("INSERT OR REPLACE INTO bot_state(key, value) VALUES(?,?)", (key, value))


db = DB(config.DB_PATH)
