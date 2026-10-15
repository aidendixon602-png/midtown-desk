# Midtown Desk changelog

## 2.3.2 — Support menu for Genre Intelligence

- Replaced the default **Report a Player** option with **Bug Report** and **Appeal** with
  **Feature Request**, including matching icons and descriptions. General Support, Partnership
  and Other remain available.
- Existing complete, unedited gaming menus migrate automatically in the database when the new
  code starts. Category IDs remain stable; customized menus and ticket history remain intact.
- Already-posted dropdowns refresh automatically after connecting. Pending updates persist across
  restarts and failed edits retry every minute, so no category-reset or panel-recreation commands
  are required for unchanged default menus.
- Panel refreshes fetch uncached channels and retain panel records after temporary/permission
  failures. Confirmed deleted channels/messages are removed from the registry.
- Added 13 offline tests for default categories, database migration, custom/history preservation,
  persisted refresh work, posted menu contents and retry/cache behavior (49 total tests).
- Updated the README and bot version. Changes take effect when the host loads the new GitHub code;
  merging a commit cannot change an already-running process that has not updated.

## 2.3.1 — Reliability and interaction fixes

This records the Midtown Desk changes in this update. Deployment requires merging the changes,
updating the host and restarting the bot. Live Discord behavior still needs a smoke test on the host.

### Startup and command registration

- Invalid bot tokens now produce a direct explanation of which host setting to fix.
- Discord's disabled/unapproved Message Content Intent error now explains where to enable it.
- Leading/trailing whitespace is removed from the configured token.
- An HTTP failure during initial slash command registration is logged without aborting the
  bot's connection. Owners can retry registration with the existing prefix `sync` command.
- Permission and cooldown failures now reach their friendly error responses instead of being
  swallowed by the generic check-failure handler.
- Version updated from 2.3.0 to 2.3.1. Dependencies stay within the current major releases
  (`discord.py` 2.x and `python-dotenv` 1.x).

### Authorization, buttons and responses

- Ticket handling now processes only its own component IDs, leaving server approval buttons
  to the authorization cog. This prevents two listeners from responding to the same approval.
- Approval buttons are validated and acknowledged before welcome messages, logging or leaving
  a server. Completion edits the original response after the work finishes.
- Failed approval actions report an error code and allow a retry.
- `/panel`, `/ticket add`, `/ticket remove`, owner authorization/revocation/leaving,
  maintenance and status commands now acknowledge before slow Discord operations.
- Authorization denial can reply through a follow-up if an interaction is already deferred.
- Owner discovery and owner-command registration tasks are cancelled when their cogs unload,
  avoiding duplicate background work after hot reloads.
- Welcome/approval notices choose a channel the bot can view and send embeds in.

### Setup and permissions

- Admin commands enforce Manage Server at execution time, including category commands.
- `/report` enforces Manage Server at execution time too.
- `/settings` rejects `@everyone` and managed bot roles as the ticket staff role.
- `/setup` saves the selected/created role and category as it progresses. If a later channel
  creation fails, retrying reuses those resources rather than creating duplicates.

### Ticket creation and forms

- Open-ticket forms recheck server authorization, maintenance/blacklist restrictions, the
  server's ticket block, cooldown, current ticket type, configured staff role and ticket limit
  when submitted. Forms opened before a setting or permission change cannot bypass it.
- Submissions from the same member in the same server are serialized, preventing two forms
  from simultaneously passing the ticket limit.
- Tickets still closing count toward the member's open-ticket limit.
- If the welcome message fails, creation attempts to remove the incomplete channel and its
  database record. If that cleanup fails too, the existing channel remains tracked and the
  member receives its link plus instructions to use `/ticket` commands without buttons.
- Close forms recheck current authorization and staff/ticket-owner permissions on submission.
- Close-form failures now produce a visible error response.

### Claims, ratings and closing

- Claims, transfers and unclaims use conditional database updates. Stale actions cannot
  overwrite a staff assignment that changed while the command was running.
- Claim/unclaim/transfer commands confirm success after the state change succeeds.
- Ratings accept only 1–5 stars for closed tickets belonging to the member. A recorded rating
  cannot be overwritten by another click.
- Ticket state is marked closed only after the channel is successfully deleted or Discord
  confirms it was already deleted. Deletion failures are no longer silently ignored.
- Failed or cancelled closes restore the open state and activity tracking so staff can retry.
- A transcript-generation failure preserves the channel.
- Closing requires successful transcript delivery to the configured log channel or the member's
  enabled transcript DM. If neither succeeds, the channel stays open with an actionable error.
- Log delivery reports success/failure to the closing code. Closing and completed-close notices
  are distinguished; rating buttons are added to the transcript DM after closure completes.

### Background tasks and validation

- Auto-close fetches uncached channels before deciding they were deleted. Temporary API/permission
  errors and unavailable servers no longer cause valid tickets to be marked closed.
- One ticket's close failure is logged without stopping the remaining auto-close work.
- Failed weekly report delivery leaves its schedule unchanged so the next hourly run retries.
- Added 36 offline regression tests covering startup, real extension/command registration,
  permissions, approval routing, stale forms, concurrent submissions, partial setup/creation,
  claims, ratings, safe closing, task cleanup and report retries.
- Added GitHub Actions checks on Python 3.10, 3.12 and 3.13, plus syntax compilation.
- Expanded README setup, troubleshooting, update and test commands, including required intents,
  invite scopes, server approval, host environment precedence and transcript permissions.

### Validation limits

Tests use real bot modules and SQLite with mocked Discord network operations. They do not
validate a live bot token, hosting configuration, Discord channel permissions or a deployed instance.
After deployment, verify `/ping`, `/setup`, opening a ticket, claiming it, closing it, the archived
transcript and its DM rating controls in a test server.
