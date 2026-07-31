const fs = require('fs');
const os = require('os');
const path = require('path');
const { detectorCache } = require('./state');

// Rustpotter wake-word detection runs entirely in-process here, no Python round trip.
const WAKE_REF_DIR = path.join(os.homedir(), '.gemini', 'linkgravity', 'wake_refs');

let rustpotterModPromise = null;
function loadRustpotterModule() {
    if (!rustpotterModPromise) {
        rustpotterModPromise = (async () => {
            // "rustpotter-web" (not "-slim") - also exposes WakewordRefCreator for /build_wakeword.
            const mod = await import('rustpotter-web/rustpotter_wasm.js');
            const wasmPath = require.resolve('rustpotter-web/rustpotter_wasm_bg.wasm');
            mod.initSync(fs.readFileSync(wasmPath));
            return mod;
        })();
    }
    return rustpotterModPromise;
}

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
    config.setThreshold(WAKE_MATCH_THRESHOLD);
    config.setAveragedThreshold(0);
    // Live logs showed genuine attempts peaking above threshold but not sustaining 4 positive-scoring
    // frames; lowered from 4. STT-side prefix-similarity check is the backstop against false wakes.
    config.setMinScores(2);
    // Max (best of the 5 enrolled samples) beats Median here - real speech isn't consistent enough
    // for Median's "middle sample must also score well" requirement; minScores compensates.
    config.setScoreMode(mod.ScoreMode.max);
    // Enrollment and live-call volume rarely match (distance, speaking softly); without this, that
    // mismatch alone can push a genuine match below threshold.
    config.setGainNormalizerEnabled(true);

    const rustpotter = mod.Rustpotter.new(config);
    rustpotter.addWakeword(rpwFile, fs.readFileSync(path.join(userDir, rpwFile)));

    // Diagnostic-only twin (same audio) so "no match" logs show a closeness score - never gates wake behavior.
    const diagConfig = mod.RustpotterConfig.new();
    diagConfig.setSampleRate(48000);
    diagConfig.setSampleFormat(mod.SampleFormat.i16);
    diagConfig.setChannels(1);
    diagConfig.setThreshold(0.01);
    diagConfig.setAveragedThreshold(0);
    diagConfig.setMinScores(1);
    diagConfig.setEager(true);
    diagConfig.setScoreMode(mod.ScoreMode.max);
    diagConfig.setGainNormalizerEnabled(true);
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

// Frame-aligns via residual carryover (Rustpotter needs a continuous stream); returns a detection if any.
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

module.exports = {
    WAKE_REF_DIR,
    WAKE_MATCH_THRESHOLD,
    loadRustpotterModule,
    getDetectorForUser,
    feedPCMToDetector,
};
