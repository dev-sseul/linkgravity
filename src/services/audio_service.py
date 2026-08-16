import asyncio
import os
import re
import tempfile
from pathlib import Path

from config import TTS_VOICE, bot_settings, logger

# Verified against edge-tts's live voice list; a name that isn't in it fails at synthesis time.
# Ordered - the first voice of a language is that language's default.
TTS_VOICES = [
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-US-AnaNeural",
    "en-US-ChristopherNeural",
    "en-US-EricNeural",
    "en-US-MichelleNeural",
    "en-US-RogerNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
    "en-AU-NatashaNeural",
    "en-AU-WilliamMultilingualNeural",
    "ko-KR-SunHiNeural",
    "ko-KR-InJoonNeural",
    "ja-JP-NanamiNeural",
    "ja-JP-KeitaNeural",
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "fr-FR-DeniseNeural",
    "de-DE-KatjaNeural",
    "es-ES-ElviraNeural",
    "it-IT-ElsaNeural",
    "pt-BR-FranciscaNeural",
    "ru-RU-SvetlanaNeural",
    "hi-IN-SwaraNeural",
    "id-ID-GadisNeural",
    "vi-VN-HoaiMyNeural",
    "th-TH-PremwadeeNeural",
    "tr-TR-EmelNeural",
    "pl-PL-ZofiaNeural",
    "nl-NL-ColetteNeural",
    "ar-SA-ZariyahNeural",
]

LANGUAGES = list(dict.fromkeys(v.rsplit("-", 1)[0] for v in TTS_VOICES))

DEFAULT_LANGUAGE = "en-US"

# Ordered: the first script reaching the share threshold wins, so Hangul beats Latin in mixed
# Korean/English text, and kana beats Han in Japanese (which is written with both).
SCRIPT_PATTERNS = [
    ("ko", re.compile(r"[가-힣]")),
    ("ja", re.compile(r"[ぁ-ゖァ-ヺ]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("en", re.compile(r"[a-zA-Z]")),
]

SCRIPT_SHARE = 0.3


def detect_script(text: str) -> str | None:
    counts = {tag: len(pattern.findall(text)) for tag, pattern in SCRIPT_PATTERNS}
    total = sum(counts.values())
    if not total:
        return None
    for tag, _ in SCRIPT_PATTERNS:
        if counts[tag] / total >= SCRIPT_SHARE:
            return tag
    return max(counts, key=counts.get)


def default_voice_for(tag: str) -> str:
    for voice in TTS_VOICES:
        if voice.startswith(f"{tag}-"):
            return voice
    if "-" in tag:
        return default_voice_for(tag.split("-")[0])
    return TTS_VOICES[0]


def resolve_language() -> str:
    # Mirrored by resolveLanguage() in voice-service/stt.js, which is what reaches Google.
    configured = bot_settings.get("language")
    if configured:
        return configured
    voice = bot_settings.get("tts_voice") or TTS_VOICE or ""
    return voice.rsplit("-", 1)[0] if voice.count("-") >= 2 else DEFAULT_LANGUAGE


def voice_for(text: str) -> str:
    # Latin script covers dozens of languages, so a configured en-GB voice must survive a bare
    # "en" detection instead of being reset to the en-US default.
    configured = bot_settings.get("tts_voice") or TTS_VOICE
    script = detect_script(text)
    if script is None or configured.startswith(f"{script}-"):
        return configured
    return default_voice_for(script)


async def tts(text: str, voice: str = None) -> bytes | None:
    try:
        import edge_tts

        clean = re.sub(r"[`*#_\[\]()]", "", text)
        clean = re.sub(r"https?://\S+", "URL", clean)
        clean = re.sub(r"\n+", ". ", clean).strip()[:800]
        if not clean:
            return None

        speed = bot_settings.get("tts_speed", 1.0)
        pct = round((speed - 1.0) * 100)
        rate = f"{'+' if pct >= 0 else ''}{pct}%"

        communicate = edge_tts.Communicate(clean, voice or voice_for(clean), rate=rate)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = f.name
        await communicate.save(tmp)
        data = Path(tmp).read_bytes()
        os.unlink(tmp)
        return data
    except Exception as e:
        logger.exception(f"TTS error: {e}")
        return None


async def stt(audio_bytes: bytes) -> str | None:
    try:

        def _transcribe():
            import io

            import speech_recognition as sr

            r = sr.Recognizer()
            with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
                audio = r.record(source)
            try:
                return r.recognize_google(audio, language=resolve_language())
            except sr.UnknownValueError:
                return None
            except sr.RequestError as e:
                logger.error(f"STT API error: {e}")
                return None

        result = await asyncio.to_thread(_transcribe)
        return result.strip() if result else None
    except Exception as e:
        logger.exception(f"STT internal error: {e}")
        return None
