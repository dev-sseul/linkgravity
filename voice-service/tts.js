const { Readable } = require('stream');
const { createAudioPlayer, createAudioResource, AudioPlayerStatus } = require('@discordjs/voice');
const { players, audioQueues, isPlaying, connections, suppressNotifyMap } = require('./state');
const { aglConfig } = require('./config');

function interruptTTS(guildId) {
    const player = players.get(guildId);
    let interrupted = false;

    if (audioQueues.has(guildId)) {
        // Queue holds in-memory Buffers (see /play), not filepaths - nothing on disk to clean up.
        audioQueues.set(guildId, []);
    }

    if (player && player.state.status !== AudioPlayerStatus.Idle) {
        player.stop();
        console.log(`[VAD] Interrupted TTS in guild ${guildId}`);
        interrupted = true;
    }

    isPlaying.set(guildId, false);
    return interrupted;
}

async function notifyTtsFinished(guild_id) {
    try {
        await fetch('http://127.0.0.1:18080/tts_finished', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-LGY-Token': aglConfig.approve_token },
            body: JSON.stringify({ guild_id }),
        });
    } catch (err) {
        console.error(`[TTS] Failed to notify Python of playback completion:`, err.message);
    }
}

function playNextInQueue(guild_id) {
    const queue = audioQueues.get(guild_id) || [];
    if (queue.length === 0) {
        isPlaying.set(guild_id, false);
        if (!suppressNotifyMap.get(guild_id)) {
            notifyTtsFinished(guild_id);
        }
        return;
    }

    const connection = connections.get(guild_id);
    if (!connection) {
        isPlaying.set(guild_id, false);
        return;
    }

    isPlaying.set(guild_id, true);
    const item = queue.shift(); // { buffer, suppressActiveWindow }
    suppressNotifyMap.set(guild_id, item.suppressActiveWindow);

    try {
        let player = players.get(guild_id);
        if (!player) {
            player = createAudioPlayer();
            players.set(guild_id, player);
            connection.subscribe(player);

            player.on(AudioPlayerStatus.Idle, () => {
                playNextInQueue(guild_id);
            });

            player.on('error', (error) => {
                console.error(`[TTS] AudioPlayer Error:`, error.message);
                playNextInQueue(guild_id);
            });
        }

        const resource = createAudioResource(Readable.from(item.buffer));
        player.play(resource);
    } catch (e) {
        console.error(`[TTS] Error playing queued audio:`, e);
        playNextInQueue(guild_id);
    }
}

module.exports = { interruptTTS, notifyTtsFinished, playNextInQueue };
