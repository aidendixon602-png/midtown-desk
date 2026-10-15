# Midtown Desk

A multi-server ticket bot for Discord, written in Python (discord.py). Any server can add it,
and each server gets its own settings, ticket types and ticket history.

## Features

- Ticket panel with a dropdown of ticket types (defaults included, fully customizable per server)
- Pop-up form so members explain their issue up front
- Private ticket channels, **Claim** and **Close** buttons
- HTML transcripts to the server's log channel and the member's DMs
- 1–5 star ratings and a staff leaderboard
- Open-ticket limit per member, spam cooldown, and auto-close for inactive tickets
- Claim, unclaim and transfer tickets; close requests the member confirms
- Ticket history per member and per-server ticket blocks
- Welcome message with setup steps when added to a new server
- Developer tools: bot-wide stats, server list, blacklist, maintenance mode, broadcast, hot reload, backups

## Project layout

```
main.py              Starts the bot
midtown/
  config.py          Settings from .env
  database.py        SQLite storage (per-server data)
  utils.py           Shared helpers, permission checks, error reporting
  tickets.py         Opening and closing tickets
  transcripts.py     HTML transcripts
cogs/
  admin.py           /setup /panel /settings /stats /category
  tickets.py         Ticket buttons, /ticket, auto-close
  general.py         /help /ping /invite /about
  events.py          Server joins/leaves, status
  dev.py             /dev (developer only)
```

To add a feature, create a new file in `cogs/` and add it to `EXTENSIONS` in `midtown/config.py`.

## 1. Create the bot

1. Go to https://discord.com/developers/applications and click **New Application**. Name it **Midtown Desk**.
2. **Bot** tab: click **Reset Token** and copy it. Turn on **Message Content Intent** (needed for transcripts).
   Leave **Public Bot** on so other servers can add it.
3. **Installation** tab (or OAuth2 > URL Generator): scopes `bot` + `applications.commands`, with these permissions:
   View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Channels,
   Manage Roles, Manage Messages, Use External Emojis. Once the bot is running, `/invite` gives you this link.

## 2. Fill in `.env`

Copy `.env.example` to `.env`:

| Setting | What to put |
|---|---|
| `DISCORD_TOKEN` | Your bot token |
| `DEV_IDS` | Your Discord user ID (Settings > Advanced > Developer Mode, then right-click your name > Copy User ID). Comma-separate teammates |
| `DEV_GUILD_ID` | Your own server's ID. `/dev` commands only appear there |
| `BOT_LOG_CHANNEL_ID` | A private channel in your server for join/leave alerts and error reports |
| `SUPPORT_URL` | Optional invite link to your support server |

## 3. Run it

Python 3.10+:

```
pip install -r requirements.txt
python main.py
```

## 4. Host it on Wispbyte with GitHub

The code lives in GitHub. Your `.env`, database (`data/`) and logs (`logs/`) are never uploaded there.
They stay on Wispbyte.

1. Create a Python server on Wispbyte.
2. In the **Startup** tab, if there's a "Git Repo Address" option, set it to
   `https://github.com/aidendixon602-png/midtown-desk`, add your GitHub username and a read-only token
   (GitHub > Settings > Developer settings > Fine-grained tokens > this repo only, *Contents: Read-only*),
   and turn on **Auto Update**. Otherwise, upload the files with the **File Manager**.
3. Set the startup file to **`main.py`** and the requirements file to `requirements.txt`.
4. Create `.env` in the File Manager and paste in your settings.
5. Start it. The console should say `Ready as Midtown Desk`.

To update: push changes to GitHub, then restart the server (Auto Update pulls the new code).
Use `/dev backup` regularly so you have a copy of your data.

## Commands

**Everyone:** `/help` · `/ping` · `/invite` · `/about`

**Server admins** (need Manage Server):

| Command | What it does |
|---|---|
| `/setup` | One command does everything: checks permissions, creates a **Ticket Staff** role, a **Tickets** category, **#ticket-logs**, and **#open-a-ticket** with the ticket menu. Pick your own role or channels instead if you want. Safe to run again (reuses what exists) |
| `/panel` | Post another ticket menu (custom title and text) |
| `/category add / edit / remove / list / reset` | Your own ticket types. Panels update automatically |
| `/settings` | Ticket limit, auto-close hours, DM transcripts, ratings |
| `/stats` | Ticket stats and staff leaderboard |

**Staff:**

| Command | What it does |
|---|---|
| Claim / Close buttons | In every ticket. The welcome message is pinned |
| `/ticket claim` · `unclaim` · `transfer` | Take a ticket, release it, or hand it to another staff member |
| `/ticket add` · `remove` · `rename` | Manage who's in the ticket and its name |
| `/ticket close` | Close with an optional reason |
| `/ticket closerequest` | Ask the member if they're done. They confirm with a button |
| `/ticket history` | A member's past tickets in this server |
| `/ticket block` · `unblock` · `blocked` | Stop a member from opening tickets in your server |

Members can only open a new ticket once every 60 seconds, to stop spam.

**Developers only** (your dev server, your user ID):

| Command | What it does |
|---|---|
| `/dev stats` | Servers, members, tickets, latency, uptime, memory, DB size |
| `/dev servers` · `/dev server` | List servers the bot is in, or details on one |
| `/dev leave` | Make the bot leave a server (optionally blacklist it) |
| `/dev blacklist add / remove / list` | Block a user or server from using the bot |
| `/dev maintenance` | Pause the bot for everyone except developers |
| `/dev status` | Set the bot's status text |
| `/dev broadcast` | Post an announcement in every server's ticket log channel |
| `/dev reload` · `/dev sync` | Reload code without restarting; push command changes to Discord |
| `/dev backup` | Download a copy of the database |
| `/dev shutdown` | Turn the bot off |

Errors are logged to `logs/bot.log` and posted to your bot log channel with a short code that users also see,
so you can match a user's report to the exact error.

## Growing past 75 servers

Discord requires **bot verification** once a bot reaches 100 servers. Apply from the Developer Portal when you're
around 75 servers. Message Content is a privileged intent: request it during verification and explain it's used to
save ticket transcripts. Without it, transcripts in new servers will be empty.

Settings and ticket history are kept when the bot is removed from a server, so everything still works if they add
it back.
