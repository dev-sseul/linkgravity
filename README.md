# LinkGravity

**A Discord and Telegram bot for the [Antigravity](https://antigravity.google/) CLI (`agy`)** - chat with your local AI agent (Gemini, Claude, or GPT-OSS models) from your phone or any chat app, with voice channel support, interactive tool-call approvals, and multi-modal input.

It translates Antigravity CLI prompts into chat UI components (buttons, embeds, inline keyboards) so you get the same agentic coding/automation workflow you'd run in a terminal, but from Discord or Telegram - including on the go, from your phone.

## Table of Contents

- [Supported Platforms](#supported-platforms)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
- [Usage](#usage)
- [Development](#development)
- [Debugging](#debugging)
- [Security Warning](#security-warning)

## Supported Platforms

|                  | Discord | Telegram | Slack |
| ---------------- | ------- | -------- | ----- |
| 1:1 chat         | ✓       | ✓        | *     |
| Group chat       | ✓       | ✗        | *     |
| Voice calls      | ✓       | ✗        | *     |
| File attachments | ✓       | ✓        | *     |

\* Planned, not implemented yet.

## Features

- **Multi-Platform:** Talk to the same local agent from Discord or Telegram - see the [comparison table](#supported-platforms) above for what each platform supports.
- **Environment Sync:** Automatically syncs with the host's `~/.gemini` configuration.
- **Voice Interaction (Discord):** Supports voice channels with adaptive voice activity detection to segment speech and filter environmental noise, plus live "listening..." feedback while you're still talking.
- **Wake Word Recognition:** Uses phoneme-level similarity to detect wake words and activate voice commands.
- **Approval Flow:** Command and tool-call approvals become interactive buttons/inline keyboards. Chained shell commands are approved individually, and any approval can be scoped to auto-allow that command or tool going forward - something plain `agy` doesn't do.
- **Multi-Modal Input:** Attach files for the AI to read, including audio, which gets transcribed to text automatically.

## Requirements

- Node.js >= 18
- Python >= 3.10
- Antigravity CLI installed on this machine
- A messenger bot token, and at least one server/channel (or chat) to allow it in
  - **Discord** - supported
  - **Telegram** - supported
  - **Slack** - not yet (planned)

### Creating the Discord bot

In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and bot, then:

- Under **Bot**, enable the **Message Content** privileged intent - required, since the bot reads message text/attachments.
- Under **OAuth2 → URL Generator**, select the **bot** and **applications.commands** scopes, then these bot permissions:
  - Send Messages, Send Messages in Threads, Create Public Threads
  - Read Message History, Attach Files, Embed Links, Add Reactions
  - Connect, Speak - for voice channel support
- Use the generated URL to invite the bot to your server.

### Creating the Telegram bot

Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, and follow the prompts to get a token. Nothing else to configure on Telegram's side - `lgy setup` handles the rest.

## Installation

```bash
npm install -g linkgravity
```

Sets up its own Python environment automatically - no manual `pip install` needed.

## Setup

Run the configuration wizard once to set your bot token, allowed servers/channels, and other settings:

```bash
lgy setup
```

This writes to `~/.gemini/linkgravity/lgy.json`, outside the package directory, so `npm update`/reinstall never touches it. You can re-run `lgy setup` any time to change settings later - each field keeps its current value if you leave it empty.

During setup you'll be asked for one or more Discord servers to allow, and optionally specific channels within each:

- Leave the channel list empty for a server → **the whole server** is allowed - any channel can start a session.
- List specific channel IDs for a server → **only those channels** in that server are allowed.

Telegram has no server/channel gating - it's DM-only for now, so any chat the bot is in works, but you still need to add your Telegram user ID to `telegram_allowed_user_ids` or nobody's messages will be answered.

A new session is only ever started with the **`/new`** slash command - never just by typing a message:

- **Discord:** works in a regular channel (opens a new thread), from inside an existing thread (opens another new one), or in a DM with the bot (uses the DM itself - no thread to open, so `/new` on an existing DM session asks you to confirm before overwriting it).
- **Telegram:** DM only. Since a chat _is_ the session, `/new` also asks for confirmation if one's already active before overwriting it.

## Usage

```bash
lgy            # Interactive menu - pick any command below from a list
lgy version    # Print the installed version
lgy start      # Start the bot as a background daemon via PM2
lgy stop       # Stop it
lgy restart    # Restart it
lgy logs       # View live logs - add -f to follow, --tail N for more lines
lgy status     # Show daemon status and per-platform enabled/session counts
lgy enable     # Register the bot to auto-start on system boot
lgy disable    # Remove it from system boot
lgy setup      # Re-run the configuration wizard
lgy update     # Check npm for a newer version and install + restart if found
lgy help       # Show all commands
```

## Development

```bash
npm i
```

That's it - it wires up [Ruff](https://docs.astral.sh/ruff/) for Python and [Prettier](https://prettier.io/) for Node, plus git hooks that lint/format on commit and enforce [Conventional Commits](https://www.conventionalcommits.org/) commit messages.

## Debugging

```bash
LOG_LEVEL=DEBUG lgy start
```

Use `lgy logs -t` to include timestamps.

## Security Warning

This bot gives an AI agent broad access to the machine it runs on - **that's inherent to what it does, so don't expose it publicly or run it somewhere you don't fully trust its users.**

- `allowed_user_ids` (Discord) and `telegram_allowed_user_ids` (Telegram), both set via `lgy setup`, are your primary access control - always set them.
- Tool calls, including shell commands, go through an approval flow by default on every supported platform; treat anyone in either allow-list as having effectively full control of this machine.
- Your bot tokens and other settings live in `~/.gemini/linkgravity/lgy.json`, outside this repo/package directory - never commit or share that file.
