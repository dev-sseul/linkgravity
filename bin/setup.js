const p = require('@clack/prompts');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

const color = {
    reset: '\x1b[0m',
    green: '\x1b[32m',
    cyan: '\x1b[36m',
    yellow: '\x1b[33m',
};

const workspaceDir = path.join(os.homedir(), '.gemini', 'linkgravity');
const settingsPath = path.join(workspaceDir, 'lgy.json');

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

function splitIds(raw) {
    return raw.split(/[\s,;]+/).filter(Boolean);
}

async function collectSessionScopes(existingScopes) {
    const scopes = [];
    const hasExisting = existingScopes && existingScopes.length > 0;

    p.note(
        'A new AI session can only be started with the /new command in Discord - never just by ' +
            'typing a message. This step controls WHERE /new is allowed to work.\n\n' +
            'For each server: leave "channels" empty to allow /new in EVERY channel of that server, ' +
            'or list specific channel IDs to restrict it to just those.',
        'Server / Channel Access',
    );

    let isFirst = true;
    while (true) {
        const promptSuffix =
            isFirst && hasExisting
                ? ' (leave empty to keep your current server/channel settings entirely unchanged)'
                : ' (leave empty if you have no more servers to add)';

        const guildId = await p.text({
            message: `Server (Guild) ID to allow${promptSuffix}. Right-click the SERVER NAME (not a channel) → Copy Server ID:`,
        });
        if (p.isCancel(guildId)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }

        if (!guildId) {
            if (isFirst) return null; // signal: user wants to keep existing config untouched
            break;
        }
        isFirst = false;

        const channelIds = await p.text({
            message:
                'Restrict to specific channel ID(s) in this server? Right-click a CHANNEL → Copy Channel ID. ' +
                'Comma-separated, or leave empty to allow the WHOLE server:',
        });
        if (p.isCancel(channelIds)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }

        scopes.push({
            guild_id: guildId.trim(),
            channel_ids: channelIds ? splitIds(channelIds) : [],
        });

        const addAnother = await p.confirm({ message: 'Add another server?', initialValue: false });
        if (p.isCancel(addAnother)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }
        if (!addAnother) break;
    }

    return scopes;
}

async function collectUserIds(existingIds) {
    const ids = [];
    const hasExisting = existingIds && existingIds.length > 0;

    p.note(
        'ONLY these users can use the bot (leave completely empty on first setup to allow EVERYONE). ' +
            'Not related to DMs - this only gates the channel/threads configured above.',
        'Allowed Discord Users',
    );

    let isFirst = true;
    while (true) {
        const promptSuffix =
            isFirst && hasExisting
                ? ' (leave empty to keep your current allowed-user settings entirely unchanged)'
                : ' (leave empty if you have no more users to add)';

        const userId = await p.text({
            message: `Discord User ID to allow${promptSuffix}:`,
        });
        if (p.isCancel(userId)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }

        if (!userId) {
            if (isFirst) return null; // signal: keep existing config untouched
            break;
        }
        isFirst = false;

        ids.push(userId.trim());

        const addAnother = await p.confirm({ message: 'Add another user?', initialValue: false });
        if (p.isCancel(addAnother)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }
        if (!addAnother) break;
    }

    return ids;
}

async function runSetup() {
    console.log();
    p.intro(`${color.cyan}▶ LinkGravity Setup Wizard${color.reset}`);

    const platform = await p.select({
        message: 'Which messenger platform would you like to configure?',
        options: [{ label: 'Discord', value: 'discord', hint: 'Configure Discord bot settings' }],
    });
    if (p.isCancel(platform)) {
        p.cancel('Setup cancelled.');
        process.exit(0);
    }

    const discordToken = await p.password({
        message: 'Discord Bot Token (Leave empty to keep current):',
    });
    if (p.isCancel(discordToken)) {
        p.cancel('Setup cancelled.');
        process.exit(0);
    }

    const existingSettings = getSettings();
    const sessionScopes = await collectSessionScopes(existingSettings.session_scopes);

    const existingUserIds = existingSettings.allowed_user_ids
        ? splitIds(existingSettings.allowed_user_ids)
        : [];
    const userIds = await collectUserIds(existingUserIds);

    p.note(
        "Wake words aren't set here anymore - they need a voice recording to register " +
            "(so only your voice triggers them), which this terminal wizard can't do. " +
            'Set them from Discord with `/sound wake_words:<word>` once the bot is running.',
        'Wake Words',
    );

    const group = await p.group(
        {
            tts_voice: () =>
                p.select({
                    message:
                        'TTS Voice Model (Select default voice - you can change this anytime later in Discord via /sound, which lists many more):',
                    options: [
                        {
                            label: 'ko-KR-SunHiNeural (Korean Female - Default)',
                            value: 'ko-KR-SunHiNeural',
                        },
                        { label: 'ko-KR-InJoonNeural (Korean Male)', value: 'ko-KR-InJoonNeural' },
                        { label: 'en-US-AriaNeural (English Female)', value: 'en-US-AriaNeural' },
                        { label: 'en-US-GuyNeural (English Male)', value: 'en-US-GuyNeural' },
                        {
                            label: 'en-US-AnaNeural (English Female, child-like)',
                            value: 'en-US-AnaNeural',
                        },
                        {
                            label: 'en-US-ChristopherNeural (English Male)',
                            value: 'en-US-ChristopherNeural',
                        },
                        {
                            label: 'en-GB-SoniaNeural (English Female, UK)',
                            value: 'en-GB-SoniaNeural',
                        },
                        { label: 'en-GB-RyanNeural (English Male, UK)', value: 'en-GB-RyanNeural' },
                        {
                            label: 'en-AU-NatashaNeural (English Female, AU)',
                            value: 'en-AU-NatashaNeural',
                        },
                        {
                            label: 'en-AU-WilliamNeural (English Male, AU)',
                            value: 'en-AU-WilliamNeural',
                        },
                        {
                            label: 'ja-JP-NanamiNeural (Japanese Female)',
                            value: 'ja-JP-NanamiNeural',
                        },
                        { label: 'ja-JP-KeitaNeural (Japanese Male)', value: 'ja-JP-KeitaNeural' },
                        {
                            label: 'fr-FR-DeniseNeural (French Female)',
                            value: 'fr-FR-DeniseNeural',
                        },
                        { label: 'de-DE-KatjaNeural (German Female)', value: 'de-DE-KatjaNeural' },
                        {
                            label: 'es-ES-ElviraNeural (Spanish Female)',
                            value: 'es-ES-ElviraNeural',
                        },
                    ],
                }),
        },
        {
            onCancel: () => {
                p.cancel('Setup cancelled.');
                process.exit(0);
            },
        },
    );

    const settingsUpdates = {};
    if (discordToken) settingsUpdates.discord_token = discordToken;
    if (sessionScopes !== null) settingsUpdates.session_scopes = sessionScopes;
    if (userIds !== null) settingsUpdates.allowed_user_ids = userIds.join(',');
    if (group.tts_voice) settingsUpdates.tts_voice = group.tts_voice;

    if (Object.keys(settingsUpdates).length > 0) {
        updateSettings(settingsUpdates);
    }

    p.note('Configuration saved to lgy.json successfully!', 'Success');

    console.log(`${color.cyan}▶${color.reset} Restarting daemon to apply changes...`);
    spawnSync('npx', ['-y', 'pm2', 'restart', 'lgy', '--update-env'], {
        stdio: 'pipe',
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    p.outro('Daemon restarted.');
}

module.exports = runSetup;
