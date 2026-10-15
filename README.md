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
- Ticket priority (🔴 urgent / 🟠 high / 🔵 low) shown in channel names
- Weekly stats report in each server's log channel
- Server authorization: only servers you approve can use it (approve from a DM)
- Welcome message with setup steps when added to a new server
- Owner tools (/owner): stats, server list, approvals, blocks, maintenance, broadcast, hot reload, backups

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
  reports.py         Weekly report, /report
  auth.py            Server authorization (,authorize ...)
  owner.py           /owner commands (+ hidden ,help text commands)
  events.py          Server joins/leaves, status
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
| `DEV_IDS` | Optional: teammates' user IDs who should also get owner commands (you're detected automatically) |
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
Use `/owner backup` regularly so you have a copy of your data.

## Server authorization

By default the bot only works in servers **you approve** (turn this off with `REQUIRE_AUTH=false`).

1. Servers **you own** are approved automatically.
2. When someone else adds the bot, their server sees a "waiting for approval" message with its server ID.
3. You get a **DM** with the server's name, size and owner, plus **Authorize** and **Deny & leave** buttons.
4. Once approved, the server gets the normal welcome message and can run `/setup`.

Until then, every command in that server (except `/help`, `/about`, `/invite`, `/ping`) says it isn't authorized.
You're detected automatically as the owner of the bot in the Developer Portal.

## Owner commands (`/owner`)

These slash commands only appear in **servers you own** (detected automatically), so nobody else ever sees them,
and they only work for you. The same commands also exist as hidden text commands (`,help`) for DMs.

| Command | What it does |
|---|---|
| `/owner authorize [server_id] [note]` | Approve a server (empty = this one). Works before they've even added the bot |
| `/owner revoke <server_id> [leave]` | Remove approval, optionally making the bot leave |
| `/owner servers` · `/owner approved` | All servers (✅ approved / 🔒 waiting), or only approved ones |
| `/owner stats` · `/owner server <id>` | Bot-wide stats, or details on one server |
| `/owner leave <server_id> [block]` | Make the bot leave, optionally blocking that server for good |
| `/owner maintenance` | Pause the bot for everyone except you |
| `/owner status [text]` | Set the bot's status text |
| `/owner block` · `unblock` · `blocked` | Block a user from using the bot everywhere |
| `/owner broadcast` | Post an announcement in every server's ticket log channel |
| `/owner backup` | DMs you a copy of the database |
| `/owner reload` · `/owner sync` | Reload code without restarting; push slash command changes |

## Commands

**Everyone:** `/help` · `/ping` · `/invite` · `/about`

**Server admins** (need Manage Server):

| Command | What it does |
|---|---|
| `/setup` | One command does everything: checks permissions, creates a **Ticket Staff** role, a **Tickets** category, **#ticket-logs**, and **#open-a-ticket** with the ticket menu. Pick your own role or channels instead if you want. Safe to run again (reuses what exists) |
| `/panel` | Post another ticket menu (custom title and text) |
| `/category add / edit / remove / list / reset` | Your own ticket types. Panels update automatically |
| `/settings` | Ticket limit, auto-close hours, DM transcripts, ratings, weekly report |
| `/stats` | All-time ticket stats and staff leaderboard |
| `/report` | Last 7 days: opened/closed with trend, avg close time, ratings, top staff, unclaimed tickets. Also posted automatically every week in the log channel (turn off with `/settings weekly_report:False`) |

**Staff:**

| Command | What it does |
|---|---|
| Claim / Close buttons | In every ticket. The welcome message is pinned |
| `/ticket claim` · `unclaim` · `transfer` | Take a ticket, release it, or hand it to another staff member |
| `/ticket priority` | 🔴 Urgent (pings staff) · 🟠 High · ⚪ Normal · 🔵 Low. Adds the color to the channel name so it stands out |
| `/ticket add` · `remove` · `rename` | Manage who's in the ticket and its name |
| `/ticket close` | Close with an optional reason |
| `/ticket closerequest` | Ask the member if they're done. They confirm with a button |
| `/ticket history` | A member's past tickets in this server |
| `/ticket block` · `unblock` · `blocked` | Stop a member from opening tickets in your server |

Members can only open a new ticket once every 60 seconds, to stop spam.

Errors are logged to `logs/bot.log` and posted to your bot log channel with a short code that users also see,
so you can match a user's report to the exact error.

## Growing past 75 servers

Discord requires **bot verification** once a bot reaches 100 servers. Apply from the Developer Portal when you're
around 75 servers. Message Content is a privileged intent: request it during verification and explain it's used to
save ticket transcripts. Without it, transcripts in new servers will be empty.

Settings and ticket history are kept when the bot is removed from a server, so everything still works if they add
it back.
