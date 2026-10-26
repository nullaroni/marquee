# now-playing

Fullscreen album-first display for whatever is playing in the room.

A local listener records a few seconds from the microphone, asks [SongRec](https://github.com/marin-m/songrec) (Shazam) what it is, and serves a kiosk page on `http://127.0.0.1:8787`. The cover is the point. Title, artist, and album sit under it.

The page only listens to localhost. Album art is fetched by the server and served at `/art`, so the display does not load Apple's CDN itself.

## Requirements

- Python 3
- `songrec`
- `arecord` (`alsa-utils`)
- `ffmpeg`
- Chromium, if you use the kiosk launcher
- Optional: `hda-verb` (`alsa-tools`), for the iMac CS8409 mic power workaround in `server.py`. If it is missing, the listener keeps running.

On Arch or Omarchy:

```bash
sudo pacman -S --needed songrec alsa-utils ffmpeg alsa-tools
```

## Run

```bash
python3 server.py
```

Open `http://127.0.0.1:8787`.

`server.py` records `hw:0,0` as `S32_LE` at 44100 Hz. That is what the patched CS8409 driver on an iMac18,2 actually accepts. `S16_LE` fails. Change `HW_DEVICE` if the mic is not card 0.

## Install

```bash
mkdir -p ~/.local/share/nowplaying/fonts ~/.config/systemd/user
cp server.py index.html launch-kiosk.sh ~/.local/share/nowplaying/
cp fonts/*.ttf ~/.local/share/nowplaying/fonts/
cp nowplaying.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nowplaying.service
```

`launch-kiosk.sh` waits for `/health`, then opens Chromium fullscreen. Close that window with Super+Q. The listener keeps running.

On Omarchy, start the launcher from `~/.config/hypr/autostart.lua` and keep the window fullscreen:

```lua
o.launch_on_start("/home/you/.local/share/nowplaying/launch-kiosk.sh")
o.window({ class = "chrome-127.0.0.1__-Default" }, { fullscreen = true })
```

The launcher also asks Omarchy to stay awake and hides the cursor. Both are skipped if those tools are not there.

## iMac

Built for an iMac18,2 running Omarchy. The stock CS8409 driver records a pop and then silence, so recognition never starts. The internal mic needs the patched module in [imac-cs8409](https://github.com/nullaroni/imac-cs8409).

## Fonts

[Instrument Serif](https://github.com/Instrument/instrument-serif) is used for the title. It is under the SIL Open Font License 1.1. See `fonts/OFL.txt`.

## License

MIT. The fonts stay under the OFL.
