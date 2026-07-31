const { spawn } = require('child_process');
const ffmpegPath = require('ffmpeg-static');

// Unofficial Google STT key - same default Python's SpeechRecognition (recognize_google) ships with.
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

module.exports = { googleSTT };
