const fs = require('fs');
const path = require('path');
const os = require('os');

const workspaceDir = path.join(os.homedir(), '.gemini', 'linkgravity');
const settingsPath = path.join(workspaceDir, 'lgy.json');
const sessionsPath = path.join(workspaceDir, 'data', 'sessions.json');

if (!fs.existsSync(workspaceDir)) fs.mkdirSync(workspaceDir, { recursive: true });

function getSettings() {
    if (fs.existsSync(settingsPath)) {
        try {
            return JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
        } catch (e) {}
    }
    return {};
}

function updateSettings(updates) {
    const settings = getSettings();
    for (const [key, value] of Object.entries(updates)) {
        settings[key] = value;
    }
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 4));
}

function getSessions() {
    if (fs.existsSync(sessionsPath)) {
        try {
            return JSON.parse(fs.readFileSync(sessionsPath, 'utf8'));
        } catch (e) {}
    }
    return {};
}

const PLATFORMS = {
    discord: {
        label: 'Discord',
        pm2Name: 'lgy-discord',
        scriptPath: path.join(__dirname, '..', 'src', 'main.py'),
    },
    telegram: {
        label: 'Telegram',
        pm2Name: 'lgy-telegram',
        scriptPath: path.join(__dirname, '..', 'src', 'main_telegram.py'),
    },
};

function platformState(key, settings) {
    const configured = !!settings[`${key}_token`];
    const enabled = settings[`${key}_enabled`] ?? (key === 'discord' && configured);
    return { configured, enabled };
}

module.exports = {
    workspaceDir,
    settingsPath,
    sessionsPath,
    getSettings,
    updateSettings,
    getSessions,
    PLATFORMS,
    platformState,
};
