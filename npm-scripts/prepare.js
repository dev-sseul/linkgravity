#!/usr/bin/env node
// Runs at `prepare` time - which npm only triggers for a local `npm
// install` inside this repo (i.e. a git clone / contributor checkout),
// never for `npm install -g linkgravity` end users installing the published
// package from the registry. That's exactly why dev-only setup (git
// hooks, lint tooling) lives here instead of postinstall.js, which runs
// for everyone, including end users who don't need any of this.
'use strict';
const { execSync } = require('child_process');
const fs = require('fs');
const { repoRoot, pip, preCommit } = require('./venv-paths');

if (!fs.existsSync(pip)) {
    console.warn(
        '⚠️  No venv found yet - skipping dev tooling install and git hook setup. ' +
            'Run `npm install` again once the venv exists, or set it up manually.',
    );
    return;
}

// 1. Dev-only Python tooling into the same venv postinstall.js already
// created (prepare always runs after postinstall in npm's lifecycle
// order): ruff (editor/manual use) and pre-commit itself, which is
// what actually runs the hooks declared in .pre-commit-config.yaml.
try {
    execSync(`"${pip}" install -r requirements-dev.txt`, { stdio: 'inherit', cwd: repoRoot });
} catch (err) {
    console.warn(`⚠️  Couldn't install dev Python tooling: ${err.message.split('\n')[0]}`);
    return;
}

// 2. Register the actual git hooks. --hook-type is passed explicitly
// here even though .pre-commit-config.yaml's default_install_hook_types
// already covers it, just so this line is self-explanatory on its own.
try {
    execSync(`"${preCommit}" install --hook-type pre-commit --hook-type commit-msg`, {
        stdio: 'inherit',
        cwd: repoRoot,
    });
} catch (err) {
    console.warn(
        `⚠️  Couldn't install git hooks (${err.message.split('\n')[0]}). ` +
            "Pre-commit checks won't run until `venv/bin/pre-commit install --hook-type pre-commit --hook-type commit-msg` is run manually.",
    );
}
