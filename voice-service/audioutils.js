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

module.exports = { stereoToMono, createWavHeader };
