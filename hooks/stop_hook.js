#!/usr/bin/env node
'use strict';
// Stop-event hook (https://antigravity.google/docs/hooks).
// `fullyIdle: false` means a run_command that detached to async is still in flight; "continue"
// keeps the turn alive so agy picks that result up instead of losing it.
const fs = require('fs');
const os = require('os');
const path = require('path');

const MAX_CONTINUE_ATTEMPTS = 20;
// Its own process, so it never reaches `lgy logs`.
const DEBUG_LOG = path.join(os.homedir(), '.gemini', 'linkgravity', 'logs', 'stop_hook_debug.log');

function log(line) {
    try {
        fs.mkdirSync(path.dirname(DEBUG_LOG), { recursive: true });
        fs.appendFileSync(DEBUG_LOG, `${new Date().toISOString()} ${line}\n`);
    } catch {}
}

function emit(payload) {
    process.stdout.write(JSON.stringify(payload));
}

async function readStdin() {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    return Buffer.concat(chunks).toString('utf8');
}

async function main() {
    const raw = await readStdin();
    let hookInput;
    try {
        hookInput = JSON.parse(raw);
    } catch (err) {
        log(`[PARSE ERROR] ${err.message} raw=${JSON.stringify(raw)}`);
        emit({});
        return;
    }

    const fullyIdle = hookInput.fullyIdle ?? true;
    const executionNum = hookInput.executionNum ?? 0;
    log(
        `[STOP HOOK] fullyIdle=${fullyIdle} executionNum=${executionNum} ` +
            `terminationReason=${JSON.stringify(hookInput.terminationReason)} ` +
            `conv=${JSON.stringify(hookInput.conversationId)}`,
    );

    if (!fullyIdle && executionNum < MAX_CONTINUE_ATTEMPTS) {
        const response = {
            decision: 'continue',
            reason:
                'A background command is still running. Wait for it to finish, ' +
                'then report its actual result to the user before ending your turn.',
        };
        log(`[STOP HOOK] -> continue: ${JSON.stringify(response)}`);
        emit(response);
    } else {
        log('[STOP HOOK] -> {} (fullyIdle true or attempt cap reached)');
        emit({});
    }
}

main();
