# O3 DJ

A live DJ for the O3 study cafes. It picks music to suit the room (time of day,
weather, how busy it is) and plays it on Sonos speakers. You drive it from a
small controller styled after the teenage engineering OP-1.

![O3 DJ controller: tape-deck screen, colour faders, genre/weather/mood/transport keys and a channel strip per room](docs/screenshot.png)

<sub>Mock mode, early evening with rain: the DJ is mixing chill with jazzy cafe (69% jazz) at low energy, two rooms synced.</sub>

This is the **test bench** version: atmosphere inputs are manual faders and keys,
not yet wired to live data (clock aside). It runs on a laptop on the same Wi-Fi as the speakers.

```bash
pip3 install -r requirements.txt       # plus ffmpeg for tempo analysis: brew install ffmpeg
python3 run.py --mock                  # silent: simulated speakers, own state file, safe to play with
python3 run.py                         # real Sonos (main room IP in config.json)
```

Open http://localhost:8330. Anyone on the same Wi-Fi can use the address printed at startup.

**On a phone** (same Wi-Fi): open the printed address, e.g. `http://192.168.0.70:8330`, in Safari, then
Share → *Add to Home Screen* for an app-like icon. The phone layout puts play / skip / votes first. Drag a fader up or down
to move it, and double-tap to reset it.

## The controller

| control | what it does |
|---|---|
| **blue fader** volume | shows the volume the DJ is aiming for. In *auto vol* it moves by itself as the mood changes; moving it trims the automatic level. With auto off: sets the volume directly. Double-click resets. |
| **green fader** energy | nudges how upbeat the picks are (centre = as the DJ sees it). Double-click resets. |
| **white fader** occupancy | sets how full the space is (moving it switches **occ** on). Volume rises as the room fills (to sit above the chatter), while energy and tempo counter it: an empty cafe gets livelier music, a packed one calmer, and above 90% instrumentals are preferred. |
| **orange fader** time | simulates a time of day (bottom = midnight) so you can test evening vs afternoon. **clock** key or a double-click returns to the real time. |

