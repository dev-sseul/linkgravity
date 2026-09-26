'use strict';
const { execSync } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { pip: venvPip, python: venvPython, workspaceDir, repoRoot } = require('./venv-paths');

const isWin = os.platform() === 'win32';
const pyCmd = isWin ? 'python' : 'python3';

const voiceServiceDir = path.join(repoRoot, 'voice-service');

function isVenvReady() {
    return fs.existsSync(venvPython);
}

function isVoiceServiceReady() {
    return fs.existsSync(path.join(voiceServiceDir, 'node_modules'));
}

const requirementsHashFile = path.join(workspaceDir, 'venv', '.requirements.sha256');

function requirementsHash() {
    return crypto
        .createHash('sha256')
        .update(fs.readFileSync(path.join(repoRoot, 'requirements.txt')))
        .digest('hex');
}

function areRequirementsInSync() {
    try {
        return fs.readFileSync(requirementsHashFile, 'utf8').trim() === requirementsHash();
    } catch {
        return false;
    }
}

function isEnvironmentReady() {
    return isVenvReady() && areRequirementsInSync() && isVoiceServiceReady();
}

function ensureEnvironment() {
    if (!isVenvReady()) {
        console.log('⚙️  Setting up Python Virtual Environment...');
        console.log(
            `   (in ${path.join(workspaceDir, 'venv')} - not inside this install, so it survives`,
        );
        console.log('    package updates/reinstalls and works the same whether this is a global');
        console.log('    `npm install -g linkgravity` or a local dev clone.)');

        fs.mkdirSync(workspaceDir, { recursive: true });
        execSync(`${pyCmd} -m venv "${path.join(workspaceDir, 'venv')}"`, { stdio: 'inherit' });

        console.log('📦 Installing Python dependencies...');
    }

    // The venv outlives package updates, so a dependency added later would otherwise never get installed.
    if (!areRequirementsInSync()) {
        if (fs.existsSync(requirementsHashFile))
            console.log('📦 Python dependencies changed - updating...');
        execSync(`"${venvPip}" install -r requirements.txt`, { stdio: 'inherit', cwd: repoRoot });
        fs.writeFileSync(requirementsHashFile, requirementsHash());
    }

    if (!isVoiceServiceReady()) {
        console.log('🎙️  Installing Voice Service dependencies...');
        execSync('npm install', { stdio: 'inherit', cwd: voiceServiceDir });
    }

    console.log('✅ Environment ready.');
}

module.exports = { ensureEnvironment, isEnvironmentReady };
