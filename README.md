# LinkGravity

A Discord bot interface for the Antigravity agentic AI system. It translates Antigravity CLI prompts into Discord UI components and provides voice interaction capabilities.

## Features

- **Environment Sync:** Automatically syncs with the host's `~/.gemini` configuration.
- **Voice Interaction:** Supports voice channels with adaptive voice activity detection to segment speech and filter environmental noise, plus live "listening..." feedback while you're still talking.
- **Wake Word Recognition:** Uses phoneme-level similarity to detect wake words and activate voice commands.
- **Approval Flow:** Command and tool-call approvals become interactive Discord buttons. Chained shell commands are approved individually, and any approval can be scoped to auto-allow that command or tool going forward - something plain `agy` doesn't do.
- **Multi-Modal Input:** Attach files for the AI to read, including audio, which gets transcribed to text automatically.

## Requirements

- Node.js >= 18
- Python >= 3.10
- Antigravity CLI installed on this machine
- A messenger bot token, and at least one server/channel to allow it in
  - **Discord** - currently the only one supported

### Creating the Discord bot

In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and bot, then:

- Under **Bot**, enable the **Message Content** privileged intent - required, since the bot reads message text/attachments.
- Under **OAuth2 → URL Generator**, select the **bot** and **applications.commands** scopes, then these bot permissions:
  - Send Messages, Send Messages in Threads, Create Public Threads
  - Read Message History, Attach Files, Embed Links, Add Reactions
  - Connect, Speak - for voice channel support
- Use the generated URL to invite the bot to your server.

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

A new session is only ever started with the **`/new`** slash command in Discord - never just by typing a message. `/new` works both in a regular channel and from inside an existing thread.

## Usage

```bash
lgy start      # Start the bot as a background daemon via PM2
lgy stop       # Stop it
lgy restart    # Restart it
lgy logs       # View live logs - add -f to follow, --tail N for more lines
lgy enable     # Register the bot to auto-start on system boot
lgy disable    # Remove it from system boot
lgy            # Interactive menu - same commands, picked from a list
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

- `allowed_user_ids`, set via `lgy setup`, is your primary access control - always set it.
- Tool calls, including shell commands, go through an approval flow in Discord by default; treat anyone in `allowed_user_ids` as having effectively full control of this machine.
- Your Discord token and other settings live in `~/.gemini/linkgravity/lgy.json`, outside this repo/package directory - never commit or share that file.