Fader trims (volume/energy) reset automatically when the real clock moves into the next part of the day.
| **genre** keys | two rows: the playlists (chill, jazzy cafe, asian), then the new music in the order it plays through the day. Pick several. The counts show cached/total tracks. **auto** (on by default) adds the genres that suit the time of day and weather (`moods` in config.json) on top of your picks; switch it off to play only what you picked. |
| **weather** keys | cloudy leans jazzier; rain leans jazzier, softer and calmer, and brings jazz in even if not selected. |
| **play / skip** | play starts the DJ. **This replaces the main room's Sonos queue.** |
| **listen** (slide switch on the unit's left side) | plays what the speakers are playing on this phone or laptop, in step with them (it streams the same file from the DJ laptop and follows the speaker's position to within a few hundredths of a second in mock mode, about half a second on real Sonos, nudging its speed rather than jumping). It acts as one more speaker in the group: it follows the group level, so fades and auto volume can be heard, but not the main room's own level offset or mute. The device volume sets the overall level. On by default in demo mode (only while the page is in front, so a forgotten tab stays quiet), off in live mode; sliding it remembers your choice on that device, and switched on by hand it keeps playing in the background. Browsers only start sound after a tap, so if its LED blinks, tap anywhere. In `--mock`, songs last their real length (measured once cached; `mock_track_seconds` until then). |
| **love it / not this** | votes. "not this" skips; 3 net downvotes remove a track from rotation. You can also vote on "up next". One vote per song per play, from anyone: extra taps while it plays don't count. |
| **feedback** (after not this) | staff notes on the music: tap reasons (too loud, too quiet, wrong style, bad song, too energetic, too sleepy, other), add a note and your name. Each one is saved with the time, song, style, volume, occupancy, weather and time of day in `data/feedback.jsonl`; the pop-up shows recent feedback, and **download all (csv)** (`/api/feedback.csv`) exports everything for tuning. |
| **skip** | with crossfade on, jumps to the last `skip_crossfade_seconds` (8) of the song so the speakers crossfade into the next one; if the song's length isn't known yet, a quick fade down and up instead. |
| **rooms** | one channel strip per speaker, with the room name printed at the top and a small LCD showing its state and volume. The **on** switch adds the room to the synced group or takes it out. Switching off the **main** room hands the music to another room in the group, which becomes main; if it's the only room playing, the music pauses. |
| **main** (on each room) | makes that room the main room: it leads the group and holds the queue. The music carries on; a room that's off joins first. Remembered across restarts. |
| **room fader** (on each room) | that room's volume relative to the main volume, in five printed detents (−10, −5, 0, +5, +10; `room_offset_steps` in `config.json`). Drag, scroll or arrow keys; double-click resets. Saved per room; starts from `room_volume_offsets`. |
| **mute** (slide switch, on each room) | mutes that room on the Sonos. It stays in the group, so unmuting is instant; a mute set in the Sonos app shows here too. |
| **eq** (next to party mode) | tone. Press it and the blue and green faders become **bass** and **treble** (−10 to +10, the speakers' own Sonos EQ) for all rooms, and the screen shows the tone curve. Tap a room's name plate to tune just that room on top (tap again for all rooms); each room's LCD shows its tone meanwhile. Press eq again, or leave it for 10 s, to go back. Double-click a fader resets it to flat. The LED is on when the tone isn't flat. The DJ keeps rooms in its group at this tone (putting it back within a minute if it's changed in the Sonos app); Sonos loudness stays on. On its first run it starts from whatever the speakers were already set to. |
| **add track** (next to eq) | opens a small form to add a song from a stream URL (or drop files into `library/<genre>/`). |

Song names too long for the screen scroll across it, pausing at the start of each pass.
The screen shows why the DJ is doing what it's doing: `early evening -> energy 0.42 · rain energy -0.08 +Jazzy Cafe`.
Energy marker ▼ = target, green bar = current track.

## Calibrating a venue (once, about 5 minutes)

It's one key, **calibrate** (next to the demo/live switch), and the screen shows everything else.

1. **Press calibrate.** The speakers play a short song (starting 45 s in) for the first of six scenarios: quiet morning,
   lunchtime rush, rainy afternoon, busy after work, winding down, and a rainy near-empty night. The screen shows the
   scenario (time, weather, how full), the song, the current volume and energy, and a key legend.
2. **Adjust by ear:** **blue fader** louder/softer, **green fader** faster/slower (a new song starts once you let go),
   **skip** for another song.
3. **Press calibrate** (now labelled *sounds right*) for the next scenario.
4. After the sixth, the screen shows what it learned in plain words: overall level, packed room, empty room, rain, cloud.
   **Press calibrate** (*apply*) to keep it, or **hold** it to discard.

Hold the key at any point to cancel (nothing changes). When not calibrating, holding it resets the venue to the defaults.
The result is saved for this venue in `data/calibration.json` on the DJ laptop, and the DJ goes back to normal.

The faders still work day to day as temporary tweaks (they reset when the time of day moves on). Each venue also has a
`max_volume` safety cap in `config.json` (Newtown 75, others 60).

## Which venue am I in?

The DJ works it out on startup, from the **Sonos system** it's controlling (each venue's Sonos household has a unique, permanent
ID, listed under `venues.<code>.sonos_households` in `config.json`), falling back to the network's public IP. The startup log
prints `venue: Newtown (sydney_01, from sonos)`. For a new venue, run it once, copy the household ID from the "not identified"
message into `config.json`, and you're done. `live.venue` in `config.local.json` overrides detection.

## Demo and live mode

The **live data** key switches modes (details and setup: [docs/live-mode-plan.md](docs/live-mode-plan.md)).

- **Demo** (default): you set weather, occupancy and time of day with the keys and faders.
- **Live**: time follows the venue clock, weather comes from Open-Meteo, and occupancy is read from O3's Supabase (read-only,
  no database changes). Pressing a weather key or moving the occupancy fader overrides live data for an hour; press the lit
  weather key again, or the occ key, to hand back early. If a feed goes quiet for 15 minutes the DJ ignores it (`stale`)
  rather than acting on old data.

