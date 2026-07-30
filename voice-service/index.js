const { Client, GatewayIntentBits, Events } = require('discord.js');

const originalLog = console.log;
const originalError = console.error;

function getTimestamp() {
    const now = new Date();
    const offset = now.getTimezoneOffset() * 60000;
    const localTime = new Date(now.getTime() - offset);
    return localTime.toISOString().replace('T', ' ').substring(0, 19);
}

console.log = function (...args) {
    originalLog(`${getTimestamp()} INFO  Voice:`, ...args);
};
console.error = function (...args) {
    originalError(`${getTimestamp()} ERROR Voice:`, ...args);
};
const {
    joinVoiceChannel,
    createAudioPlayer,
    createAudioResource,
    AudioPlayerStatus,
    EndBehaviorType,
    VoiceConnectionStatus,
} = require('@discordjs/voice');

const express = require('express');
const axios = require('axios');
const os = require('os');
const prism = require('prism-media');
const fs = require('fs');
const path = require('path');
const { Readable } = require('stream');
const { spawn } = require('child_process');
const ffmpegPath = require('ffmpeg-static');

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

const app = express();
app.use(express.json({ limit: '50mb' }));

let vadThreshold = 3000;
if (aglConfig.voice_threshold) {
    vadThreshold = parseInt(aglConfig.voice_threshold) || 3000;
}

