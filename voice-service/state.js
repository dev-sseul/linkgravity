const connections = new Map();
const players = new Map();
const audioQueues = new Map();
const isPlaying = new Map();
const activeStreams = new Map();

// user_id -> recording wake-word samples right now; routes to /enroll_sample instead of STT.
const enrollingUsers = new Set();

// guild_id -> ms epoch until the "awake, skip wake word" window closes (set via /set_active).
const activeUntil = new Map();

// user_id -> opted out of wake-word gating via /sound - scoped per-user, unlike activeUntil.
const wakeWordOptedOut = new Set();

// Whether finished audio should extend the "stay awake" window (false for enrollment playback).
const suppressNotifyMap = new Map();

// userId -> { rustpotter, samplesPerFrame, residual: Int16Array }
const detectorCache = new Map();

// Object property, not a plain `let` - a `let` wouldn't propagate its reassignment across modules.
const runtime = { vadThreshold: 3000 };

function isGuildActive(guildId) {
    return Date.now() < (activeUntil.get(guildId) || 0);
}

module.exports = {
    connections,
    players,
    audioQueues,
    isPlaying,
    activeStreams,
    enrollingUsers,
    activeUntil,
    wakeWordOptedOut,
    suppressNotifyMap,
    detectorCache,
    runtime,
    isGuildActive,
};