Set it up in `config.local.json` (gitignored; copy `config.local.example.json`): the venue code, the O3 app's public key,
and an O3 app account for the DJ to sign in with. Occupancy comes from O3's existing `get_location_occupancy_counts` function,
and capacity from the venue's red threshold. Details: [docs/live-mode-plan.md](docs/live-mode-plan.md).

## How it decides

`o3dj/brain.py` is pure logic. Tune it in `config.json`:

- **daypart_curve**: energy + volume by hour (a 24h cafe: calm late night, peak in the afternoon, winding down in the evening).
Thesis: music anchors the room's atmosphere. Chill is the centre (people feel they can lock in); **volume counters
occupancy** (louder as the room fills) and **genre complements time and weather**.

- **weather**: energy/volume nudges and genre boosts.
- **moods** + **mood_hours**: genres for each part of the day (morning 6–12, afternoon 12–17, evening 17–22, after hours)
  and weather, e.g. morning sun → bossa nova, rainy evening → dark academia, after hours → ambient dream.
  They're added on top of the selected genres (the **auto** key).
- **occupancy**: `threshold`/`max_volume` raise volume as the room fills; `bands` set what the room needs:

  | occupancy | need | adjustment |
  |---|---|---|
  | 0–15% | make an empty cafe feel alive | +0.15 energy, +8 BPM |
  | 15–40% | warmth / momentum | +0.05 energy |
  | 40–70% | ideal natural atmosphere | baseline |
  | 70–90% | reduce sensory load | −0.10 energy, −5 BPM |
  | 90–100% | calm the room | −0.20 energy, −10 BPM, instrumental bias |
- **genres[...].bpm / fallback**: a genre's tempo range (target = middle + the band's BPM shift) and the genre that
  stands in while it has fewer than `min_tracks` tracks (its weight moves over in proportion).
- **new_music**: the slot's new (Suno) genres get `start_share` (0.4) of the picks, rising to `full_share` (1.0) as each fills to
  `full_at_tracks` (30) songs; the current Chillify genres cover the rest, and stand in for a new genre that has fewer than `min_tracks`.
  Morning and evening keep Chill as 20% of their mix even when the Suno library is full (fewer songs to make).
- **occupancy.volume_rules**: e.g. `{"part": "afternoon", "below": 60, "volume": -5}`: afternoons (12pm to 5pm)
  under 60% full play 5 volume steps quieter. Only when occupancy is known.
- **min_genre_run** (2): a genre plays at least this many songs in a row, so the music doesn't jump between styles,
  as long as it has a song that hasn't played recently.
- **genres[...].volume_offset**: plays that genre a little quieter or louder (Sonos volume steps, auto volume only),
  e.g. -3 for Dark Academia and Ambient Dream: levelling brings quiet styles up to the same loudness as busier music.
- **source_weight** (optional per-source preference within a genre), **vocals_penalty** (×0.3 for vocal tracks when the room needs calm).
- **min_volume / max_volume**: hard safety limits. **room_volume_offsets**: starting per-room levels, e.g. `{"Cafe Entrance": 4}`; the fader on each room overrides them.

Picking: choose a genre by weight, then a track whose **energy** (and, where the genre has a BPM range, **tempo**) is
close to the target, weighted by votes, avoiding the last 60 plays. A Suno track's tempo comes from its sidecar. Energy comes from analysing
each track's audio (tempo, loudness, rhythm density, brightness; see `o3dj/analysis.py`),
and is ranked relative to the whole library. Chillify has no tempo metadata,
so analysis fills in in the background (~0.1 s per cached track; streaming
tracks are analysed from a 60 s range read). Unanalysed tracks can still be picked, just less often.

The DJ only keeps ~2 tracks queued ahead on the speaker. When you change the mood,
the upcoming picks are replaced within a couple of seconds and the current song plays out.

## Loudness