const client = new Client({
    intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

const connections = new Map();
const players = new Map();

function stereoToMono(buffer) {
    // Discord voice receive is 48kHz stereo; everything downstream (WAV, Rustpotter) expects mono.
    const samples = buffer.length >> 2; // 2 bytes/sample * 2 channels
    const mono = Buffer.alloc(samples * 2);
    for (let i = 0; i < samples; i++) {
        const l = buffer.readInt16LE(i * 4);
        const r = buffer.readInt16LE(i * 4 + 2);
        mono.writeInt16LE((l + r) >> 1, i * 2);
    }
    return mono;
}

function createWavHeader(dataLength, sampleRate = 48000, channels = 1, bitDepth = 16) {
    const buffer = Buffer.alloc(44);
    buffer.write('RIFF', 0);
    buffer.writeUInt32LE(36 + dataLength, 4);
    buffer.write('WAVE', 8);
    buffer.write('fmt ', 12);
    buffer.writeUInt32LE(16, 16);
    buffer.writeUInt16LE(1, 20);
    buffer.writeUInt16LE(channels, 22);
    buffer.writeUInt32LE(sampleRate, 24);
    buffer.writeUInt32LE(sampleRate * channels * (bitDepth / 8), 28);
    buffer.writeUInt16LE(channels * (bitDepth / 8), 32);
    buffer.writeUInt16LE(bitDepth, 34);
    buffer.write('data', 36);
    buffer.writeUInt32LE(dataLength, 40);
    return buffer;
}

client.once(Events.ClientReady, () => {
    console.log(`🎤 Node.js Voice Microservice is online as ${client.user.tag}`);
});

// Unofficial Google speech API endpoint/key - same one Python's SpeechRecognition library
// (recognize_google) ships as its default; publicly known but could be rate-limited/changed anytime.
const GOOGLE_STT_KEY = 'AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw';

function flacEncode(wavBuffer) {
    return new Promise((resolve, reject) => {
        const ff = spawn(ffmpegPath, [
            '-hide_banner',
            '-loglevel',
            'error',
            '-i',
            'pipe:0',
            '-f',
            'flac',
            'pipe:1',
        ]);
        const out = [];
        ff.stdout.on('data', (d) => out.push(d));
        ff.stderr.on('data', () => {}); // -loglevel error already keeps this quiet in the normal case
        ff.on('error', (err) => reject(new Error(`ffmpeg-static failed to run (${err.message})`)));
        ff.on('close', (code) => {
            if (code !== 0) return reject(new Error(`ffmpeg exited with code ${code}`));
            resolve(Buffer.concat(out));
        });
        ff.stdin.write(wavBuffer);
        ff.stdin.end();
    });
}

async function googleSTT(wavBuffer, lang = 'ko-KR') {
    let flacBuffer;
    try {
        flacBuffer = await flacEncode(wavBuffer);
    } catch (err) {
        console.error('[STT] FLAC encode failed:', err.message);
        return null;
    }

    let res;
    try {
        res = await fetch(
            `https://www.google.com/speech-api/v2/recognize?output=json&client=chromium&lang=${encodeURIComponent(lang)}&key=${GOOGLE_STT_KEY}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'audio/x-flac; rate=48000' },
                body: flacBuffer,
            },
        );
    } catch (err) {
        console.error('[STT] Request to Google STT failed:', err.message);
        return null;
    }

    const raw = await res.text();
    // Response is newline-delimited JSON, one object per line.
    for (const line of raw.trim().split('\n')) {
        if (!line) continue;
        try {
            const obj = JSON.parse(line);
            const transcript = obj.result?.[0]?.alternative?.[0]?.transcript;
            if (transcript) return transcript.trim();
        } catch (e) {
            // not JSON / partial line - ignore
        }
    }
    return null;
}

const audioQueues = new Map();
const isPlaying = new Map();

function interruptTTS(guildId) {
    const player = players.get(guildId);
    let interrupted = false;

    if (audioQueues.has(guildId)) {
        // Queue now holds in-memory audio Buffers (see /play), not
        // filepaths, so there's nothing on disk to clean up here.
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

const activeStreams = new Map();

// user_id -> recording wake-word samples right now; routes to /enroll_sample instead of STT.
const enrollingUsers = new Set();

// guild_id -> ms epoch until the "awake, skip wake word" window closes (set via /set_active).
const activeUntil = new Map();

// user_id -> opted out of wake-word gating via /sound - scoped per-user, unlike activeUntil.
const wakeWordOptedOut = new Set();

function isGuildActive(guildId) {
    return Date.now() < (activeUntil.get(guildId) || 0);
}

// Rustpotter wake-word detection runs entirely in-process here, no Python round trip.
const WAKE_REF_DIR = path.join(os.homedir(), '.gemini', 'linkgravity', 'wake_refs');

let rustpotterModPromise = null;
function loadRustpotterModule() {
    if (!rustpotterModPromise) {
        rustpotterModPromise = (async () => {
            // Node's ESM loader needs the explicit entry file; "rustpotter-web" (not "-slim")
            // is used because it also exposes WakewordRefCreator, used by /build_wakeword below.
            const mod = await import('rustpotter-web/rustpotter_wasm.js');
            const wasmPath = require.resolve('rustpotter-web/rustpotter_wasm_bg.wasm');
            mod.initSync(fs.readFileSync(wasmPath));
            return mod;
        })();
    }
    return rustpotterModPromise;
}

// userId -> { rustpotter, samplesPerFrame, residual: Int16Array }
const detectorCache = new Map();

// Wake-word confirm cutoff - must stay well above ~0.05 (rustpotter's countdown never finalizes if noise/silence clears it too); 0.4 chosen after live use kept narrowly missing genuine hits just under 0.5.
const WAKE_MATCH_THRESHOLD = 0.4;

async function getDetectorForUser(userId) {
    if (detectorCache.has(userId)) return detectorCache.get(userId);

    const userDir = path.join(WAKE_REF_DIR, userId);
    if (!fs.existsSync(userDir)) return null; // not enrolled

    const rpwFile = fs.readdirSync(userDir).find((f) => f.endsWith('.rpw'));
    if (!rpwFile) return null; // samples exist but .rpw build hasn't happened/failed - see _commit_enrollment

    const mod = await loadRustpotterModule();
    const config = mod.RustpotterConfig.new();
    config.setSampleRate(48000);
    config.setSampleFormat(mod.SampleFormat.i16);
    config.setChannels(1);
    // Must stay a real cutoff (not near-zero) for rustpotter's confirm-after-N-frames logic to finalize.
    config.setThreshold(WAKE_MATCH_THRESHOLD);
    config.setAveragedThreshold(0);
    // Raised from default 1 so a candidate has to keep winning for a few frames before it's trusted.
    config.setMinScores(4);
    // Max (best of the 5 enrolled samples) beats Median here - real speech isn't consistent enough
    // for Median's "middle sample must also score well" requirement; minScores(4) compensates.
    config.setScoreMode(mod.ScoreMode.max);

    const rustpotter = mod.Rustpotter.new(config);
    rustpotter.addWakeword(rpwFile, fs.readFileSync(path.join(userDir, rpwFile)));

    // Diagnostic-only twin, fed the same audio, purely so "no match" logs show a real closeness
    // score - the real detector's own threshold hides sub-threshold scores entirely, and lowering
    // its threshold isn't safe (near-zero means noise keeps resetting the confirm countdown).
    // Never gates wake behavior; only entry.rustpotter above does.
    const diagConfig = mod.RustpotterConfig.new();
    diagConfig.setSampleRate(48000);
    diagConfig.setSampleFormat(mod.SampleFormat.i16);
    diagConfig.setChannels(1);
    diagConfig.setThreshold(0.01);
    diagConfig.setAveragedThreshold(0);
    diagConfig.setMinScores(1);
    diagConfig.setEager(true);
    diagConfig.setScoreMode(mod.ScoreMode.max);
    const diagRustpotter = mod.Rustpotter.new(diagConfig);
    diagRustpotter.addWakeword(rpwFile, fs.readFileSync(path.join(userDir, rpwFile)));

    const entry = {
        rustpotter,
        samplesPerFrame: rustpotter.getSamplesPerFrame(),
        residual: new Int16Array(0),
        diag: {
            rustpotter: diagRustpotter,
            samplesPerFrame: diagRustpotter.getSamplesPerFrame(),
            residual: new Int16Array(0),
        },
    };
    console.log(
        `[Wake] Loaded detector for ${userId} from ${rpwFile}: samplesPerFrame=${entry.samplesPerFrame}`,
    );
    detectorCache.set(userId, entry);
    return entry;
}

// Feeds a PCM chunk to a detector frame-aligned via residual carryover (Rustpotter needs a
// genuinely continuous stream). Returns a detection if a complete frame in this chunk triggered one.
function feedPCMToDetector(entry, chunk) {
    const incoming = new Int16Array(chunk.buffer, chunk.byteOffset, chunk.length / 2);
    let combined = incoming;
    if (entry.residual.length) {
        combined = new Int16Array(entry.residual.length + incoming.length);
        combined.set(entry.residual, 0);
        combined.set(incoming, entry.residual.length);
    }

    let offset = 0;
    let detection = null;
    while (combined.length - offset >= entry.samplesPerFrame) {
        const frame = combined.subarray(offset, offset + entry.samplesPerFrame);
        const result = entry.rustpotter.processI16(frame);
        if (result) detection = result;
        offset += entry.samplesPerFrame;
    }
    entry.residual = combined.subarray(offset);
    return detection;
}

function setupReceiver(connection, guildId) {
    const receiver = connection.receiver;

    receiver.speaking.removeAllListeners('start');

    receiver.speaking.on('start', (userId) => {
        if (client.user.id === userId) return;

        if (activeStreams.get(userId)) {
            return;
        }
        activeStreams.set(userId, true);

        let hasInterrupted = false;

        const opusStream = receiver.subscribe(userId, {
            end: {
                behavior: EndBehaviorType.Manual,
            },
        });
        const pcmStream = opusStream.pipe(
            new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 }),
        );

        // Without a listener, an unhandled 'error' event here crashes the ENTIRE process on one bad packet.
        opusStream.on('error', (err) => {
            console.error(`[Voice] Opus stream error for ${userId}:`, err.message);
            forceEndStream();
        });
        pcmStream.on('error', (err) => {
            console.error(
                `[Voice] Opus decode error for ${userId} (bad/corrupted packet):`,
                err.message,
            );
            forceEndStream();
        });

        const chunks = [];

        let hasEnded = false;

        const forceEndStream = () => {
            if (hasEnded) return;
            try {
                opusStream.destroy();
            } catch (e) {}
            try {
                pcmStream.destroy();
            } catch (e) {}
            pcmStream.emit('end');
        };

        const maxDurationTimer = setTimeout(forceEndStream, 30000);

        // Rustpotter runs in-process here, no Python round trip - detectors cached per user.
        let wakeConfirmed = false;
        let matchedWakeWord = null;
        let detectorEntry = null;
        let bestWakeScore = 0;
        let bestWakeScoreName = null;
        let bestDiagScore = 0;
        let bestDiagScoreName = null;

        if (!enrollingUsers.has(userId) && !isGuildActive(guildId)) {
            getDetectorForUser(userId)
                .then((entry) => {
                    if (entry) {
                        entry.rustpotter.reset();
                        entry.residual = new Int16Array(0);
                        entry.diag.rustpotter.reset();
                        entry.diag.residual = new Int16Array(0);
                        detectorEntry = entry;
                    }
                })
                .catch((err) =>
                    console.error(`[Wake] Failed to load detector for ${userId}:`, err.message),
                );
        }

        // Only runs during the active/awake window, not on every VAD sound - reuses the same
        // googleSTT() call utterance-end uses anyway, just invoked earlier for live feedback.
        const PARTIAL_INTERVAL_MS = 1500;
        const PARTIAL_MIN_NEW_BYTES = 24000;
        let lastPartialLength = 0;
        let partialSent = false;
        const partialTimer = setInterval(async () => {
            if (hasEnded || !isSpeaking || enrollingUsers.has(userId)) return;
            if (!isGuildActive(guildId)) return;

            const currentLength = chunks.reduce((sum, c) => sum + c.length, 0);
            if (currentLength - lastPartialLength < PARTIAL_MIN_NEW_BYTES) return;
            lastPartialLength = currentLength;

            const windowPcm = Buffer.concat(chunks);
            const wavHeader = createWavHeader(windowPcm.length);
            const wavBuffer = Buffer.concat([wavHeader, windowPcm]);

            const text = await googleSTT(wavBuffer);
            partialSent = true;
            fetch('http://127.0.0.1:18080/stt_partial', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ guild_id: guildId, text }),
            }).catch((err) =>
                console.error(`[STT] Failed to send partial text to Python:`, err.message),
            );
        }, PARTIAL_INTERVAL_MS);

        let bgNoiseRMS = 500;
        let isSpeaking = false;
        let silenceBytes = 0;
        let silenceTimer = null;

        pcmStream.on('data', (rawChunk) => {
            if (hasEnded) return;
            const chunk = stereoToMono(rawChunk); // see stereoToMono's comment - decoder gives real stereo now

            let sumSquare = 0;
            for (let i = 0; i < chunk.length; i += 2) {
                const sample = chunk.readInt16LE(i);
                sumSquare += sample * sample;
            }
            const rms = Math.sqrt(sumSquare / (chunk.length / 2));

            const isBotPlaying = isPlaying.get(guildId) || false;

            if (!hasInterrupted) {
                const dynamicThreshold = isBotPlaying ? vadThreshold * 3 : vadThreshold;
                if (rms > dynamicThreshold) {
                    if (interruptTTS(guildId)) {
                        console.log(
                            `[VAD] Loud voice detected (${Math.round(rms)}), interrupting TTS (Threshold: ${dynamicThreshold})`,
                        );
                        hasInterrupted = true;
                    }
                }
            }

            if (isPlaying.get(guildId)) {
                return;
            }

            chunks.push(chunk);

            if (detectorEntry) {
                const detection = feedPCMToDetector(detectorEntry, chunk);
                if (detection && detection.getScore() > bestWakeScore) {
                    bestWakeScore = detection.getScore();
                    bestWakeScoreName = detection.getName();
                }
                const diagDetection = feedPCMToDetector(detectorEntry.diag, chunk);
                if (diagDetection && diagDetection.getScore() > bestDiagScore) {
                    bestDiagScore = diagDetection.getScore();
                    bestDiagScoreName = diagDetection.getName();
                }
            }

            if (!isSpeaking) {
                bgNoiseRMS = bgNoiseRMS * 0.98 + rms * 0.02;
                bgNoiseRMS = Math.max(50, Math.min(bgNoiseRMS, 3000));
            }

            const threshold = Math.max(bgNoiseRMS * 2.0, 800);

            if (rms > threshold) {
                isSpeaking = true;
                silenceBytes = 0;
            } else {
                if (isSpeaking) {
                    silenceBytes += chunk.length;
                    if (silenceBytes >= 76800) {
                        forceEndStream();
                        return;
                    }
                }
            }

            if (isSpeaking) {
                if (silenceTimer) clearTimeout(silenceTimer);
                silenceTimer = setTimeout(() => forceEndStream(), 800);
            }
        });

        pcmStream.on('end', async () => {
            if (hasEnded) return;
            hasEnded = true;
            clearTimeout(maxDurationTimer);
            clearInterval(partialTimer);
            if (silenceTimer) clearTimeout(silenceTimer);

            activeStreams.delete(userId);

            if (detectorEntry) {
                // rustpotter needs MORE frames fed after a candidate
                // match before it finalizes one (detection_countdown
                // counts down from max_mfcc_frames/2 before confirming
                // or discarding a partial match - see the Rust source's
                // detector.rs). Live capture stops the instant the user
                // stops talking, so without this, a genuine match
                // candidate never gets the chance to finish counting
                // down and is silently discarded - this is exactly why
                // rustpotter-cli's own `test` command pads its input
                // with 100 extra silent frames before processing (see
                // its test.rs), and why testing the identical captured
                // audio through the CLI scored well while this always
                // failed live. Same fix here: flush the residual plus
                // a few seconds of silence through the same detector
                // before deciding pass/fail.
                const paddingBuffer = Buffer.alloc(detectorEntry.samplesPerFrame * 100 * 2);
                const paddingDetection = feedPCMToDetector(detectorEntry, paddingBuffer);
                if (paddingDetection && paddingDetection.getScore() > bestWakeScore) {
                    bestWakeScore = paddingDetection.getScore();
                    bestWakeScoreName = paddingDetection.getName();
                }
                const diagPaddingBuffer = Buffer.alloc(
                    detectorEntry.diag.samplesPerFrame * 100 * 2,
                );
                const diagPaddingDetection = feedPCMToDetector(
                    detectorEntry.diag,
                    diagPaddingBuffer,
                );
                if (diagPaddingDetection && diagPaddingDetection.getScore() > bestDiagScore) {
                    bestDiagScore = diagPaddingDetection.getScore();
                    bestDiagScoreName = diagPaddingDetection.getName();
                }

                // Real pass/fail uses bestWakeScore; bestDiagScore is a separate, much looser detector shown only for "how close" - not on the same scale, not comparable to WAKE_MATCH_THRESHOLD.
                wakeConfirmed = bestWakeScore >= WAKE_MATCH_THRESHOLD;
                matchedWakeWord = wakeConfirmed ? bestWakeScoreName : null;
                console.log(
                    wakeConfirmed
                        ? `[Wake] ${userId}: CONFIRMED (score ${bestWakeScore.toFixed(3)} for ` +
                              `"${bestWakeScoreName}", threshold ${WAKE_MATCH_THRESHOLD})`
                        : `[Wake] ${userId}: no match (score ${bestWakeScore.toFixed(3)}, ` +
                              `threshold ${WAKE_MATCH_THRESHOLD}; diagnostic-only closeness ` +
                              `${bestDiagScore.toFixed(3)} for "${bestDiagScoreName ?? 'n/a'}" - ` +
                              `different scoring config, not directly comparable to the threshold)`,
                );
            }

            const pcmBuffer = Buffer.concat(chunks);

            // Enrollment mode: this utterance is a wake-word reference
            // sample, not a command - hand it straight to Python and skip
            // wake-check/STT/active-window logic entirely.
            if (enrollingUsers.has(userId)) {
                if (pcmBuffer.length < 4000) return; // too short to be a real sample
                const wavHeader = createWavHeader(pcmBuffer.length);
                const wavBuffer = Buffer.concat([wavHeader, pcmBuffer]);
                try {
                    await fetch(
                        `http://127.0.0.1:18080/enroll_sample?user_id=${encodeURIComponent(userId)}`,
                        {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/octet-stream' },
                            body: wavBuffer,
                        },
                    );
                } catch (err) {
                    console.error(`[Enroll] Failed to send sample to Python:`, err.message);
                }
                return;
            }

            // Was 24000 (250ms) - cut off short Korean replies ("네"/"어"/"응"); noise is filtered upstream by isSpeaking's RMS/sustain check, not by duration.
            if (pcmBuffer.length < 9600) {
                if (partialSent) {
                    fetch('http://127.0.0.1:18080/stt_partial_cancel', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ guild_id: guildId }),
                    }).catch(() => {});
                }
                return;
            }

            const shouldTranscribe =
                isGuildActive(guildId) || wakeConfirmed || wakeWordOptedOut.has(userId);

            if (!shouldTranscribe) {
                if (partialSent) {
                    fetch('http://127.0.0.1:18080/stt_partial_cancel', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ guild_id: guildId }),
                    }).catch(() => {});
                }
                return;
            }

            const wavHeader = createWavHeader(pcmBuffer.length);
            const wavBuffer = Buffer.concat([wavHeader, pcmBuffer]);
            const text = await googleSTT(wavBuffer);

            try {
                await fetch('http://127.0.0.1:18080/stt_input', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        user_id: userId,
                        guild_id: guildId,
                        text,
                        wake_confirmed: wakeConfirmed,
                        matched_wake_word: matchedWakeWord,
                    }),
                });
            } catch (err) {
                console.error(`[STT] Failed to send recognized text to Python:`, err.message);
            }
        });
    });
}

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

        connections.set(guild_id, connection);

        setupReceiver(connection, guild_id);

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
    const connection = connections.get(guild_id);
    if (!connection) {
        return res.status(404).json({ error: 'Not connected' });
    }

    const player = players.get(guild_id);
    if (player) {
        try {
            player.stop(true);
        } catch (e) {
            // already stopped/destroyed - fine
        }
    }
    connection.destroy();

    // Without this, a stale isPlaying/players entry silently breaks STT/wake detection on the next /join.
    connections.delete(guild_id);
    players.delete(guild_id);
    audioQueues.delete(guild_id);
    isPlaying.delete(guild_id);
    activeUntil.delete(guild_id);
    suppressNotifyMap.delete(guild_id);

    res.json({ success: true });
});

