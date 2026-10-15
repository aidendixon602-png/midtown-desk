"""
AI helpers for Midtown Desk: Claude (Anthropic) + ChatGPT (OpenAI).

Both are optional. Add ANTHROPIC_API_KEY and/or OPENAI_API_KEY to your .env.
If the preferred provider fails or isn't set up, the other one is used automatically.
"""

import logging
import os
import time

log = logging.getLogger("midtown-desk.ai")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY") or None
OPENAI_KEY = os.getenv("OPENAI_API_KEY") or None
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
MAX_OUTPUT = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "1000"))

PROVIDERS = {"claude": "Claude", "gpt": "ChatGPT"}

_claude = None
_openai = None
if ANTHROPIC_KEY:
    from anthropic import AsyncAnthropic
    _claude = AsyncAnthropic(api_key=ANTHROPIC_KEY, timeout=60)
if OPENAI_KEY:
    from openai import AsyncOpenAI
    _openai = AsyncOpenAI(api_key=OPENAI_KEY, timeout=60)


class AIError(Exception):
    pass


def available() -> list[str]:
    return [name for name, client in (("claude", _claude), ("gpt", _openai)) if client]


def model_name(provider: str) -> str:
    return CLAUDE_MODEL if provider == "claude" else OPENAI_MODEL


async def _ask_claude(system: str, prompt: str, max_tokens: int) -> str:
    resp = await _claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


async def _ask_gpt(system: str, prompt: str, max_tokens: int) -> str:
    resp = await _openai.responses.create(
        model=OPENAI_MODEL,
        instructions=system,
        input=prompt,
        max_output_tokens=max_tokens,
    )
    return (resp.output_text or "").strip()


async def ask(prompt: str, system: str, preferred: str = "claude", max_tokens: int = MAX_OUTPUT) -> tuple[str, str]:
    """Returns (text, provider_used). Tries the preferred provider first, then the other one."""
    order = [preferred] + [p for p in PROVIDERS if p != preferred]
    errors = []
    for provider in order:
        if provider not in available():
            continue
        try:
            fn = _ask_claude if provider == "claude" else _ask_gpt
            text = await fn(system, prompt, max_tokens)
            if text:
                return text, provider
            errors.append(f"{PROVIDERS[provider]}: empty reply")
        except Exception as e:  # network errors, rate limits, bad keys, etc.
            log.warning("%s request failed: %s", PROVIDERS[provider], e)
            errors.append(f"{PROVIDERS[provider]}: {type(e).__name__}")
    if not available():
        raise AIError("No AI is set up. Add ANTHROPIC_API_KEY and/or OPENAI_API_KEY to the bot's .env file.")
    raise AIError("The AI couldn't answer right now (" + "; ".join(errors) + ").")


# ---- prompts ----------------------------------------------------------------------------

def system_prompt(server_name: str, server_info: str | None, role: str) -> str:
    info = f"\n\nInformation about this server, written by its staff (trust this over your own guesses):\n{server_info}" \
        if server_info else ""
    return (
        f"You are Midtown Desk, the support assistant for the Discord server \"{server_name}\". {role}\n"
        "Rules:\n"
        "- Be friendly, clear and short. Write for Discord: plain sentences, simple bullet points if helpful, no headings.\n"
        "- Never promise outcomes like unbans, refunds, rank changes or punishments. Staff decide those.\n"
        "- If you don't know something specific to this server, say a staff member will confirm. Never make up rules, links or prices.\n"
        "- Never ask for passwords, payment details or other private info."
        + info
    )


def first_reply_prompt(category: str, subject: str, details: str) -> str:
    return (
        f"A member just opened a \"{category}\" ticket.\n"
        f"Subject: {subject}\nDetails: {details or '(none given)'}\n\n"
        "Write a helpful first reply (under 120 words). If you can answer or point them in the right direction, do so. "
        "If it needs staff (reports, appeals, anything account-specific), say what info would help staff and that someone "
        "will be with them soon."
    )


def summary_prompt(conversation: str) -> str:
    return (
        "Summarize this support ticket for the staff log in 2-4 short bullet points: the issue, what was done, "
        "and the outcome. Under 80 words.\n\n" + conversation
    )


def suggest_prompt(conversation: str) -> str:
    return (
        "Here is a support ticket so far. Draft the next reply a staff member could send to the member. "
        "Only output the reply itself, ready to paste, under 120 words.\n\n" + conversation
    )


# ---- cooldowns ----------------------------------------------------------------------------

_last_used: dict[tuple[int, int], float] = {}


def cooldown_left(guild_id: int, user_id: int, seconds: int) -> int:
    """Returns seconds remaining, or 0 (and starts the cooldown) if the user can go."""
    key = (guild_id, user_id)
    now = time.monotonic()
    left = seconds - (now - _last_used.get(key, 0))
    if left > 0:
        return int(left) + 1
    _last_used[key] = now
    return 0
