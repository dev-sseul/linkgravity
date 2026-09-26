#!/usr/bin/env node
'use strict';

const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const readline = require('readline');

const CONFIG_FILE = path.join(os.homedir(), '.gemini', 'linkgravity', 'lgy.json');
const APPROVE_HOST = '127.0.0.1';
const APPROVE_PORT = 18080;
const SUPPORTED_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05'];

const TOOLS = [
    {
        name: 'send_attachment',
        description:
            'Send a file to the user as a real attachment in the chat they are talking to you from ' +
            '(Discord/Telegram/Slack). Use this whenever the user asks for something as a file, image, ' +
            'document or download - writing the file to disk alone does NOT deliver it to them. ' +
            'Call it once per file, after the file exists on disk.',
        inputSchema: {
            type: 'object',
            properties: {
                path: {
                    type: 'string',
                    description: 'Absolute path of an existing file to attach.',
                },
                caption: { type: 'string', description: 'Optional short line sent with the file.' },
            },
            required: ['path'],
        },
    },
    {
        name: 'schedule_create',
        description:
            'Schedule a prompt to run later in THIS chat, in this same conversation, so the user can reply to the ' +
            'result. Use this for every reminder or scheduled/recurring task instead of the built-in schedule tool ' +
            'or sidecar automations, which never reach the chat. ' +
            'LinkGravity keeps the schedule itself - it survives restarts; do not try to wait or sleep. ' +
            'Give exactly one of at, delay, every, cron. Times are the local time of the machine. ' +
            'Write the prompt as the instruction to carry out when it fires (e.g. "Summarize today\'s AI news"), ' +
            'not as a reminder about scheduling. For conditional checks, put the condition in the prompt ' +
            '(e.g. "Check PM2.5 in Seoul; only report if it is bad") - runs with nothing to report stay silent. ' +
            'The user then picks in the chat where results go, which model runs it, and how approvals are ' +
            'handled while it runs, so just call this - do not ask them about any of that first. ' +
            'If they cancel, nothing is saved.',
        inputSchema: {
            type: 'object',
            properties: {
                prompt: { type: 'string', description: 'What to do when the schedule fires.' },
                name: {
                    type: 'string',
                    description: 'Short label in the user\'s language, e.g. "Bedtime reminder".',
                },
                at: {
                    type: 'string',
                    description:
                        'Run once at this local time: ISO like 2026-09-27T09:00, or HH:MM for its next occurrence.',
                },
                delay: {
                    type: 'string',
                    description: 'Run once after this long, e.g. 30m, 2h, 1d.',
                },
                every: {
                    type: 'string',
                    description:
                        'Repeat at this interval, e.g. 90m, 6h, 1d (daily), 3d (every three days).',
                },
                start: {
                    type: 'string',
                    description:
                        'With every: first run time (ISO, or HH:MM for its next occurrence). "every 1d, start 09:00" = daily at 9. ' +
                        'Defaults to one interval from now.',
                },
                cron: {
                    type: 'string',
                    description: 'Standard 5-field cron, e.g. "0 9 * * 1-5" for weekdays at 9:00.',
                },
            },
            required: ['prompt', 'name'],
        },
    },
    {
        name: 'schedule_list',
        description: 'List all LinkGravity schedules with their ids, timing and last result.',
        inputSchema: { type: 'object', properties: {} },
    },
    {
        name: 'schedule_update',
        description:
            'Change a schedule by id. Only the fields given change; giving at/delay/every/cron replaces the timing. ' +
            'enabled=false pauses it.',
        inputSchema: {
            type: 'object',
            properties: {
                id: { type: 'string' },
                prompt: { type: 'string' },
                name: { type: 'string' },
                at: { type: 'string' },
                delay: { type: 'string' },
                every: { type: 'string' },
                start: { type: 'string' },
                cron: { type: 'string' },
                approval: {
                    type: 'string',
                    enum: ['readonly', 'ask', 'auto'],
                    description:
                        'Give any value to ask the user to pick a new approval mode in the chat.',
                },
                change_model: {
                    type: 'boolean',
                    description: 'true asks the user in the chat which model should run it.',
                },
                change_destination: {
                    type: 'boolean',
                    description:
                        'true asks the user in the chat where results should go from now on.',
                },
                enabled: { type: 'boolean' },
            },
            required: ['id'],
        },
    },
    {
        name: 'schedule_delete',
        description: 'Delete a schedule by id.',
        inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
    },
    {
        name: 'schedule_run_now',
        description: 'Run a schedule once right away, without changing its next run.',
        inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'] },
    },
];