// Whether the audio that just finished playing should extend the "stay awake" window
// (false for wake-word enrollment sample playback - see suppress_active_window).
const suppressNotifyMap = new Map();

async function notifyTtsFinished(guild_id) {
    try {
        await fetch('http://127.0.0.1:18080/tts_finished', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
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

app.post('/play', express.raw({ type: 'application/octet-stream', limit: '20mb' }), (req, res) => {
    const guild_id = req.query.guild_id;
    const connection = connections.get(guild_id);
    if (!connection) return res.status(404).json({ error: 'Not connected' });

    if (!audioQueues.has(guild_id)) {
        audioQueues.set(guild_id, []);
    }

    audioQueues.get(guild_id).push({
        buffer: req.body, // req.body is a Buffer here
        suppressActiveWindow: req.query.suppress_active_window === 'true',
    });

    if (!isPlaying.get(guild_id)) {
        playNextInQueue(guild_id);
    }

    res.json({ success: true, queued: true });
});

app.post('/interrupt', (req, res) => {
    const { guild_id } = req.body;
    // Lets Python trigger the same cutoff the VAD loud-voice check uses, regardless of volume.
    interruptTTS(guild_id);
    res.json({ success: true });
});

app.post('/invalidate_detector', (req, res) => {
    // Without this, detectorCache keeps serving the OLD .rpw after a user re-enrolls.
    const { user_id } = req.body;
    const deleted = detectorCache.delete(user_id);
    console.log(`[Wake] Invalidated cached detector for ${user_id} (was cached: ${deleted})`);
    res.json({ success: true, was_cached: deleted });
});

app.post('/build_wakeword', async (req, res) => {
    // Builds a .rpw reference in-process via rustpotter-web's WakewordRefCreator, instead of
    // shelling out to a separately-downloaded rustpotter-cli binary like this used to.
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
        vadThreshold = voice_threshold;
        console.log(`[Config] Updated VAD threshold to ${vadThreshold}`);
    }
    res.json({ success: true });
});

app.post('/enroll_start', (req, res) => {
    const { user_id } = req.body;
    if (!user_id) return res.status(400).json({ error: 'user_id required' });
    enrollingUsers.add(user_id);
    res.json({ success: true });
});

app.post('/enroll_stop', (req, res) => {
    const { user_id } = req.body;
    if (!user_id) return res.status(400).json({ error: 'user_id required' });
    enrollingUsers.delete(user_id);
    res.json({ success: true });
});

app.post('/set_active', (req, res) => {
    const { guild_id, active_until } = req.body;
    if (!guild_id || !active_until)
        return res.status(400).json({ error: 'guild_id and active_until required' });
    activeUntil.set(guild_id, active_until);
    res.json({ success: true });
});

app.post('/set_wake_word_required', (req, res) => {
    const { user_id, required } = req.body;
    if (!user_id) return res.status(400).json({ error: 'user_id required' });
    if (required) wakeWordOptedOut.delete(user_id);
    else wakeWordOptedOut.add(user_id);
    res.json({ success: true });
});

process.on('unhandledRejection', (reason) => {
    console.error('Unhandled promise rejection (voice service stays alive):', reason);
});

process.on('uncaughtException', (err) => {
    // Without this handler, an uncaught synchronous error kills the
    // process with no trace of why - which is what made the previous
    // "voice service just disappeared mid-enrollment" reports
    // undiagnosable. Node still exits after this (an uncaughtException
    // means something is in an unknown state - continuing risks worse
    // corruption than restarting), but now the cause is on record.
    console.error('Uncaught exception - voice service is exiting:', err);
    process.exit(1);
});

// Without this, SIGTERM (lgy stop/restart) kills the process mid-connection and Discord never gets a clean leave.
function shutdownGracefully() {
    console.log(`[Shutdown] Disconnecting from ${connections.size} active voice connection(s)...`);
    for (const connection of connections.values()) {
        try {
            connection.destroy();
        } catch (e) {
            // already destroyed/disconnected - fine
        }
    }
    process.exit(0);
}

process.on('SIGTERM', shutdownGracefully);
process.on('SIGINT', shutdownGracefully);

const PORT = 18081;
app.listen(PORT, '0.0.0.0', () => {
    console.log(`Node.js Voice API listening on port ${PORT}`);
    client.login(process.env.DISCORD_TOKEN).catch((err) => {
        console.error('Failed to log in to Discord:', err.message);
        console.error(
            'Voice features will be unavailable until this is fixed - check your Discord token (run `lgy setup`).',
        );
    });
});
