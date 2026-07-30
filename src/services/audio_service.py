import asyncio
import os
import re
import tempfile
from pathlib import Path

from config import TTS_VOICE, bot_settings, logger


async def tts(text: str, voice: str = None) -> bytes | None:
    voice = voice or bot_settings.get("tts_voice", TTS_VOICE)
    try:
        import edge_tts

        clean = re.sub(r"[`*#_\[\]()]", "", text)
        clean = re.sub(r"https?://\S+", "URL", clean)
        clean = re.sub(r"\n+", ". ", clean).strip()[:800]
        if not clean:
            return None
        hangul_chars = len(re.findall(r"[가-힣]", clean))
        alpha_chars = len(re.findall(r"[a-zA-Z]", clean))
        total_letters = hangul_chars + alpha_chars
        ko_voice = os.getenv("TTS_VOICE_KO", "ko-KR-SunHiNeural")
        en_voice = os.getenv("TTS_VOICE_EN", "en-US-AriaNeural")

        if total_letters == 0:
            active_voice = TTS_VOICE
        elif (hangul_chars / total_letters) >= 0.3:
            active_voice = ko_voice
        else:
            active_voice = en_voice

        speed = bot_settings.get("tts_speed", 1.0)
        pct = round((speed - 1.0) * 100)
        rate = f"{'+' if pct >= 0 else ''}{pct}%"

        communicate = edge_tts.Communicate(clean, active_voice, rate=rate)
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
                return r.recognize_google(audio, language="ko-KR")
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
