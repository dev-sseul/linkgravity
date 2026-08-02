const p = require('@clack/prompts');
const { spawnSync } = require('child_process');
const { python: pythonExe } = require('../npm-scripts/venv-paths');
const {
    getSettings,
    updateSettings,
    PLATFORMS,
    platformState,
    LGY_PM2_NAME,
    LGY_SCRIPT_PATH,
} = require('./platforms');

const color = {
    reset: '\x1b[0m',
    green: '\x1b[32m',
    cyan: '\x1b[36m',
    yellow: '\x1b[33m',
};

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

    if (hasExisting) {
        const summary = existingScopes
            .map(
                (s) =>
                    `${s.guild_id}${s.channel_ids.length ? ` (channels: ${s.channel_ids.join(', ')})` : ' (whole server)'}`,
            )
            .join('; ');
        p.note(`Current server/channel settings: ${summary}`, 'Current Setting');

        const change = await p.confirm({
            message: 'Change the server/channel access list?',
            initialValue: false,
        });
        if (p.isCancel(change)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }
        if (!change) return null; // keep existing config untouched
    }

    let isFirst = true;
    while (true) {
        const guildId = await p.text({
            message: isFirst
                ? 'Server (Guild) ID to allow. Right-click the SERVER NAME (not a channel) → Copy Server ID ' +
                  '(leave empty if done - an empty list means /new works nowhere):'
                : 'Another server ID to add (leave empty if done):',
        });
        if (p.isCancel(guildId)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }
        if (!guildId) break;
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
        'ONLY these users can use the bot (leave completely empty to allow EVERYONE). ' +
            'Not related to DMs - this only gates the channel/threads configured above.',
        `Allowed ${platformLabel} Users`,
    );

    if (hasExisting) {
        p.note(
            `Current allowed ${platformLabel} users: ${existingIds.join(', ')}`,
            'Current Setting',
        );

        const change = await p.confirm({
            message: `Change the allowed-user list for ${platformLabel}?`,
            initialValue: false,
        });
        if (p.isCancel(change)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }
        if (!change) return null; // keep existing config untouched
    }

    let isFirst = true;
    while (true) {
        const userId = await p.text({
            message: isFirst
                ? `${platformLabel} User ID to allow (leave empty to allow EVERYONE):`
                : `Another ${platformLabel} user ID to allow (leave empty if done):`,
        });
        if (p.isCancel(userId)) {
            p.cancel('Setup cancelled.');
            process.exit(0);
        }

        if (!userId) break;
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

function stopDaemon(pm2Name, label) {
    console.log(`${color.cyan}▶${color.reset} Stopping ${label} daemon...`);
    spawnSync('npx', ['-y', 'pm2', 'delete', pm2Name], { stdio: 'pipe' });

    const jlist = spawnSync('npx', ['-y', 'pm2', 'jlist'], { stdio: 'pipe' });
    let stillRunning = false;
    if (jlist.status === 0) {
        try {
            stillRunning = JSON.parse(jlist.stdout.toString()).some((p) => p.name === pm2Name);
        } catch (e) {}
    }

    if (!stillRunning) {
        p.outro(`${label} daemon stopped.`);
    } else {
        p.outro(
            `${color.yellow}⚠${color.reset} ${label} daemon is still running - run \`npx pm2 delete ${pm2Name}\` manually and check \`npx pm2 list\`.`,
        );
    }
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

async function configureDiscord(existingSettings) {
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

    const updates = {};
    if (discordToken) updates.discord_token = discordToken;
    if (sessionScopes !== null) updates.session_scopes = sessionScopes;
    if (userIds !== null) updates.allowed_user_ids = userIds.join(',');

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

    if (group.tts_voice) updates.tts_voice = group.tts_voice;
    return updates;
}

async function configureTelegram(existingSettings) {
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

    const updates = {};
    if (telegramToken) updates.telegram_token = telegramToken;
    if (telegramUserIds !== null) updates.telegram_allowed_user_ids = telegramUserIds.join(',');
    return updates;
}

async function configureSlack(existingSettings) {
    p.note(
        'Create a Slack app at api.slack.com/apps, enable Socket Mode, and add an app-level token with the ' +
            '`connections:write` scope (Basic Information → App-Level Tokens). The bot token (starts with xoxb-) ' +
            'is under OAuth & Permissions.',
        'Slack App Setup',
    );

    const slackBotToken = await p.password({
        message: 'Slack Bot Token (xoxb-..., leave empty to keep current):',
    });
    if (p.isCancel(slackBotToken)) {
        p.cancel('Setup cancelled.');
        process.exit(0);
    }

    const slackAppToken = await p.password({
        message: 'Slack App-Level Token (xapp-..., leave empty to keep current):',
    });
    if (p.isCancel(slackAppToken)) {
        p.cancel('Setup cancelled.');
        process.exit(0);
    }

    p.note(
        'Slack has no channel/server gating yet, and threads have no title surface (same as Telegram) - ' +
            'there is no voice support yet either.',
        'Slack Access',
    );

    const existingSlackUserIds = existingSettings.slack_allowed_user_ids
        ? splitIds(existingSettings.slack_allowed_user_ids)
        : [];
    const slackUserIds = await collectUserIds(existingSlackUserIds, 'Slack');

    const updates = {};
    if (slackBotToken) updates.slack_bot_token = slackBotToken;
    if (slackAppToken) updates.slack_app_token = slackAppToken;
    if (slackUserIds !== null) updates.slack_allowed_user_ids = slackUserIds.join(',');
    return updates;
}

const CONFIGURERS = {
    discord: configureDiscord,
    telegram: configureTelegram,
    slack: configureSlack,
};

function applyDaemonState() {
    const settings = getSettings();
    const anyEnabled = Object.keys(PLATFORMS).some((k) => platformState(k, settings).enabled);
    if (anyEnabled) {
        startOrRestartDaemon(LGY_PM2_NAME, LGY_SCRIPT_PATH, 'LinkGravity');
    } else {
        stopDaemon(LGY_PM2_NAME, 'LinkGravity');
    }
}

async function platformMenu(key) {
    const def = PLATFORMS[key];

    while (true) {
        const settings = getSettings();
        const { configured, enabled } = platformState(key, settings);

        const options = [];
        options.push(
            enabled
                ? { value: 'off', label: 'Turn OFF' }
                : { value: 'on', label: configured ? 'Turn ON' : 'Configure & turn ON' },
        );
        if (configured)
            options.push({ value: 'edit', label: 'Edit settings (token, access, etc.)' });

        const action = await p.select({
            message: `${def.label} — currently ${enabled ? 'ON' : 'OFF'}${configured ? '' : ' (not configured)'} (Esc to go back)`,
            options,
        });
        if (p.isCancel(action)) return;

        if (action === 'off') {
            updateSettings({ [`${key}_enabled`]: false });
            applyDaemonState();
            continue;
        }

        if (action === 'on' && configured) {
            updateSettings({ [`${key}_enabled`]: true });
            applyDaemonState();
            continue;
        }

        // action === 'edit', or first-time 'on' (not configured yet) - both need the full wizard.
        const updates = await CONFIGURERS[key](settings);
        updates[`${key}_enabled`] = true;
        updateSettings(updates);
        applyDaemonState();
    }
}

async function runSetup() {
    console.log();
    p.intro(`${color.cyan}▶ LinkGravity Setup Wizard${color.reset}`);

    while (true) {
        const settings = getSettings();
        const options = Object.entries(PLATFORMS).map(([key, def]) => {
            const { configured, enabled } = platformState(key, settings);
            return {
                value: key,
                label: `${def.label} — ${enabled ? 'ON' : 'OFF'}`,
                hint: configured ? undefined : 'not configured yet',
            };
        });
        const choice = await p.select({ message: 'LinkGravity Setup (Esc to finish)', options });
        if (p.isCancel(choice)) break;

        await platformMenu(choice);
    }

    p.outro('Setup complete.');
}

module.exports = runSetup;
