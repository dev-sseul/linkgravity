#!/usr/bin/env node
'use strict';
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');

const LGY_CONFIG_FILE = path.join(os.homedir(), '.gemini', 'linkgravity', 'lgy.json');
// Not "localhost" - the server binds 127.0.0.1 and node resolves localhost to ::1 first.
const APPROVE_HOST = '127.0.0.1';
const APPROVE_PORT = 18080;
const TIMEOUT_MS = 3600 * 1000;

function emit(payload) {
    // Exits explicitly: the keep-alive socket and its hour-long timer stay open after the
    // response arrives, and agy SIGABRTs the process instead of waiting for them to expire.
    process.stdout.write(JSON.stringify(payload), () => process.exit(0));
}

function loadApproveToken() {
    try {
        return JSON.parse(fs.readFileSync(LGY_CONFIG_FILE, 'utf8')).approve_token || '';
    } catch {
        return '';
    }
}

async function readStdin() {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    return Buffer.concat(chunks).toString('utf8');
}

function requestDecision(body) {
    return new Promise((resolve, reject) => {
        const req = http.request(
            {
                host: APPROVE_HOST,
                port: APPROVE_PORT,
                path: '/approve',
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': body.length,
                    'X-LGY-Token': loadApproveToken(),
                },
            },
            (res) => {
                const parts = [];
                res.on('data', (chunk) => parts.push(chunk));
                res.on('end', () => {
                    try {
                        resolve(JSON.parse(Buffer.concat(parts).toString('utf8')));
                    } catch (err) {
                        reject(err);
                    }
                });
            },
        );
        req.on('error', reject);
        req.setTimeout(TIMEOUT_MS, () => req.destroy(new Error('timed out waiting for approval')));
        req.end(body);
    });
}

async function main() {
    if (process.env.LGY_APPROVAL_HOOK !== '1') {
        emit({ decision: 'allow' });
        return;
    }

    let hookInput;
    try {
        hookInput = JSON.parse(await readStdin());
    } catch {
        emit({ decision: 'deny', reason: 'Failed to parse hook input.' });
        return;
    }

    const toolCall = hookInput.toolCall || {};
    const body = Buffer.from(
        JSON.stringify({
            conversation_id: hookInput.conversationId || 'unknown',
            tool_name: toolCall.name || 'unknown_tool',
            tool_input: toolCall.args || {},
            thread_id: process.env.LGY_THREAD_ID || null,
        }),
        'utf8',
    );

    try {
        const result = await requestDecision(body);
        if ((result.decision || 'allow') === 'allow') {
            const out = { decision: 'allow' };
            // Print mode requires a matching allow rule even when this hook says "allow", or it soft-denies anyway.
            if (result.permissionOverrides) out.permissionOverrides = result.permissionOverrides;
            emit(out);
        } else {
            emit({ decision: 'deny', reason: result.reason || 'User rejected the action.' });
        }
    } catch (err) {
        process.stderr.write(`Hook error: ${err.message}\n`);
        emit({ decision: 'deny', reason: `Connection to webhook failed: ${err.message}` });
    }
}

main();
