const express = require('express');
const { joinVoiceChannel, VoiceConnectionStatus } = require('@discordjs/voice');
const state = require('./state');
const { setupReceiver } = require('./receiver');
const { interruptTTS, playNextInQueue } = require('./tts');
const { loadRustpotterModule } = require('./wakeword');

function registerRoutes(app, client) {
    app.get('/health', (req, res) => {
        res.json({ ready: client.isReady() });
    });

    app.post('/join', async (req, res) => {
        const { guild_id, channel_id } = req.body;
        try {
            const guild = client.guilds.cache.get(guild_id);
            if (!guild) return res.status(404).json({ error: 'Guild not found' });

            let connection = joinVoiceChannel({
                channelId: channel_id,
                guildId: guild_id,
                adapterCreator: guild.voiceAdapterCreator,
                selfDeaf: false,
                selfMute: false,
            });

            state.connections.set(guild_id, connection);

            setupReceiver(connection, guild_id, client);

            connection.removeAllListeners(VoiceConnectionStatus.Ready);
            connection.on(VoiceConnectionStatus.Ready, () => {
                console.log(`[Voice] Connected to ${channel_id} in ${guild_id}`);
            });

            res.json({ success: true });
        } catch (e) {
            console.error(e);
            res.status(500).json({ error: e.message });
        }
    });

    app.post('/leave', (req, res) => {
        const { guild_id } = req.body;
        const connection = state.connections.get(guild_id);
        if (!connection) {
            return res.status(404).json({ error: 'Not connected' });
        }

        const player = state.players.get(guild_id);
        if (player) {
            try {
                player.stop(true);
            } catch (e) {
                // already stopped/destroyed - fine
            }
        }
        connection.destroy();

        // Without this, a stale isPlaying/players entry silently breaks STT/wake detection on the next /join.
        state.connections.delete(guild_id);
        state.players.delete(guild_id);
        state.audioQueues.delete(guild_id);
        state.isPlaying.delete(guild_id);
        state.activeUntil.delete(guild_id);
        state.suppressNotifyMap.delete(guild_id);

        res.json({ success: true });
    });

    app.post(
        '/play',
        express.raw({ type: 'application/octet-stream', limit: '20mb' }),
        (req, res) => {
            const guild_id = req.query.guild_id;
            const connection = state.connections.get(guild_id);
            if (!connection) return res.status(404).json({ error: 'Not connected' });

            if (!state.audioQueues.has(guild_id)) {
                state.audioQueues.set(guild_id, []);
            }

            state.audioQueues.get(guild_id).push({
                buffer: req.body, // req.body is a Buffer here
                suppressActiveWindow: req.query.suppress_active_window === 'true',
            });

            if (!state.isPlaying.get(guild_id)) {
                playNextInQueue(guild_id);
            }

            res.json({ success: true, queued: true });
        },
    );

    app.post('/interrupt', (req, res) => {
        const { guild_id } = req.body;
        // Lets Python trigger the same cutoff the VAD loud-voice check uses, regardless of volume.
        interruptTTS(guild_id);
        res.json({ success: true });
    });

    app.post('/invalidate_detector', (req, res) => {
        // Without this, detectorCache keeps serving the OLD .rpw after a user re-enrolls.
        const { user_id } = req.body;
        const deleted = state.detectorCache.delete(user_id);
        console.log(`[Wake] Invalidated cached detector for ${user_id} (was cached: ${deleted})`);
        res.json({ success: true, was_cached: deleted });
    });

    app.post('/build_wakeword', async (req, res) => {
        // Builds a .rpw in-process via WakewordRefCreator, instead of shelling out to rustpotter-cli.
        try {
            const { name, samples } = req.body;
            if (!name || !Array.isArray(samples) || samples.length === 0) {
                return res
                    .status(400)
                    .json({ error: 'name and at least one sample (wav bytes) are required' });
            }

            const mod = await loadRustpotterModule();
            const creator = mod.WakewordRefCreator.new(name);
            try {
                for (const sample of samples) {
                    const buf = Buffer.from(sample.data_base64, 'base64');
                    creator.addFile(sample.filename || `${name}.wav`, buf);
                }
                const rpwBytes = creator.saveToBytes();
                console.log(
                    `[Wake] Built .rpw for '${name}' from ${samples.length} sample(s) via WakewordRefCreator`,
                );
                res.json({ success: true, rpw_base64: Buffer.from(rpwBytes).toString('base64') });
            } finally {
                creator.free();
            }
        } catch (e) {
            console.error(`[Wake] Failed to build wakeword reference:`, e);
            res.status(500).json({ error: e.message || String(e) });
        }
    });

    app.post('/set_config', (req, res) => {
        const { voice_threshold } = req.body;
        if (voice_threshold) {
            state.runtime.vadThreshold = voice_threshold;
            console.log(`[Config] Updated VAD threshold to ${state.runtime.vadThreshold}`);
        }
        res.json({ success: true });
    });

    app.post('/enroll_start', (req, res) => {
        const { user_id } = req.body;
        if (!user_id) return res.status(400).json({ error: 'user_id required' });
        state.enrollingUsers.add(user_id);
        res.json({ success: true });
    });

    app.post('/enroll_stop', (req, res) => {
        const { user_id } = req.body;
        if (!user_id) return res.status(400).json({ error: 'user_id required' });
        state.enrollingUsers.delete(user_id);
        res.json({ success: true });
    });

    app.post('/set_active', (req, res) => {
        const { guild_id, active_until } = req.body;
        if (!guild_id || !active_until)
            return res.status(400).json({ error: 'guild_id and active_until required' });
        state.activeUntil.set(guild_id, active_until);
        res.json({ success: true });
    });

    app.post('/set_wake_word_required', (req, res) => {
        const { user_id, required } = req.body;
        if (!user_id) return res.status(400).json({ error: 'user_id required' });
        if (required) state.wakeWordOptedOut.delete(user_id);
        else state.wakeWordOptedOut.add(user_id);
        res.json({ success: true });
    });
}

module.exports = { registerRoutes };