Every cached track is levelled once to the same loudness (EBU R128 integrated loudness, `normalise.target_lufs`,
default −16 LUFS), so no song is much louder or quieter than the rest and the volume numbers mean the same thing for
every track. The prefetcher measures each file with ffmpeg and rewrites it with the gain applied (boosts go through a
limiter, capped at `max_boost_db`; tracks already within `skip_within_db` are left alone). The screen shows the gain,
e.g. `lvl −1.9 dB`. The playing track and the next one (which Sonos pre-loads) are never rewritten; they get levelled
next time. Results are in `data/normalised.json`, with the target each file was levelled for, so changing
`target_lufs` re-levels the cache in the background. Not levelled yet: tracks still streaming through before they're cached,
and files in `library/` (they're played as they are; Suno downloads are levelled by the generator).

## Resilience

- **Patchy Wi-Fi**: upcoming tracks are downloaded ahead. When a track is cached, the speakers
  stream it from this laptop over the local network, so an internet drop mid-song doesn't cut out.
- **Source down**: 40 tracks per genre are kept warm in `data/cache/` (`cache.warm_per_genre`,
  capped by `cache.max_mb`). If chillify.me can't be reached, the DJ plays only cached/local tracks
  and switches back when it returns. The catalogue itself is snapshotted too.
- **A track won't play**: it's skipped; after two failures it's rested for an hour.
- **Someone plays Spotify on the speakers**: the DJ notices and stands down until you press play.
- **Paused/stopped from the Sonos app**: the DJ respects it and waits for play.
- **The DJ restarts**: it takes back its own queue and carries on, as long as the speaker is still playing it.
- **Queue safety**: the DJ never removes the playing track or the next one (the Sonos pre-loads it), so mood changes
  take effect from the track after next. If the speaker jumps back to an old track, it skips forward to new music instead of replaying.

The laptop has to stay on and awake. `./dj.sh start` runs the DJ in the background under `caffeinate`, independent of any
terminal window (`./dj.sh status`, `log`, `restart`, `stop`; log in `data/server.log`). It also has to
allow incoming connections if the firewall asks, so the speakers can fetch cached files.

## Adding music

See [library/README.md](library/README.md): drop files into `library/<genre>/`, paste stream URLs into
the "add a track" box, or write a new source class in `o3dj/library.py`.

**Suno**: make songs in the Suno web app from the prompts in `suno/styles.json`, download them, then
`python3 tools/suno_import.py add ~/Downloads/O3*.m4a`, then `python3 tools/suno_import.py export` and, on the DJ device,
`./dj.sh update` (pulls the code, unpacks the newest zip from Downloads, restarts).
See [library/README.md](library/README.md#suno) for the steps and the licence rule.
Only play music O3 is licensed to use in a commercial venue.

## Layout

```
run.py              entry point (threads: dj loop, health check, prefetch/analysis; Flask server)
config.json         tuning; put machine-specific overrides in config.local.json (gitignored)
o3dj/brain.py       atmosphere -> targets, track picking (pure, tested)
o3dj/dj.py          rolling queue, volume ramps, votes, fallbacks
o3dj/player.py      SonosPlayer (SoCo) and MockPlayer
o3dj/library.py     sources: Chillify, library/ folders (+ Suno sidecars), custom URL list
suno/styles.json    Suno prompts per genre, and the pilot against the reference tracks
tools/suno_import.py    add downloaded Suno songs; export/unpack them to the DJ device
tools/suno_generate.py  generator for an official Suno API, if one appears (never run by the DJ)
o3dj/cache.py       download cache + background prefetch/analysis
o3dj/analysis.py    ffmpeg/numpy tempo & energy
o3dj/server.py      JSON API, UI, /media for speakers
web/                the controller (plain HTML/CSS/JS, no external assets)
data/               runtime state, cache, votes (gitignored)
```

API: `GET /api/state`, `POST /api/control {action: play|pause|skip|up|down, id?}`,
`POST /api/inputs {genres, moods, weather, occupancy, occupancy_enabled, hour_override, auto, manual_volume, volume_trim, energy_trim}`,
`POST /api/speakers {action: party|join|leave|main|offset|mute|discover, ip?, value?}`, `POST /api/eq {bass?, treble?, room?, reset?}`, `POST /api/tracks {url, title, genre}`.
These are the hooks for live data later: a weather or occupancy feed only needs to POST to `/api/inputs`.

## Not yet

- No login: anyone on the Wi-Fi can open the controller. Fine for testing; add auth before staff use.
- Live feeds (weather API, door counter / booking occupancy) aren't connected yet.
- Energy estimates are heuristic; votes gradually correct them.

Tests: `python3 -m pytest -q`
