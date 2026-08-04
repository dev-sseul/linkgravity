'use strict';
// Only ever overwrites `command` on an existing entry - never `type`/`timeout`, which the user may have customized.
const fs = require('fs');
const path = require('path');
const os = require('os');
const { repoRoot, python: venvPython } = require('./venv-paths');

const hooksJsonPath = path.join(os.homedir(), '.gemini', 'config', 'hooks.json');

// PreToolUse/Stop meanings are agy's own hook contract: Stop fires when agy is about to
// end a turn, and if fullyIdle is false (an async run_command still in flight), stop_hook.py
// tells agy to keep going instead of losing that result.
const HOOK_REGISTRATIONS = [
    {
        eventType: 'PreToolUse',
        name: 'discord-approval',
        scriptPath: path.join(repoRoot, 'hooks', 'hook.py'),
        defaultTimeout: 3600,
        wrapInMatcher: true,
    },
    {
        eventType: 'Stop',
        name: 'discord-approval-stop',
        scriptPath: path.join(repoRoot, 'hooks', 'stop_hook.py'),
        defaultTimeout: 30,
        wrapInMatcher: false,
    },
];

const RETIRED_HOOKS = [{ eventType: 'PreInvocation', name: 'wait-ms-before-async-reminder' }];

function loadHooksConfig() {
    if (!fs.existsSync(hooksJsonPath)) {
        return { hooks: {} };
    }
    try {
        return JSON.parse(fs.readFileSync(hooksJsonPath, 'utf8'));
    } catch (err) {
        const backupPath = `${hooksJsonPath}.corrupted-${Date.now()}`;
        fs.copyFileSync(hooksJsonPath, backupPath);
        console.warn(
            `⚠️  ${hooksJsonPath} was invalid JSON - backed up to ${backupPath} and starting fresh.`,
        );
        return { hooks: {} };
    }
}

function findHookEntry(config, eventType, name, wrapInMatcher) {
    config.hooks[eventType] = config.hooks[eventType] || [];
    if (wrapInMatcher) {
        let matcher = config.hooks[eventType].find((m) =>
            (m.hooks || []).some((h) => h.name === name),
        );
        if (!matcher) {
            matcher = { matcher: '.*', hooks: [] };
            config.hooks[eventType].push(matcher);
        }
        let hookEntry = matcher.hooks.find((h) => h.name === name);
        if (!hookEntry) {
            hookEntry = { name };
            matcher.hooks.push(hookEntry);
        }
        return hookEntry;
    }
    let hookEntry = config.hooks[eventType].find((h) => h.name === name);
    if (!hookEntry) {
        hookEntry = { name };
        config.hooks[eventType].push(hookEntry);
    }
    return hookEntry;
}

function removeRetiredHooks(config) {
    let removedAny = false;
    for (const retired of RETIRED_HOOKS) {
        const arr = config.hooks[retired.eventType];
        if (!arr) continue;

        const nextArr = [];
        for (const entry of arr) {
            if (Array.isArray(entry.hooks)) {
                const beforeLen = entry.hooks.length;
                entry.hooks = entry.hooks.filter((h) => h.name !== retired.name);
                if (entry.hooks.length !== beforeLen) {
                    removedAny = true;
                    console.log(
                        `🧹 Removed retired agy ${retired.eventType} hook '${retired.name}' from ${hooksJsonPath}`,
                    );
                }
                if (entry.hooks.length > 0) nextArr.push(entry);
            } else {
                if (entry.name === retired.name) {
                    removedAny = true;
                    console.log(
                        `🧹 Removed retired agy ${retired.eventType} hook '${retired.name}' from ${hooksJsonPath}`,
                    );
                } else {
                    nextArr.push(entry);
                }
            }
        }
        config.hooks[retired.eventType] = nextArr;
    }
    return removedAny;
}

function registerHook({ allowFirstTimeCreate = true } = {}) {
    const config = loadHooksConfig();
    config.hooks = config.hooks || {};

    const isFirstTime = HOOK_REGISTRATIONS.some((reg) => {
        const arr = config.hooks[reg.eventType] || [];
        return reg.wrapInMatcher
            ? !arr.some((m) => (m.hooks || []).some((h) => h.name === reg.name))
            : !arr.some((h) => h.name === reg.name);
    });

    if (isFirstTime && !allowFirstTimeCreate) {
        console.log(
            "ℹ️  LinkGravity's Discord/Telegram/Slack approval hook isn't registered with agy yet - run `lgy setup` to enable it.",
        );
        return;
    }

    let wroteChange = false;
    let backedUp = false;

    const backupBeforeFirstChange = () => {
        if (!backedUp && fs.existsSync(hooksJsonPath)) {
            fs.copyFileSync(hooksJsonPath, `${hooksJsonPath}.bak`);
            backedUp = true;
        }
    };

    if (removeRetiredHooks(config)) {
        backupBeforeFirstChange();
        wroteChange = true;
    }

    for (const reg of HOOK_REGISTRATIONS) {
        const command = `"${venvPython}" "${reg.scriptPath}"`;
        const hookEntry = findHookEntry(config, reg.eventType, reg.name, reg.wrapInMatcher);
        const isNew = !hookEntry.command;

        if (isNew) {
            hookEntry.type = 'command';
            hookEntry.timeout = reg.defaultTimeout;
            hookEntry.command = command;
            console.log(
                `🔗 Registered agy ${reg.eventType} hook '${reg.name}' -> ${reg.scriptPath}`,
            );
            wroteChange = true;
        } else if (hookEntry.command !== command) {
            backupBeforeFirstChange();
            console.log(
                `🔗 Fixing agy ${reg.eventType} hook '${reg.name}' in ${hooksJsonPath}` +
                    (backedUp ? ` (previous version backed up to ${hooksJsonPath}.bak)` : '') +
                    `:\n   was: ${hookEntry.command}\n   now: ${command}`,
            );
            hookEntry.command = command;
            wroteChange = true;
        } else {
            console.log(
                `🔗 agy ${reg.eventType} hook '${reg.name}' already up to date -> ${reg.scriptPath}`,
            );
        }
    }

    if (wroteChange) {
        fs.mkdirSync(path.dirname(hooksJsonPath), { recursive: true });
        fs.writeFileSync(hooksJsonPath, JSON.stringify(config, null, 2));
    }
}

function isHookRegistered() {
    const config = loadHooksConfig();
    config.hooks = config.hooks || {};
    return HOOK_REGISTRATIONS.every((reg) => {
        const arr = config.hooks[reg.eventType] || [];
        return reg.wrapInMatcher
            ? arr.some((m) => (m.hooks || []).some((h) => h.name === reg.name))
            : arr.some((h) => h.name === reg.name);
    });
}

module.exports = registerHook;
module.exports.isHookRegistered = isHookRegistered;

if (require.main === module) {
    registerHook();
}
