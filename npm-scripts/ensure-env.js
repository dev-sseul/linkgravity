'use strict';
const { execSync } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');
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

function isEnvironmentReady() {
    return isVenvReady() && isVoiceServiceReady();
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
        execSync(`"${venvPip}" install -r requirements.txt`, { stdio: 'inherit', cwd: repoRoot });
    }

    if (!isVoiceServiceReady()) {
        console.log('🎙️  Installing Voice Service dependencies...');
        execSync('npm install', { stdio: 'inherit', cwd: voiceServiceDir });
    }

    console.log('✅ Environment ready.');
}

module.exports = { ensureEnvironment, isEnvironmentReady };
