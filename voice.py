"""Short voice commands for the cinema board.

Trailers stay muted. "sound on" unmutes. Once the trailer is audible, the
mic hears the movie, so muting likes the wake word — but a clean exact
"sound off" is accepted too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_NAME = "vosk-model-small-en-us-0.15"

ON = (
    "sound on",
    "sounds on",
    "unmute",
    "volume on",
    "turn sound on",
    "turn the sound on",
    "turn on sound",
    "turn on the sound",
)
OFF = (
    "sound off",
    "sounds off",
    "mute",
    "no sound",
    "turn sound off",
    "turn the sound off",
    "turn off sound",
    "turn off the sound",
)
LYRICS_ON = ("lyrics", "show lyrics")
LYRICS_OFF = ("hide lyrics", "close lyrics", "lyrics off", "no lyrics", "stop lyrics")
WAKE = ("hey marquee", "marquee")
FILLERS = ("please", "okay", "ok", "hey")

_model = None
_model_failed = False
_ready = False


def model_dir() -> Path | None:
    candidates = []
    if os.environ.get("MARQUEE_VOSK_MODEL"):
        candidates.append(Path(os.environ["MARQUEE_VOSK_MODEL"]))
    home = Path.home() / ".local" / "share" / "marquee" / MODEL_NAME
    candidates.append(home)
    candidates.append(Path("/usr/share/vosk") / MODEL_NAME)
    for path in candidates:
        if (path / "conf").is_dir() or (path / "am").is_dir():
            return path
    return None


def ensure_model() -> Path | None:
    found = model_dir()
    if found:
        return found
    dest = Path.home() / ".local" / "share" / "marquee"
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{MODEL_NAME}.zip"
    try:
        print("downloading voice model", flush=True)
        urlretrieve(MODEL_URL, archive)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(dest)
        archive.unlink(missing_ok=True)
    except Exception as exc:
        print(f"voice model download failed: {exc}", flush=True)
        return None
    return model_dir()


def _load():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    try:
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
    except Exception as exc:
        print(f"voice off, vosk missing: {exc}", flush=True)
        _model_failed = True
        return None
    path = ensure_model()
    if not path:
        _model_failed = True
        return None
    try:
        _model = Model(str(path))
        global _ready
        _ready = True
        print(f"voice ready {path}", flush=True)
    except Exception as exc:
        print(f"voice model failed: {exc}", flush=True)
        _model_failed = True
    return _model


def start():
    threading.Thread(target=_load, name="voice-model", daemon=True).start()


def to_speech_wav(src: Path, dest: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000", "-af", "volume=14dB", str(dest)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ffmpeg failed: {exc}", flush=True)
        return False
    return proc.returncode == 0 and dest.exists()


def transcribe(path: Path) -> str:
    if not _ready or _model is None:
        return ""
    model = _model
    try:
        import wave

        from vosk import KaldiRecognizer
    except Exception:
        return ""
    try:
        with wave.open(str(path)) as audio:
            if audio.getnchannels() != 1 or audio.getframerate() != 16000:
                return ""
            recognizer = KaldiRecognizer(model, audio.getframerate())
            while True:
                chunk = audio.readframes(4000)
                if not chunk:
                    break
                recognizer.AcceptWaveform(chunk)
            payload = json.loads(recognizer.FinalResult())
    except Exception as exc:
        print(f"voice failed: {exc}", flush=True)
        return ""
    return (payload.get("text") or "").strip()


def _normalize(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " if ch != "'" else "" for ch in text.lower())
    return " ".join(cleaned.split())


def _strip_fillers(words: str):
    woke = False
    for _ in range(3):
        for filler in WAKE + FILLERS:
            if words.startswith(filler + " "):
                words = words[len(filler) + 1 :]
            elif words.endswith(" " + filler):
                words = words[: -len(filler) - 1]
            else:
                continue
            woke = woke or filler in WAKE
            break
        else:
            break
    return words, woke


def command_for(text: str, sound_on: bool) -> str | None:
    words = _normalize(text)
    if not words or len(words.split()) > 8:
        return None
    core, woke = _strip_fillers(words)
    if core in LYRICS_OFF:
        return "hide-lyrics"
    if core in LYRICS_ON:
        return "lyrics"
    if sound_on:
        # the movie is audible, so dialogue could stumble onto an off
        # phrase; require the exact phrase or the wake word.
        if words in OFF or (woke and core in OFF):
            return "off"
        return None
    if core in ON:
        return "on"
    if core in OFF:
        return "off"
    return None
