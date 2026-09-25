# CLAUDE.md

Atmosphere-aware Sonos DJ for the O3 study cafes, with an OP-1 style web controller.
Read `README.md` for how it works and `todo.md` for what's next. Keep todo.md current when fixing or finding things.

## Commands

```bash
python3 run.py --mock      # silent simulated speakers, own state (data/mock_state.json), port 8330
python3 run.py             # REAL speakers in a working cafe; see safety below
python3 -m pytest -q       # tests; no network or speakers needed
```

Stack: Python 3.11, Flask, SoCo, numpy, ffmpeg/ffprobe (tempo + duration). Frontend is plain HTML/CSS/JS in `web/`
with no build step and **no external assets** (no web fonts or CDNs), because it must work when the internet is down.

## Safety: these are live speakers in a public cafe

- Never press play, party mode, join rooms or change volume on the real system unless the user asks for that action.
  Reading state (discover, status, queue, volumes) is fine. Test behaviour with `--mock` and the tests.
- Pressing play replaces whatever the main room's group is playing (often staff Spotify across all rooms).
- Speakers: Office era 100 `192.168.1.136` (main room in config), Cafe Entrance `.1.94`, 2nd Floor Painting Corner `.1.205`,
  2nd floor Storage room `.1.68`, Mazenine Floor `192.168.0.93`. The laptop must be on the Wi-Fi that gives it a
  `192.168.0.x` address; another Wi-Fi (`192.168.24.x`) can't reach them. Check `ipconfig getifaddr en0` first.
- Processes launched by the Claude desktop preview pane get "No route to host" to the speakers (macOS Local Network
  permission). Run the live server from a terminal/Bash instead.

## Architecture

- `o3dj/brain.py`: pure logic. Atmosphere inputs → targets (energy, volume, genre weights, reasons), and track picking.
- `o3dj/dj.py`: the stateful core. Keeps ~2 tracks queued ahead on the Sonos, re-picks when the mood changes,
  ramps volume, follows what the speaker is doing, recovers from failures, adopts its own queue after a restart.
- `o3dj/player.py`: `SonosPlayer` (SoCo) and `MockPlayer` share one interface. Keep them in step; the mock
  deliberately reproduces Sonos quirks (duration reported as 0, removing the playing item resets to item 1).
- `o3dj/library.py`: sources (Chillify catalogue, `library/<genre>/` files, `library/custom_tracks.json`).
- `o3dj/cache.py`: download cache + a background worker that measures durations, downloads upcoming/warm tracks, and analyses tempo.
- `o3dj/analysis.py`: ffmpeg + numpy tempo/energy; energy is a percentile across analysed tracks. `track_meta.json` also stores durations.
- `o3dj/server.py`: JSON API, `/ui/*` static files, `/media/*` (speakers stream cached files from this laptop over the LAN).
- `data/` (gitignored): `state.json` (inputs, votes, history, running), `track_meta.json`, `cache/`, catalogue snapshot.

## Sonos gotchas (learned live; don't regress)

- Sonos reports duration `0:00:00` for these http streams. Use `meta.duration()` (ffprobe) instead.
- Never remove the playing item **or the next one** (`PROTECTED_AHEAD` in `player.py`). Sonos pre-loads the next item as
  soon as the current file is buffered (seconds, for files served from this laptop), and removing it stopped playback and
  reset the queue to item 1. That happened live twice: ~20 s and ~40 s before a track's end, so a time guard is not enough.
  Mood changes re-pick from the track after next. `drop_from` re-reads the live position before removing.
- Don't trim "up next" by queue position: right after a skip Sonos reports the new position with the old URI.
  Advance only when the reported URI changes (`_on_playing`).
- On a reset or failure, jump to the first *unplayed* DJ track (`_jump_to_next_unplayed`); never replay from the top.
- Count a play only once the speaker reports PLAYING.
- Play mode / crossfade can't be set while the source is Spotify Connect (UPnP 712). Switch to the queue first, and treat those settings as best-effort.
- A foreign URI playing (Spotify, `x-sonos-vli:`) means someone else took over: the DJ stands down and doesn't adopt it.
- Sonos crossfade only applies to natural track changes, not manual skips; skips use our own group-volume fade (`_fade_skip`).
- Always queue tracks with this laptop's `/media/cache/<name>` URL (not the remote URL): cached files start instantly and
  survive internet drops; `server.py` passes uncached ones through from the source.
- Queue item IDs `Q:0/n` are positions (SoCo's `remove_from_queue` is 0-based).

## Conventions

- Tuning belongs in `config.json` (`daypart_curve`, `weather`, `occupancy`, volume limits); keep `brain.py` free of constants that staff might want to change.
- New queue/transport behaviour needs a test in `tests/test_dj_mock.py`, extending `MockPlayer` if Sonos behaves differently.
- Future live data (weather, occupancy) should just POST to `/api/inputs`.
- Only play music licensed for commercial venues; check the licence before adding sources.
