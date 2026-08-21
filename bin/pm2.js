const path = require('path');

// Not `npx pm2`: its `npm exec` + `sh -c` wrapping survives cp.kill() as a zombie, and LPM
// Firewall reads it as a runtime package install.
const PM2_BIN = require.resolve('pm2/bin/pm2');
const PM2_CWD = path.join(__dirname, '..');

function pm2Env() {
    return {
        ...process.env,
        // pm2 gives Python a pipe not a TTY, so it block-buffers stdout and can sit on log lines indefinitely - force line buffering.
        PYTHONUNBUFFERED: '1',
        // Without this Python inherits the console codepage (cp949 on Korean Windows) and loguru
        // drops every line it can't encode - including the one verifyStartup waits for.
        PYTHONIOENCODING: 'utf-8',
        // pm2 merges --update-env rather than replacing, so a LOG_LEVEL from an earlier run
        // survives unless a value is passed every time.
        LOG_LEVEL: process.env.LOG_LEVEL || 'INFO',
        // Version managers (fnm, nvm) put node on PATH from a shell hook the daemon never runs,
        // so the bot's own `node` lookup for voice-service would fail without this.
        PATH: `${path.dirname(process.execPath)}${path.delimiter}${process.env.PATH || ''}`,
    };
}

module.exports = { PM2_BIN, PM2_CWD, pm2Env };
