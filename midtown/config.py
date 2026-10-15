"""All settings come from the .env file (see .env.example)."""

import os

from dotenv import load_dotenv

load_dotenv()


def _ids(value: str | None) -> set[int]:
    return {int(x) for x in (value or "").replace(" ", "").split(",") if x.isdigit()}


def _int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


TOKEN = os.getenv("DISCORD_TOKEN")

# Developer setup
DEV_IDS = _ids(os.getenv("DEV_IDS"))                    # your Discord user ID(s), comma separated
DEV_GUILD_ID = _int(os.getenv("DEV_GUILD_ID"))          # your own server, where /dev commands show up
BOT_LOG_CHANNEL_ID = _int(os.getenv("BOT_LOG_CHANNEL_ID"))  # channel for joins, leaves and errors

# Branding / links
BOT_NAME = os.getenv("BOT_NAME", "Midtown Desk")
SUPPORT_URL = os.getenv("SUPPORT_URL") or None          # invite link to your support server (optional)

DB_PATH = os.getenv("DB_PATH", "data/midtown_desk.db")
VERSION = "2.1.0"

# Feature modules loaded at startup (each file in cogs/)
EXTENSIONS = ["cogs.events", "cogs.general", "cogs.admin", "cogs.tickets", "cogs.dev"]

COLOR = 0x4F7CFF
GREEN = 0x3BA55C
RED = 0xED4245
YELLOW = 0xFAA61A
GREY = 0x2B2D31

DEFAULT_CATEGORIES = [
    ("General Support", "🎫", "Questions or help with anything"),
    ("Report a Player", "🚨", "Report someone breaking the rules"),
    ("Appeal", "⚖️", "Appeal a ban, mute or warning"),
    ("Partnership", "🤝", "Partner or collab with the server"),
    ("Other", "❓", "Anything that doesn't fit above"),
]
