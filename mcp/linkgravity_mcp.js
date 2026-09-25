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
];

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

async function callTool(params) {
    if (params.name !== 'send_attachment') {
        return toolText(`Unknown tool: ${params.name}`, true);
    }
    const args = params.arguments || {};
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
