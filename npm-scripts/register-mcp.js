'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const { repoRoot, workspaceDir } = require('./venv-paths');

const mcpJsonPath = path.join(os.homedir(), '.gemini', 'config', 'mcp_config.json');

// Copied out of node_modules and run via 'node', for the same reasons as register-hook.js.
const NODE_CMD = 'node';
const SERVER_NAME = 'linkgravity';
const FILE_NAME = 'linkgravity_mcp.js';
const installedMcpDir = path.join(workspaceDir, 'mcp');
// agy cuts MCP tool calls off after 3 minutes by default, and schedule_create waits for the user's button picks.
const CALL_TIMEOUT_SEC = 86400;

function installMcpScript() {
    const source = fs.readFileSync(path.join(repoRoot, 'mcp', FILE_NAME), 'utf8');
    const installedPath = path.join(installedMcpDir, FILE_NAME);
    let installed = null;
    try {
        installed = fs.readFileSync(installedPath, 'utf8');
    } catch {}
    if (installed !== source) {
        try {
            fs.mkdirSync(installedMcpDir, { recursive: true });
            fs.writeFileSync(installedPath, source);
        } catch (err) {
            if (installed === null) throw err;
            console.log(`⚠️  Couldn't refresh ${installedPath}: ${err.message}`);
        }
    }
    return installedPath;
}

function registerMcp({ quiet = false } = {}) {
    let installedPath;
    try {
        installedPath = installMcpScript();
    } catch (err) {
        console.log(`⚠️  Couldn't install the MCP server script: ${err.message}`);
        return false;
    }

    let config = {};
    if (fs.existsSync(mcpJsonPath)) {
        try {
            config = JSON.parse(fs.readFileSync(mcpJsonPath, 'utf8').replace(/^\uFEFF/, '')) || {};
        } catch (err) {
            const backupPath = `${mcpJsonPath}.corrupted-${Date.now()}`;
            fs.copyFileSync(mcpJsonPath, backupPath);
            console.log(
                `⚠️  ${mcpJsonPath} was invalid JSON - copied to ${backupPath} before rewriting it.`,
            );
            config = {};
        }
    }

    config.mcpServers = config.mcpServers || {};
    const desired = { command: NODE_CMD, args: [installedPath], timeoutSeconds: CALL_TIMEOUT_SEC };
    const existing = config.mcpServers[SERVER_NAME];
    if (
        existing &&
        existing.command === desired.command &&
        JSON.stringify(existing.args) === JSON.stringify(desired.args) &&
        existing.timeoutSeconds === desired.timeoutSeconds
    ) {
        if (!quiet)
            console.log(
                `🔌 agy MCP server '${SERVER_NAME}' already up to date -> ${installedPath}`,
            );
        return true;
    }

    try {
        fs.mkdirSync(path.dirname(mcpJsonPath), { recursive: true });
        if (existing) fs.copyFileSync(mcpJsonPath, `${mcpJsonPath}.bak`);
        config.mcpServers[SERVER_NAME] = desired;
        fs.writeFileSync(mcpJsonPath, JSON.stringify(config, null, 2) + '\n');
        console.log(`🔌 Registered agy MCP server '${SERVER_NAME}' -> ${installedPath}`);
        return true;
    } catch (err) {
        console.log(`⚠️  Couldn't write ${mcpJsonPath}: ${err.message}`);
        return false;
    }
}

module.exports = registerMcp;
module.exports.registerMcp = registerMcp;
