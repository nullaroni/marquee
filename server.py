#!/usr/bin/env python3
"""Always-listening now-playing kiosk.

Listens on the built-in mic, asks SongRec/Shazam what is playing, and serves
a fullscreen album-first page on localhost.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8787
ROOT = Path(__file__).resolve().parent
SOURCE = "alsa_input.pci-0000_00_1f.3.analog-stereo"
QUIET_AFTER = 18
STALE_AFTER = 90

state_lock = threading.Lock()
state = {
    "status": "listening",
    "title": "",
    "artist": "",
    "album": "",
    "art_url": "",
    "share": "",
    "key": "",
    "recognized_at": 0,
    "level": 0.0,
    "changed_at": 0,
}
art_bytes = b""
art_type = "image/jpeg"
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


def snapshot():
    with state_lock:
        data = dict(state)
    now = time.time()
    level = data["level"]
    age = now - data["recognized_at"] if data["recognized_at"] else None
    if not data["title"]:
        data["status"] = "listening"
    elif level < 0.012 and age is not None and age > QUIET_AFTER:
        data["status"] = "quiet"
    elif age is not None and age > STALE_AFTER and level < 0.02:
        data["status"] = "quiet"
    else:
        data["status"] = "playing"
    data["art"] = "/art" if art_bytes else ""
    data["server_time"] = now
    return data


def hi_res(url: str) -> str:
    if not url:
        return url
    for size in ("100x100", "200x200", "400x400", "500x500", "600x600", "800x800", "1000x1000", "1200x1200"):
        url = url.replace(f"{size}bb", "2000x2000bb")
        url = url.replace(f"/{size}bb.", "/2000x2000bb.")
    return url


def album_from(track: dict) -> str:
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            title = (item.get("title") or "").lower()
            if title == "album":
                return item.get("text") or ""
    return ""


def fetch_art(url: str):
    global art_bytes, art_type
    if not url:
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nowplaying-kiosk"})
        with urllib.request.urlopen(req, timeout=12) as response:
            body = response.read()
            ctype = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if body:
            art_bytes = body
            art_type = ctype or "image/jpeg"
            publish()
    except Exception as exc:
        print(f"art fetch failed: {exc}", flush=True)


def ingest(payload: dict):
    track = payload.get("track") or {}
    title = track.get("title") or ""
    artist = track.get("subtitle") or ""
    if not title:
        return
    images = track.get("images") or {}
    art_url = hi_res(images.get("coverarthq") or images.get("coverart") or "")
    share = ((track.get("share") or {}).get("href")) or ""
    key = f"{artist}\n{title}"
    previous = snapshot()["key"]
    set_state(
        title=title,
        artist=artist,
        album=album_from(track),
        art_url=art_url,
        share=share,
        key=key,
        recognized_at=time.time(),
    )
    if key != previous or not art_bytes:
        threading.Thread(target=fetch_art, args=(art_url,), daemon=True).start()


def json_stream(pipe):
    decoder = json.JSONDecoder()
    buf = ""
    while True:
        chunk = pipe.read(4096)
        if not chunk:
            break
        buf += chunk
        buf = buf.lstrip()
        while buf:
            try:
                obj, end = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                break
            if isinstance(obj, dict):
                ingest(obj)
            buf = buf[end:].lstrip()


CLIP = "/tmp/nowplaying-clip.wav"
HW_DEVICE = "hw:0,0"


def mic_bias():
    # Optional. Missing hda-verb must not take the kiosk down.
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
            rec = subprocess.Popen(
                ["arecord", "-D", HW_DEVICE, "-f", "S32_LE", "-r", "44100", "-c", "2", "-d", "7", CLIP],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.25)
            mic_bias()
            rec.wait(timeout=12)
            level = clip_level(Path(CLIP))
            set_state(level=level)
            if level < 0.01:
                print(f"quiet level={level:.4f}", flush=True)
                time.sleep(1)
                continue
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


def boost_mic():
    subprocess.run(
        ["pactl", "set-source-mute", SOURCE, "0"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["pactl", "set-source-volume", SOURCE, "150%"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        if path == "/api/now":
            self._send(200, json.dumps(snapshot()), "application/json")
            return
        if path == "/art":
            if not art_bytes:
                self._send(404, b"", "text/plain")
                return
            self._send(200, art_bytes, art_type)
            return
        if path.startswith("/fonts/"):
            font = ROOT / path.lstrip("/")
            if font.is_file():
                self._send(200, font.read_bytes(), "font/ttf")
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
    boost_mic()
    mic_bias()
    threading.Thread(target=listen_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"now playing on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
