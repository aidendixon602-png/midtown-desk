"""Pretty console output: startup banner, colored log lines, ready summary."""

import logging
import os
import sys

from . import config

COLOR = not os.getenv("NO_COLOR")


def _c(code: str) -> str:
    return f"\033[{code}m" if COLOR else ""


RESET, BOLD, DIM = _c("0"), _c("1"), _c("2")
BLUE, CYAN, GREEN, YELLOW, RED, MAGENTA = _c("38;5;69"), _c("36"), _c("32"), _c("33"), _c("31"), _c("35")

LEVELS = {
    logging.DEBUG: (DIM, "·", "DEBUG"),
    logging.INFO: (CYAN, "●", "INFO "),
    logging.WARNING: (YELLOW, "▲", "WARN "),
    logging.ERROR: (RED, "✖", "ERROR"),
    logging.CRITICAL: (RED + BOLD, "✖", "FATAL"),
}

# Log lines that are just noise for a ticket bot
NOISE = (
    "voice will NOT be supported",
    "logging in using static token",
)


class NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(n in message for n in NOISE)


class ColorFormatter(logging.Formatter):
    """`● INFO  gateway   Connected` — the host already adds the time, so we skip it."""

    def format(self, record: logging.LogRecord) -> str:
        color, icon, label = LEVELS.get(record.levelno, LEVELS[logging.INFO])
        source = record.name.split(".")[-1][:9]
        line = f"{color}{icon} {label}{RESET} {DIM}{source:<9}{RESET} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + RED + self.formatException(record.exc_info) + RESET
        return line


def banner():
    title = f"🎫  {config.BOT_NAME}"
    sub = f"v{config.VERSION} · discord ticket bot"
    width = 44
    print(f"""
{BLUE}{BOLD}╭{'─' * width}╮
│{title.center(width - 1)}│
│{RESET}{DIM}{sub.center(width)}{RESET}{BLUE}{BOLD}│
╰{'─' * width}╯{RESET}
""", flush=True)


def ready_box(rows: list[tuple[str, str]], warnings: list[str] | None = None):
    """Print a summary box once the bot is online."""
    key_width = max(len(k) for k, _ in rows)
    lines = [f"{GREEN}{BOLD}✔ Online and ready{RESET}", ""]
    lines += [f"  {DIM}{k.ljust(key_width)}{RESET}  {v}" for k, v in rows]
    for w in warnings or []:
        lines.append(f"  {YELLOW}▲ {w}{RESET}")
    bar = f"{GREEN}{'─' * 48}{RESET}"
    print("\n".join(["", bar, *lines, bar, ""]), flush=True)


def setup_console_logging(level: int = logging.INFO) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter())
    handler.addFilter(NoiseFilter())
    handler.setLevel(level)
    return handler