// Only agy runs started by LinkGravity carry LGY_THREAD_ID; a terminal session keeps agy's own defaults.
const CHAT_INSTRUCTIONS =
    'You are talking to the user through a chat app (Discord/Telegram/Slack) via LinkGravity. ' +
    "For any reminder, timer, or scheduled/recurring task, use this server's schedule_create tool rather than " +
    'the built-in schedule tool or sidecar automations: those run inside this agy process or a separate agent, ' +
    'so their results never reach this chat. Use send_attachment to deliver files.';

const SCHEDULE_ACTIONS = {
    schedule_create: 'create',
    schedule_list: 'list',
    schedule_update: 'update',
    schedule_delete: 'delete',
    schedule_run_now: 'run_now',
};

function loadApproveToken() {
    try {
        return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')).approve_token || '';
    } catch {
        return '';
    }
}

function postJson(urlPath, payload) {
    return new Promise((resolve, reject) => {
        const body = Buffer.from(JSON.stringify(payload), 'utf8');
        const req = http.request(
            {
                host: APPROVE_HOST,
                port: APPROVE_PORT,
                path: urlPath,
                method: 'POST',
                agent: false,
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': body.length,
                    'X-LGY-Token': loadApproveToken(),
                },
            },
            (res) => {
                const chunks = [];
                res.on('data', (c) => chunks.push(c));
                res.on('end', () => {
                    const text = Buffer.concat(chunks).toString('utf8');
                    try {
                        resolve(JSON.parse(text));
                    } catch {
                        reject(
                            new Error(
                                `LinkGravity returned ${res.statusCode}: ${text.slice(0, 200)}`,
                            ),
                        );
                    }
                });
            },
        );
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

function send(msg) {
    process.stdout.write(JSON.stringify(msg) + '\n');
}

function result(id, payload) {
    send({ jsonrpc: '2.0', id, result: payload });
}

function toolText(text, isError = false) {
    return { content: [{ type: 'text', text }], isError };
}

async function callSchedule(action, args) {
    try {
        const res = await postJson('/schedule', {
            ...args,
            action,
            thread_id: process.env.LGY_THREAD_ID || '',
        });
        if (!res.ok)
            return toolText(res.error || 'LinkGravity could not update the schedule.', true);
        return toolText(JSON.stringify(res.jobs || res.job, null, 2));
    } catch (err) {
        return toolText(`Could not reach the LinkGravity daemon: ${err.message}`, true);
    }
}

async function callTool(params) {
    const args = params.arguments || {};
    if (SCHEDULE_ACTIONS[params.name]) return callSchedule(SCHEDULE_ACTIONS[params.name], args);
    if (params.name !== 'send_attachment') {
        return toolText(`Unknown tool: ${params.name}`, true);
    }
    if (!args.path) return toolText('The "path" argument is required.', true);

    try {
        const res = await postJson('/attach', {
            thread_id: process.env.LGY_THREAD_ID || '',
            path: args.path,
            caption: args.caption || '',
        });
        if (res.ok) return toolText(res.message || 'Attachment sent to the user.');
        return toolText(res.error || 'LinkGravity could not send the attachment.', true);
    } catch (err) {
        return toolText(`Could not reach the LinkGravity daemon: ${err.message}`, true);
    }
}

async function handle(msg) {
    const { id, method, params } = msg;
    if (method === 'initialize') {
        const requested = params && params.protocolVersion;
        return result(id, {
            protocolVersion: SUPPORTED_VERSIONS.includes(requested)
                ? requested
                : SUPPORTED_VERSIONS[0],
            capabilities: { tools: {} },
            serverInfo: { name: 'linkgravity', version: '1.0.0' },
            ...(process.env.LGY_THREAD_ID ? { instructions: CHAT_INSTRUCTIONS } : {}),
        });
    }
    if (method === 'tools/list') return result(id, { tools: TOOLS });
    if (method === 'tools/call') return result(id, await callTool(params || {}));
    if (method === 'ping') return result(id, {});
    if (method && method.startsWith('notifications/')) return;
    if (id !== undefined) {
        send({
            jsonrpc: '2.0',
            id,
            error: { code: -32601, message: `Method not found: ${method}` },
        });
    }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('close', () => process.exit(0));
// agy stops MCP servers with SIGABRT, whose default action leaves a core dump in agy's cwd.
for (const sig of ['SIGABRT', 'SIGTERM', 'SIGHUP']) process.on(sig, () => process.exit(0));
rl.on('line', async (line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg;
    try {
        msg = JSON.parse(trimmed);
    } catch {
        return;
    }
    try {
        await handle(msg);
    } catch (err) {
        if (msg.id !== undefined) {
            send({
                jsonrpc: '2.0',
                id: msg.id,
                error: { code: -32603, message: String(err.message || err) },
            });
        }
    }
});
