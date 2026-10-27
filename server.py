#!/usr/bin/env python3
"""Marquee.

Listens on the mic, shows the album as large as the screen allows, and after
the room has been quiet for a couple of minutes switches to the nearest
Cinemark board. Trailers start muted. Say "sound on" to hear one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cinema
import voice

HOST = "127.0.0.1"
PORT = 8787
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SOURCE = "alsa_input.pci-0000_00_1f.3.analog-stereo"
CLIP = "/tmp/marquee-clip.wav"
SPEECH = "/tmp/marquee-speech.wav"
HW_DEVICE = "hw:0,0"
QUIET_AFTER = 18
DEFAULT_CINEMA_AFTER = 120
SOUND_LEVEL = 0.012
CINEMA_REFRESH = 20 * 60
CINEMA_RETRY = 60
ALLOWED_ORIGINS = ("http://127.0.0.1:8787", "http://localhost:8787")

state_lock = threading.Lock()
state = {
    "status": "listening",
    "title": "",
    "artist": "",
    "album": "",
    "art_url": "",
    "share": "",
    "key": "",
    "lyrics": "",
    "synced": [],
    "song_offset": 0.0,
    "show_lyrics": False,
    "recognized_at": 0,
    "heard_at": 0,
    "loud_at": 0,
    "level": 0.0,
    "changed_at": 0,
    "trailer_sound": False,
    "heard_command": "",
    "command_at": 0,
}
art_bytes = b""
art_type = "image/jpeg"
cinema_board = {
    "id": cinema.LAKELAND["id"],
    "name": cinema.LAKELAND["name"],
    "label": "",
    "date": "",
    "movies": [],
}
config = {
    "theater_id": cinema.LAKELAND["id"],
    "theater_name": cinema.LAKELAND["name"],
    "quiet_for_cinema": DEFAULT_CINEMA_AFTER,
}
config_lock = threading.Lock()
subscribers: list[threading.Condition] = []
subscribers_lock = threading.Lock()


def publish():
    with subscribers_lock:
        conds = list(subscribers)
    for cond in conds:
        with cond:
            cond.notify_all()


def set_state(**kwargs):
    changed = False
    with state_lock:
        for key, value in kwargs.items():
            if state.get(key) != value:
                state[key] = value
                changed = True
        if changed and "key" in kwargs:
            state["changed_at"] = time.time()
    if changed:
        publish()


def load_config():
    global config
    if not CONFIG_PATH.exists():
        return
    try:
        loaded = json.loads(CONFIG_PATH.read_text())
    except Exception as exc:
        print(f"config unreadable: {exc}", flush=True)
        return
    if isinstance(loaded, dict):
        config.update(loaded)


def save_config():
    # write then rename so a power cut never leaves a half-written config
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)


def cinema_after() -> float:
    try:
        return float(config.get("quiet_for_cinema") or DEFAULT_CINEMA_AFTER)
    except (TypeError, ValueError):
        return DEFAULT_CINEMA_AFTER


def scene_for(title, loud_at, trailer_sound, now) -> str:
    # The album stays up until the room has been quiet for the whole window.
    if trailer_sound or not title or now - loud_at >= cinema_after():
        return "cinema"
    return "music"


def scene_now():
    with state_lock:
        title, loud_at, sound_on = state["title"], state["loud_at"], state["trailer_sound"]
    return scene_for(title, loud_at, sound_on, time.time()), sound_on


def snapshot():
    with state_lock:
        data = dict(state)
        board = dict(cinema_board)
        board["movies"] = list(cinema_board.get("movies") or [])
        has_art = bool(art_bytes)
    now = time.time()
    data["scene"] = scene_for(data["title"], data["loud_at"], data["trailer_sound"], now)
    if data["scene"] == "cinema":
        data["status"] = "cinema"
    else:
        age = now - data["recognized_at"] if data["recognized_at"] else None
        if data["level"] >= SOUND_LEVEL or (age is not None and age < QUIET_AFTER):
            data["status"] = "playing"
        else:
            data["status"] = "quiet"
    data["art"] = f"/art?v={int(data['changed_at'])}" if has_art else ""
    data["show_lyrics"] = bool(data.get("show_lyrics"))
    data["synced"] = data.get("synced") or [] if data["show_lyrics"] else []
    if data["recognized_at"]:
        data["song_position"] = max(0.0, data.get("song_offset") or 0) + (now - data["recognized_at"])
    else:
        data["song_position"] = 0
    data["cinema"] = {
        "id": board.get("id"),
        "name": board.get("name") or config.get("theater_name") or "",
        "label": board.get("label") or "",
        "date": board.get("date") or "",
        "movies": [
            {
                "title": movie.get("title") or "",
                "rating": movie.get("rating") or "",
                "runtime": movie.get("runtime") or "",
                "director": movie.get("director") or "",
                "poster": movie.get("poster") or "",
                "trailer": movie.get("trailer") or "",
                "formats": movie.get("formats") or [],
                "days": movie.get("days") or [],
                "times": [item.get("label") for item in (movie.get("times") or []) if item.get("label")],
            }
            for movie in board["movies"]
        ],
    }
    data["server_time"] = now
    return data


def parse_synced(text: str) -> list[dict]:
    timed = []
    for raw in text.splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        if not stamps:
            continue
        start = int(stamps[0][0]) * 60 + float(stamps[0][1])
        marked = re.findall(r"<(\d+):(\d+(?:\.\d+)?)>([^<]+)", raw)
        if marked:
            for minutes, seconds, word in marked:
                word = word.strip()
                if word:
                    timed.append({"t": int(minutes) * 60 + float(seconds), "w": word})
            continue
        line = re.sub(r"\[[^\]]+\]", "", raw).strip()
        if line:
            timed.append({"t": start, "line": line})
    if timed and "w" in timed[0]:
        return [item for item in timed if item.get("w")]
    words = []
    for index, line in enumerate(timed):
        parts = line["line"].split()
        end = timed[index + 1]["t"] if index + 1 < len(timed) else line["t"] + 3.5
        step = max(0.22, (end - line["t"]) / max(1, len(parts)))
        for place, word in enumerate(parts):
            words.append({"t": round(line["t"] + place * step, 3), "w": word})
    return words


def hi_res(url: str) -> str:
    if not url:
        return url
    return re.sub(r"\d{2,4}x\d{2,4}(?:bb|cc|ss)", "2000x2000cc", url)


def album_from(track: dict) -> str:
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if (item.get("title") or "").lower() == "album":
                return item.get("text") or ""
    return ""


def fetch_art(url: str, key: str):
    global art_bytes, art_type
    if not url:
        return
    for candidate in dict.fromkeys((hi_res(url), url)):
        try:
            req = urllib.request.Request(candidate, headers={"User-Agent": "marquee"})
            with urllib.request.urlopen(req, timeout=12) as response:
                body = response.read()
                ctype = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
            if body:
                with state_lock:
                    if state.get("key") != key:
                        return
                    art_bytes = body
                    art_type = ctype or "image/jpeg"
                publish()
                return
        except Exception as exc:
            print(f"art fetch failed: {exc}", flush=True)


def fetch_lyrics(artist: str, title: str, key: str):
    query = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    try:
        req = urllib.request.Request(
            f"https://lrclib.net/api/search?{query}",
            headers={"User-Agent": "marquee"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            rows = json.loads(response.read().decode())
    except Exception as exc:
        print(f"lyrics failed: {exc}", flush=True)
        return
    synced = []
    plain = ""
    for row in rows or []:
        if not plain and row.get("plainLyrics"):
            plain = row["plainLyrics"]
        if row.get("syncedLyrics"):
            synced = parse_synced(row["syncedLyrics"])
            plain = plain or row.get("plainLyrics") or ""
            break
    with state_lock:
        if state.get("key") != key:
            return
    set_state(lyrics=plain, synced=synced)


def ingest(payload: dict):
    global art_bytes, art_type
    track = payload.get("track") or {}
    title = track.get("title") or ""
    artist = track.get("subtitle") or ""
    if not title:
        return
    images = track.get("images") or {}
    art_url = images.get("coverarthq") or images.get("coverart") or ""
    share = ((track.get("share") or {}).get("href")) or ""
    key = f"{artist}\n{title}"
    with state_lock:
        previous = state["key"]
        if key != previous:
            # switch the key with the art so a late fetch for the old song is dropped
            state["key"] = key
            state["changed_at"] = time.time()
            art_bytes = b""
            art_type = "image/jpeg"
        need_art = key != previous or not art_bytes
    offset = 0.0
    for match in payload.get("matches") or []:
        try:
            offset = float(match.get("offset") or 0)
            break
        except (TypeError, ValueError):
            continue
    set_state(
        title=title,
        artist=artist,
        album=album_from(track),
        art_url=art_url,
        share=share,
        key=key,
        song_offset=offset,
        recognized_at=time.time(),
        loud_at=time.time(),
    )
    if key != previous:
        set_state(lyrics="", synced=[])
        threading.Thread(target=fetch_lyrics, args=(artist, title, key), daemon=True).start()
    if need_art:
        threading.Thread(target=fetch_art, args=(art_url, key), daemon=True).start()


def mic_bias():
    for args in (
        ["/usr/bin/hda-verb", "/dev/snd/hwC0D0", "0x45", "0x707", "0x24"],
        ["/usr/bin/hda-verb", "/dev/snd/hwC0D0", "0x23", "0x705", "0x00"],
    ):
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except FileNotFoundError:
            return


def clip_level(path: Path) -> float:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except Exception:
        return 0.0
    samples = memoryview(proc.stdout).cast("h")
    if not samples:
        return 0.0
    return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32768


def listen_loop():
    while True:
        print("recording", flush=True)
        try:
            seconds = "4" if scene_now()[0] == "cinema" else "7"
            rec = subprocess.Popen(
                ["arecord", "-D", HW_DEVICE, "-f", "S32_LE", "-r", "44100", "-c", "2", "-d", seconds, CLIP],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.25)
            mic_bias()
            rec.wait(timeout=12)
            level = clip_level(Path(CLIP))
            if level >= SOUND_LEVEL:
                set_state(level=level, loud_at=time.time())
            else:
                set_state(level=level)
            scene, sound_on = scene_now()
            command = None
            on_cinema = scene == "cinema"
            if (on_cinema or level >= 0.004) and voice.to_speech_wav(Path(CLIP), Path(SPEECH)):
                heard = voice.transcribe(Path(SPEECH))
                if heard:
                    print(f"heard: {heard}", flush=True)
                command = voice.command_for(heard, sound_on)
            if command == "lyrics":
                print("voice: lyrics", flush=True)
                set_state(show_lyrics=True, heard_command="lyrics", command_at=time.time())
            elif command == "hide-lyrics":
                print("voice: hide lyrics", flush=True)
                set_state(show_lyrics=False, heard_command="hide lyrics", command_at=time.time())
            elif command == "on" and scene_now()[0] == "cinema":
                print("voice: sound on", flush=True)
                set_state(trailer_sound=True, heard_command="sound on", command_at=time.time())
                continue
            elif command == "off":
                print("voice: sound off", flush=True)
                set_state(trailer_sound=False, heard_command="muted", command_at=time.time())
                continue
            if sound_on:
                continue
            if level < 0.01:
                print(f"quiet level={level:.4f}", flush=True)
                time.sleep(1)
                continue
            set_state(heard_at=time.time())
            proc = subprocess.run(
                ["songrec", "recognize", "--json", CLIP],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=25,
                check=False,
            )
            if proc.stdout.strip():
                try:
                    ingest(json.loads(proc.stdout))
                    print("recognized", flush=True)
                except json.JSONDecodeError as exc:
                    print(f"bad json: {exc}", flush=True)
        except Exception as exc:
            print(f"listen failed: {exc}", flush=True)
            time.sleep(2)


def refresh_cinema() -> bool:
    """Fetch the showtimes board. Returns True when it has movies."""
    global cinema_board
    theater_id = config.get("theater_id") or cinema.LAKELAND["id"]
    try:
        board = cinema.board(int(theater_id))
    except Exception as exc:
        print(f"cinema refresh failed: {exc}", flush=True)
        return False
    if config.get("theater_name"):
        board["name"] = config["theater_name"]
    for movie in board.get("movies") or []:
        # Keep a local copy so posters survive a Cinemark CDN hiccup. If the download
        # fails, the remote URL stays and the browser tries it directly.
        name = cinema.cache_poster(movie.get("poster") or "")
        if name:
            movie["poster"] = f"/posters/{name}"
    movies = board.get("movies") or []
    with state_lock:
        previous = cinema_board
        if not movies and previous.get("movies") and previous.get("id") == board.get("id"):
            # Failed fetches come back empty (no network yet at boot, a Cinemark outage).
            # Keep the last good board for this theater instead of blanking the screen.
            print(f"cinema refresh empty, keeping {len(previous['movies'])} movies", flush=True)
            return False
        cinema_board = board
    print(f"cinema {len(movies)} movies at {board.get('name')}", flush=True)
    publish()
    return bool(movies)


def cinema_loop():
    retry = CINEMA_RETRY
    while True:
        if refresh_cinema():
            retry = CINEMA_RETRY
            time.sleep(CINEMA_REFRESH)
        else:
            # Retry soon after a failure, backing off so a theater with no showtimes
            # does not hit Cinemark every minute.
            time.sleep(retry)
            retry = min(retry * 2, CINEMA_REFRESH)


def boost_mic():
    subprocess.run(["pactl", "set-source-mute", SOURCE, "0"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pactl", "set-source-volume", SOURCE, "150%"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type, extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/mute", "/api/config"):
            self._send(404, b"not found", "text/plain")
            return
        # A JSON content type forces a CORS preflight, which is never answered,
        # so other sites can't post here.
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            self._send(403, b"forbidden", "text/plain")
            return
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("application/json"):
            self._send(415, b"expected application/json", "text/plain")
            return
        if path == "/mute":
            # a trailer finished; sound on was for that one only
            set_state(trailer_sound=False, heard_command="")
            publish()
            self._send(200, b"muted", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative length")
            incoming = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError, UnicodeDecodeError):
            self._send(400, b"bad json", "text/plain")
            return
        if not isinstance(incoming, dict):
            self._send(400, b"expected an object", "text/plain")
            return
        update = {}
        if incoming.get("theater_id"):
            theater_id = as_int(incoming["theater_id"])
            if theater_id is None or theater_id <= 0:
                self._send(400, b"bad theater_id", "text/plain")
                return
            update["theater_id"] = theater_id
        if "theater_name" in incoming:
            name = incoming["theater_name"]
            if name is not None and not isinstance(name, str):
                self._send(400, b"bad theater_name", "text/plain")
                return
            update["theater_name"] = name or ""
        if "quiet_for_cinema" in incoming:
            quiet = as_int(incoming["quiet_for_cinema"])
            if quiet is None or not 0 <= quiet <= 3600:
                self._send(400, b"bad quiet_for_cinema", "text/plain")
                return
            update["quiet_for_cinema"] = quiet
        with config_lock:
            config.update(update)
            save_config()
            body = json.dumps(config)
        threading.Thread(target=refresh_cinema, daemon=True).start()
        self._send(200, body, "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/setup":
            self._send(200, (ROOT / "setup.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        if path == "/api/now":
            self._send(200, json.dumps(snapshot()), "application/json")
            return
        if path == "/api/config":
            with config_lock:
                body = json.dumps(config)
            self._send(200, body, "application/json")
            return
        if path == "/api/theaters":
            found = None
            if "lat" in query and "lon" in query:
                try:
                    lat, lon = float(query["lat"][0]), float(query["lon"][0])
                except ValueError:
                    self._send(400, b"bad lat/lon", "text/plain")
                    return
                found = cinema.nearest_geo(lat, lon)
            elif "q" in query:
                found = cinema.nearest_text(query["q"][0])
            self._send(200, json.dumps(found or {}), "application/json")
            return
        if path.startswith("/posters/"):
            found = cinema.poster_file(path[len("/posters/"):])
            if found:
                self._send(200, found[1], found[0])
                return
            self._send(404, b"not found", "text/plain")
            return
        if path == "/art":
            with state_lock:
                body, ctype = art_bytes, art_type
            if not body:
                self._send(404, b"", "text/plain")
                return
            self._send(200, body, ctype)
            return
        if path.startswith("/fonts/"):
            fonts = (ROOT / "fonts").resolve()
            font = (ROOT / path.lstrip("/")).resolve()
            if fonts in font.parents and font.is_file():
                self._send(200, font.read_bytes(), "font/ttf")
                return
            self._send(404, b"not found", "text/plain")
            return
        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            cond = threading.Condition()
            with subscribers_lock:
                subscribers.append(cond)
            try:
                self.wfile.write(f"data: {json.dumps(snapshot())}\n\n".encode())
                self.wfile.flush()
                while True:
                    with cond:
                        cond.wait(timeout=8)
                    self.wfile.write(f"data: {json.dumps(snapshot())}\n\n".encode())
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with subscribers_lock:
                    if cond in subscribers:
                        subscribers.remove(cond)
            return
        self._send(404, b"not found", "text/plain")


def main():
    load_config()
    if not config.get("theater_id"):
        config["theater_id"] = cinema.LAKELAND["id"]
        config["theater_name"] = cinema.LAKELAND["name"]
    voice.start()
    boost_mic()
    mic_bias()
    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=cinema_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"marquee on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
