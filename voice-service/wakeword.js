const fs = require('fs');
const os = require('os');
const path = require('path');
const { detectorCache, wakeThresholds } = require('./state');

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

// Wake-word confirm cutoff, applied in receiver.js against the detection score - 0.4 chosen after
// live use kept narrowly missing genuine hits just under 0.5.
const DEFAULT_WAKE_THRESHOLD = 0.4;

// What rustpotter itself is told to use. It emits nothing below its own threshold, so this has to
// sit under every allowed user threshold or misses would report no score at all. Not lower than
// this either: by ~0.15 its countdown gets cleared by noise/silence before anything confirms.
const DETECTION_FLOOR = 0.2;

function wakeThresholdFor(userId) {
    return wakeThresholds.get(userId) ?? DEFAULT_WAKE_THRESHOLD;
}

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
    config.setThreshold(DETECTION_FLOOR);
    config.setAveragedThreshold(0);
    // Live logs showed genuine attempts peaking above threshold but not sustaining 4 positive-scoring
    // frames; lowered from 4. STT-side prefix-similarity check is the backstop against false wakes.
    config.setMinScores(2);
    // Max (best of the 5 enrolled samples) beats Median here - real speech isn't consistent enough
    // for Median's "middle sample must also score well" requirement.
    config.setScoreMode(mod.ScoreMode.max);
    // Enrollment and live-call volume rarely match (distance, speaking softly); without this, that
    // mismatch alone can push a genuine match below threshold.
    config.setGainNormalizerEnabled(true);

    const rustpotter = mod.Rustpotter.new(config);
    rustpotter.addWakeword(rpwFile, fs.readFileSync(path.join(userDir, rpwFile)));

    const entry = {
        rustpotter,
        samplesPerFrame: rustpotter.getSamplesPerFrame(),
        residual: new Int16Array(0),
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
    DEFAULT_WAKE_THRESHOLD,
    DETECTION_FLOOR,
    wakeThresholdFor,
    loadRustpotterModule,
    getDetectorForUser,
    feedPCMToDetector,
};
