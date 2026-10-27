# marquee

A fullscreen room display. It listens to the microphone, recognizes the song, and shows the album art. After the room has been quiet for a while it switches to a Cinemark showtimes board with trailers.

A local server records short clips, asks [SongRec](https://github.com/marin-m/songrec) (Shazam) what is playing, and serves the kiosk page on `http://127.0.0.1:8787`. Album art is fetched by the server and served at `/art`. Trailers start muted; say "sound on" to hear one.

## Requirements

- Linux with ALSA and PipeWire (PulseAudio compatible)
- Python 3.9+
- `songrec`, `arecord` (`alsa-utils`), `pactl`, `ffmpeg`
- Python package `vosk` and its small English model (`vosk-model-small-en-us-0.15`) for voice commands. The model is downloaded on first start if missing. Without Vosk, everything else still works.
- Chromium, for the kiosk launcher
- Optional: Omarchy/Hyprland (the launcher uses `omarchy-toggle-idle` and `hyprctl` when present), `hda-verb` (`alsa-tools`) for the iMac CS8409 mic workaround

`install.sh` installs the system packages it can (apt, pacman, or dnf) and creates a virtualenv with `vosk`. `songrec` is not in the apt repos, so install it yourself (AUR `songrec-bin` on Arch, or `cargo install songrec`).

The mic settings are constants at the top of `server.py` (`HW_DEVICE`, `SOURCE`). They are set for an iMac18,2 with the patched [imac-cs8409](https://github.com/nullaroni/imac-cs8409) driver. Change them for other hardware.

## Install

```bash
./install.sh                      # install or upgrade
./install.sh --skip-packages      # skip the system package step
./install.sh --uninstall          # remove the service and app, keep config
./install.sh --uninstall --purge  # also remove config, Chrome profile, and models
```

The installer copies the app to `~/.local/share/marquee`, installs the user service `marquee.service`, and starts it. Run it as your normal user; it uses sudo only for packages.

To try it without installing: `python3 server.py`, then open `http://127.0.0.1:8787`.

`marquee-kiosk` (installed to `~/.local/bin`) waits for `/health`, then opens Chromium fullscreen with a profile in `~/.local/share/marquee/chrome-profile`. Close the window with Super+Q; the listener keeps running. On Omarchy, start it from `~/.config/hypr/autostart.lua`:

```lua
o.launch_on_start("/home/you/.local/bin/marquee-kiosk")
o.window({ class = "chrome-127.0.0.1__-Default" }, { fullscreen = true })
```

## Configuration

Open `http://127.0.0.1:8787/setup` to pick your theater by location or text search. The choice is saved to `config.json` next to `server.py`. You can also copy `config.example.json` to `config.json`.

| Field | Meaning |
| --- | --- |
| `theater_id` | Cinemark theater id |
| `theater_name` | Display name |
| `quiet_for_cinema` | Seconds of room quiet before the showtimes board appears (default 120) |

## Voice commands

Vosk listens for short commands. The exact phrases are in `voice.py`. Categories:

- Sound on / off for trailers (for example "sound on", "unmute"). Sound is for the current trailer only.
- Lyrics on / off.
- An optional wake word ("marquee"). While a trailer is audible the mic hears the movie, so use the wake word to mute.

## HTTP API

All on `127.0.0.1:8787`.

| Route | Purpose |
| --- | --- |
| `GET /` | Kiosk page |
| `GET /setup` | Theater setup page |
| `GET /health` | Returns `ok` |
| `GET /api/now` | Current state as JSON |
| `GET /events` | Server-sent events with the same state |
| `GET /art` | Current album art |
| `GET /api/config` | Current config |
| `POST /api/config` | Update config (JSON body) |
| `GET /api/theaters` | Find a theater: `?lat=..&lon=..` or `?q=..` |
| `POST /mute` | Re-mute trailer sound |
| `GET /fonts/...` | Bundled fonts |

## Security

The server binds to `127.0.0.1` only. POSTs must send a JSON content type and a local Origin. There is no authentication, so do not expose the port to a network (no port forwarding, no `0.0.0.0` proxy).

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

## Credits and license

MIT, see `LICENSE`. [Instrument Serif](https://github.com/Instrument/instrument-serif) is used for titles under the SIL Open Font License 1.1 (`fonts/OFL.txt`).
