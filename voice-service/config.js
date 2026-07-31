const fs = require('fs');
const os = require('os');
const path = require('path');

const aglJsonPath = path.join(os.homedir(), '.gemini', 'linkgravity', 'lgy.json');
let aglConfig = {};
try {
    if (fs.existsSync(aglJsonPath)) {
        aglConfig = JSON.parse(fs.readFileSync(aglJsonPath, 'utf8'));
    }
} catch (e) {
    console.error('Failed to load lgy.json:', e.message);
}

process.env.DISCORD_TOKEN =
    aglConfig.discord_token || aglConfig.DISCORD_TOKEN || process.env.DISCORD_TOKEN;

process.env.http_proxy = '';
process.env.https_proxy = '';
process.env.HTTP_PROXY = '';
process.env.HTTPS_PROXY = '';
process.env.PYTHON_HOST = '';

module.exports = { aglConfig };
