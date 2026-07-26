# LinkGravity (lgy)

A Discord bot interface for the Antigravity (agy) agentic AI system. It translates Antigravity CLI prompts into Discord UI components and provides voice interaction capabilities.

## Features

- **Environment Sync:** Automatically syncs with the host's `~/.gemini` configuration.
- **Voice Interaction:** Supports voice channels with adaptive VAD (Voice Activity Detection) to segment speech and filter environmental noise, plus live "listening..." feedback while you're still talking.
- **Wake Word Recognition:** Uses phoneme-level similarity to detect wake words and activate voice commands.
- **CLI Prompt Interception:** Converts CLI prompts (`ask_question`, `run_command` approvals) into interactive Discord buttons.
- **Command Security:** Parses chained shell commands (`&&`, `||`, `;`) and requires individual approval for each command. Supports prefix-based scope whitelisting.
- **Multi-Modal Input:** Attach any file (not just images) for the AI to read; audio attachments (`.ogg`/`.mp3`/`.m4a`/`.wav`) are transcribed to text automatically.

## Prerequisites

- Node.js >= 18
- Python >= 3.10
- `agy` (Antigravity CLI) installed on this machine
- `ffmpeg` on your PATH (needed for TTS/voice playback)
- A Discord bot token, and at least one server/channel to allow it in

### Creating the Discord bot

In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and bot, then:

- Under **Bot**, enable the **Message Content** privileged intent (required - the bot reads message text/attachments).
- Under **OAuth2 → URL Generator**, select the **bot** and **applications.commands** scopes, then these bot permissions:
  - Send Messages, Send Messages in Threads, Create Public Threads
  - Read Message History, Attach Files, Embed Links, Add Reactions
  - Connect, Speak (for voice channel support)
- Use the generated URL to invite the bot to your server.

## Installation

```bash
npm install -g linkgravity
```

This runs a postinstall step that creates a Python virtual environment at `~/.gemini/linkgravity/venv/` (kept outside the package install location on purpose - see `npm-scripts/venv-paths.js`) and installs `requirements.txt` into it - no manual `pip install` needed.

## Setup

Run the configuration wizard once to set your bot token, allowed servers/channels, and other settings:

```bash
lgy setup
```

This writes to `~/.gemini/linkgravity/lgy.json` (outside the package directory, so `npm update`/reinstall never touches it). You can re-run `lgy setup` any time to change settings later - each field keeps its current value if you leave it empty.

During setup you'll be asked for one or more Discord servers to allow, and optionally specific channels within each:

- Leave the channel list empty for a server → **the whole server** is allowed (any channel can start a session).
- List specific channel IDs for a server → **only those channels** in that server are allowed.

A new session is only ever started with the **`/new`** slash command in Discord - never just by typing a message. `/new` works both in a regular channel and from inside an existing thread.

## Usage

```bash
lgy start      # Start the bot as a background daemon (via PM2)
lgy stop       # Stop it
lgy restart    # Restart it
lgy logs       # View live logs (add -f to follow, --tail N for more lines)
lgy enable     # Register the bot to auto-start on system boot
lgy disable    # Remove it from system boot
lgy            # Interactive menu (same commands, picked from a list)
```

## Development

This project uses [Ruff](https://docs.astral.sh/ruff/) for Python linting/formatting and [Prettier](https://prettier.io/) for the Node.js side. `npm install` sets both up automatically (installs `requirements-dev.txt` into the venv at `~/.gemini/linkgravity/venv/`, registers git hooks) - manual install is only needed if you want to run them yourself outside of a commit. The venv lives outside this checkout (see `npm-scripts/venv-paths.js` for why), so on macOS/Linux:

```bash
~/.gemini/linkgravity/venv/bin/ruff check src/    # lint
~/.gemini/linkgravity/venv/bin/ruff format src/   # format

npm run format:check        # check JS formatting
npm run format              # format JS
```

(On Windows, replace `venv/bin/ruff` with `venv\Scripts\ruff.exe` under the same `~/.gemini/linkgravity/` directory.)

Git hooks (via [pre-commit](https://pre-commit.com/), config in `.pre-commit-config.yaml`) run automatically once you `npm install`:

- **pre-commit**: runs `ruff` (lint + format) and `prettier` on staged files, auto-fixing what it can.
- **commit-msg**: enforces [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `fix: ...`, `feat: ...`, `docs: ...`) via [conventional-pre-commit](https://github.com/compilerla/conventional-pre-commit).

If a hook doesn't seem to be running, check `git config --get core.hooksPath` - it should be unset (or point at `.git/hooks`, pre-commit's default). A leftover `.husky` value from an older checkout will silently make git skip pre-commit's hooks entirely; `git config --unset core.hooksPath` fixes it.

## Debugging

Set `LOG_LEVEL=DEBUG` in your shell before `lgy restart` for verbose logs, including agy's raw stdout for each turn (`lgy` passes your shell's environment through to the daemon on restart). Defaults to `INFO`. Use `lgy logs -t` (or `--timestamp`) to include timestamps - they're stripped by default.

## Known Issues

**Wake word false positives on short words** (e.g. "시리", "잼민이"): Rustpotter's phoneme matching carries less signal for 1-2 syllable words, so genuine-match and unrelated-speech score distributions overlap - no single threshold cleanly separates them. Current mitigations in `voice-service/index.js`'s `getDetectorForUser` and `cogs/voice_cog.py`'s `handle_stt_input`:

- `score_mode: Max` (each of the 5 enrollment samples can cover a different natural tone/pace, instead of requiring all 5 to be delivered consistently like `Median` did)
- `min_scores: 4` (requires a candidate to keep winning across several frames, compensating for `Max` being more permissive per-frame)
- The STT-based text cross-check is tightened specifically for short wake words (similarity floor 0.55, vs. 0.35 for longer ones) - this is currently doing most of the real work of rejecting false positives

This isn't fully solved. If issues persist after real-world use, prefer these over further threshold guessing:

1. Encourage re-enrolling with a longer/more distinctive wake word (a 1-2 syllable word is close to a hard ceiling for this approach, regardless of tuning)
2. Log `bestWakeScore` + outcome (no raw audio) during a trial period and re-tune the constants above against that data instead of guessing

## Security Warning

This bot gives an AI agent broad access to the machine it runs on - **that's inherent to what it does, so don't expose it publicly or run it somewhere you don't fully trust its users.**

- `allowed_user_ids` (set via `lgy setup`) is your primary access control - always set it.
- Tool calls (including shell commands) go through an approval flow in Discord by default; treat anyone in `allowed_user_ids` as having effectively full control of this machine.
- Your Discord token and other settings live in `~/.gemini/linkgravity/lgy.json`, outside this repo/package directory - never commit or share that file.
