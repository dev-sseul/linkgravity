# Contributing to LinkGravity

Bridges Discord/Telegram/Slack to the Antigravity CLI, with a Node voice service alongside the Python bot core. Contributions to either side welcome.

## Setup

```bash
git clone https://github.com/dev-sseul/linkgravity.git
cd linkgravity
npm install
```

That's it. Sets up the Python venv, installs deps (runtime + dev tooling), and registers git hooks - nothing manual after.

## Running it

```bash
npm run dev
```

Needs your own bot token(s) - see the README's bot setup sections, then `lgy setup`.

## Code style

Python: ruff. JS: prettier. Both enforced via git hooks, no manual step needed.

## No automated tests

Verify changes by running the bot for real. If it's platform-specific, say which platform(s) you tested, and check whether it applies to the others too - one-platform-only fixes have been a recurring bug source here (see the README's `Supported Platforms` table).

## Where to start

Anything marked ✗ in that table is a known gap. Open an issue before a large PR so we can agree on approach first.

## Commits

Conventional Commits, enforced by the commit-msg hook. One-line summary is fine.
