const p = require('@clack/prompts');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');
const { python: pythonExe } = require('../npm-scripts/venv-paths');

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

        const channelIds = [];
        let isFirstChannel = true;
        while (true) {
            const channelId = await p.text({
                message: isFirstChannel
                    ? 'Restrict to a specific channel in this server? Right-click a CHANNEL → Copy Channel ID. ' +
                      '(leave empty to allow the WHOLE server):'
                    : 'Another channel ID to restrict to (leave empty if done):',
            });
            if (p.isCancel(channelId)) {
                p.cancel('Setup cancelled.');
                process.exit(0);
            }
            if (!channelId) break;
            isFirstChannel = false;
            channelIds.push(channelId.trim());

            const addAnotherChannel = await p.confirm({
                message: 'Add another channel?',
                initialValue: false,
            });
            if (p.isCancel(addAnotherChannel)) {
                p.cancel('Setup cancelled.');
                process.exit(0);
            }
            if (!addAnotherChannel) break;
        }

        scopes.push({
            guild_id: guildId.trim(),
            channel_ids: channelIds,
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

async function collectUserIds(existingIds, platformLabel) {
    const ids = [];
    const hasExisting = existingIds && existingIds.length > 0;

    p.note(
        'ONLY these users can use the bot (leave completely empty on first setup to allow EVERYONE). ' +
            'Not related to DMs - this only gates the channel/threads configured above.',
        `Allowed ${platformLabel} Users`,
    );

    let isFirst = true;
    while (true) {
        const promptSuffix =
            isFirst && hasExisting
                ? ' (leave empty to keep your current allowed-user settings entirely unchanged)'
                : ' (leave empty if you have no more users to add)';

        const userId = await p.text({
            message: `${platformLabel} User ID to allow${promptSuffix}:`,
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

function startOrRestartDaemon(pm2Name, scriptPath, label) {
    console.log(`${color.cyan}▶${color.reset} Restarting ${label} daemon to apply changes...`);
    const restartResult = spawnSync('npx', ['-y', 'pm2', 'restart', pm2Name, '--update-env'], {
        stdio: 'pipe',
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    if (restartResult.status === 0) {
        p.outro(`${label} daemon restarted.`);
        return;
    }

    const stderr = (restartResult.stderr || '').toString();
    if (stderr.includes('not found')) {
        // Nothing to restart yet - start it instead of a false "restarted".
        const startResult = spawnSync(
            'npx',
            ['-y', 'pm2', 'start', scriptPath, '--interpreter', pythonExe, '--name', pm2Name],
            { stdio: 'pipe', env: { ...process.env, PYTHONUNBUFFERED: '1' } },
        );
        if (startResult.status === 0) {
            p.outro(`${label} daemon wasn't running yet - started it fresh instead.`);
        } else {
            console.error((startResult.stderr || '').toString().trim());
            p.outro(
                `${color.yellow}⚠${color.reset} Failed to start the ${label} daemon - run \`lgy start\` manually to see the full error.`,
            );
        }
    } else {
        console.error(stderr.trim());
        p.outro(
            `${color.yellow}⚠${color.reset} Failed to restart the ${label} daemon - run \`lgy restart\` manually to see the full error.`,
        );
    }
}

async function runSetup() {
    console.log();
    p.intro(`${color.cyan}▶ LinkGravity Setup Wizard${color.reset}`);

    const existingSettings = getSettings();

    const platforms = await p.multiselect({
        message: 'Which messenger platform(s) would you like to configure?',
        options: [
            { label: 'Discord', value: 'discord', hint: 'Slash commands, threads, voice' },
            { label: 'Telegram', value: 'telegram', hint: '1 chat = 1 session, no voice yet' },
        ],
        required: true,
    });
    if (p.isCancel(platforms)) {
        p.cancel('Setup cancelled.');
        process.exit(0);
    }

    const settingsUpdates = {};

    if (platforms.includes('discord')) {
        const discordToken = await p.password({
            message: 'Discord Bot Token (Leave empty to keep current):',
        });
        if (p.isCancel(discordToken)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }

        const sessionScopes = await collectSessionScopes(existingSettings.session_scopes);

        const existingUserIds = existingSettings.allowed_user_ids
            ? splitIds(existingSettings.allowed_user_ids)
            : [];
        const userIds = await collectUserIds(existingUserIds, 'Discord');

        if (discordToken) settingsUpdates.discord_token = discordToken;
        if (sessionScopes !== null) settingsUpdates.session_scopes = sessionScopes;
        if (userIds !== null) settingsUpdates.allowed_user_ids = userIds.join(',');

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
                            {
                                label: 'ko-KR-InJoonNeural (Korean Male)',
                                value: 'ko-KR-InJoonNeural',
                            },
                            {
                                label: 'en-US-AriaNeural (English Female)',
                                value: 'en-US-AriaNeural',
                            },
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
                            {
                                label: 'en-GB-RyanNeural (English Male, UK)',
                                value: 'en-GB-RyanNeural',
                            },
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
                            {
                                label: 'ja-JP-KeitaNeural (Japanese Male)',
                                value: 'ja-JP-KeitaNeural',
                            },
                            {
                                label: 'fr-FR-DeniseNeural (French Female)',
                                value: 'fr-FR-DeniseNeural',
                            },
                            {
                                label: 'de-DE-KatjaNeural (German Female)',
                                value: 'de-DE-KatjaNeural',
                            },
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

        if (group.tts_voice) settingsUpdates.tts_voice = group.tts_voice;
    }

    if (platforms.includes('telegram')) {
        const telegramToken = await p.password({
            message: 'Telegram Bot Token (from @BotFather, leave empty to keep current):',
        });
        if (p.isCancel(telegramToken)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }

        p.note(
            'Telegram has no channel/server gating yet - every chat you DM (or add) the bot to becomes ' +
                'its own session, and there is no voice support yet.',
            'Telegram Access',
        );

        const existingTelegramUserIds = existingSettings.telegram_allowed_user_ids
            ? splitIds(existingSettings.telegram_allowed_user_ids)
            : [];
        const telegramUserIds = await collectUserIds(existingTelegramUserIds, 'Telegram');

        if (telegramToken) settingsUpdates.telegram_token = telegramToken;
        if (telegramUserIds !== null)
            settingsUpdates.telegram_allowed_user_ids = telegramUserIds.join(',');
    }

    if (Object.keys(settingsUpdates).length > 0) {
        updateSettings(settingsUpdates);
    }

    p.note('Configuration saved to lgy.json successfully!', 'Success');

    if (platforms.includes('discord') && platforms.includes('telegram')) {
        p.note(
            'Both platforms share one tool-approval webhook port (18080), so running both daemons at ' +
                'the same time means only one of them can receive approval callbacks right now. Safe to ' +
                'run either one alone; running both simultaneously is not supported yet.',
            'Known limitation',
        );
    }

    if (platforms.includes('discord')) {
        startOrRestartDaemon('lgy', path.join(__dirname, '..', 'src', 'main.py'), 'Discord');
    }
    if (platforms.includes('telegram')) {
        startOrRestartDaemon(
            'lgy-telegram',
            path.join(__dirname, '..', 'src', 'main_telegram.py'),
            'Telegram',
        );
    }
}

module.exports = runSetup;
