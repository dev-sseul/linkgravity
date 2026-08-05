const { EndBehaviorType } = require('@discordjs/voice');
const prism = require('prism-media');
const { stereoToMono, createWavHeader } = require('./audioUtils');
const { googleSTT } = require('./stt');
const { getDetectorForUser, feedPCMToDetector, WAKE_MATCH_THRESHOLD } = require('./wakeword');
const { interruptTTS } = require('./tts');
const state = require('./state');
const { aglConfig } = require('./config');
const { activeStreams, enrollingUsers, isPlaying, wakeWordOptedOut, runtime, isGuildActive } =
    state;

function setupReceiver(connection, guildId, client) {
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

        // Only during the active/awake window - reuses the same googleSTT() call for earlier live feedback.
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
                headers: {
                    'Content-Type': 'application/json',
                    'X-LGY-Token': aglConfig.approve_token,
                },
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
                const dynamicThreshold = isBotPlaying
                    ? runtime.vadThreshold * 3
                    : runtime.vadThreshold;
                if (rms > dynamicThreshold) {
                    if (interruptTTS(guildId)) {
                        console.log(
                            `[VAD] Loud voice detected (${Math.round(rms)}), interrupting TTS (Threshold: ${dynamicThreshold})`,
                        );
                        hasInterrupted = true;
                    }
                }
            }

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

            if (isPlaying.get(guildId)) {
                return;
            }

            chunks.push(chunk);

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
                // Live capture stops before rustpotter's confirm countdown finishes, so pad with
                // silence to let a genuine match finalize instead of being silently discarded.
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

            // Enrollment mode: this is a reference sample, not a command - skip wake-check/STT.
            if (enrollingUsers.has(userId)) {
                if (pcmBuffer.length < 4000) return; // too short to be a real sample
                const wavHeader = createWavHeader(pcmBuffer.length);
                const wavBuffer = Buffer.concat([wavHeader, pcmBuffer]);
                try {
                    await fetch(
                        `http://127.0.0.1:18080/enroll_sample?user_id=${encodeURIComponent(userId)}`,
                        {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/octet-stream',
                                'X-LGY-Token': aglConfig.approve_token,
                            },
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
                        headers: {
                            'Content-Type': 'application/json',
                            'X-LGY-Token': aglConfig.approve_token,
                        },
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
                        headers: {
                            'Content-Type': 'application/json',
                            'X-LGY-Token': aglConfig.approve_token,
                        },
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
                    headers: {
                        'Content-Type': 'application/json',
                        'X-LGY-Token': aglConfig.approve_token,
                    },
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

module.exports = { setupReceiver };
