# Midtown Desk

A ticket bot for Discord, written in Python (discord.py), with built-in AI from **Claude** and **ChatGPT**.

## 1. Create the bot

1. Go to https://discord.com/developers/applications and click **New Application**. Name it **Midtown Desk**.
2. Open the **Bot** tab, click **Reset Token**, and copy the token.
3. On the same page, turn on **Message Content Intent** (needed for transcripts and AI).
4. Open **OAuth2 > URL Generator**. Tick `bot` and `applications.commands`, then under permissions tick **Administrator**
   (or at minimum: Manage Channels, Manage Roles, View Channels, Send Messages, Embed Links, Attach Files,
   Read Message History, Manage Messages). Open the link and add the bot to your server.
5. In Server Settings > Roles, drag the bot's role **above** your staff role.

## 2. Get AI keys (optional)

Add one or both. If you add both, the bot uses your preferred one and switches to the other if it fails.

- **Claude:** https://console.anthropic.com > API Keys
- **ChatGPT:** https://platform.openai.com > API keys

These are **pay-as-you-go** and billed separately from Claude Pro / ChatGPT Plus subscriptions.
Add a small amount of credit and set a monthly spending limit in each dashboard.

## 3. Run it on your computer (for testing)

You need Python 3.10 or newer.

```
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your token and keys, then:

```
python bot.py
```

## 4. Host it on Wispbyte with GitHub

The code lives in your **private** GitHub repo. Your secrets (`.env`) and data (`midtown_desk.db`) are **never**
uploaded to GitHub. They only live on Wispbyte.

1. Create a Python server on Wispbyte.
2. **Get the code onto it.** In the server's **Startup** tab, look for a "Git Repo Address" (or similar) option:
   - **If it has one:** set it to `https://github.com/aidendixon602-png/midtown-desk`. Since the repo is private,
     also fill in your GitHub username and a **token** (GitHub > Settings > Developer settings > Fine-grained tokens >
     only this repo, *Contents: Read-only*). Turn on **Auto Update** so it pulls the latest code on every restart.
   - **If it doesn't:** download the repo as a ZIP from GitHub and upload the files in the **File Manager**.
3. Set the startup file to `bot.py` and the requirements file to `requirements.txt`.
4. In the **File Manager**, create a file named `.env` and paste in your filled-in `.env.example`.
5. Start the server. The console should say `Logged in as Midtown Desk`.

**Making changes:** edit the code on GitHub (or have Claude push the change), then **restart** the server on Wispbyte.
With Auto Update on, it pulls the new code automatically.

**Before you rely on it**, check on your plan that: files persist across restarts (or you'll lose ticket data),
the bot can reach the internet (needed for the AI), and Python 3.10+ is available.

## 5. Set it up in Discord

1. `/setup staff_role:@Staff` — creates a Tickets category and a private ticket-logs channel (or pick your own).
2. `/panel` in the channel where members should open tickets.
3. `/ai info` — paste your rules, FAQs and links so the AI gives accurate, server-specific answers.

## Commands

| Command | What it does |
|---|---|
| `/setup` | Pick the staff role, ticket category and log channel |
| `/panel` | Post the ticket panel (custom title and text supported) |
| `/category add / edit / remove / list / reset` | Make your own dropdown options. Panels update automatically |
| `/settings` | Tickets per user, auto-close hours, DM transcripts, ratings |
| `/stats` | Totals, average rating, staff leaderboard |
| `/ticket add / remove / rename / close` | Manage the ticket you're in |
| `/ask` | Ask the AI anything. Pick Claude or ChatGPT, or use the server default |
| `/ai settings` | Preferred AI, turn each AI feature on/off, `/ask` cooldown |
| `/ai info` | Teach the AI about your server |

Inside each ticket there are **Claim**, **Close** and (with AI) **Suggest reply** buttons.

## AI features

- **Quick answer:** when a ticket opens, the AI posts a helpful first reply while members wait for staff.
- **Close summary:** a short AI summary is added to the log next to the transcript.
- **Suggest reply:** staff-only button that drafts a reply privately for staff to edit and send.
- **`/ask`:** members can ask questions, with a cooldown so nobody runs up your bill. Staff skip the cooldown.
- **Automatic fallback:** if Claude or ChatGPT is down or out of credit, the other one answers.

The AI never decides bans, appeals or punishments. It's told to leave those to staff.

**Privacy:** AI features send ticket messages to Anthropic and/or OpenAI. Mention this in your server rules.
Turn off any feature you don't want with `/ai settings`.

**Models:** defaults are `claude-haiku-5-5` and `gpt-5.4-mini` (fast and cheap). Change `CLAUDE_MODEL` /
`OPENAI_MODEL` in `.env` to use a different one, using the model ID exactly as it appears in that provider's docs.
`/ai settings` shows which models are active.

## How tickets work

- Member picks a type from the dropdown, fills in a short form, and gets a private channel with staff.
- Staff hit **Claim** to take it. Everyone sees who's handling it.
- On close, an HTML transcript goes to the log channel and the member's DMs, and the member can rate 1–5 stars.
- Tickets with no messages for 48 hours (change with `/settings`) get a warning, then close an hour later.
- Data is saved in `midtown_desk.db` next to the bot. Keep that file if you move hosts.
