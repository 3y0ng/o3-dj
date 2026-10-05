# O3 DJ — todo

Review of the test-bench build after the first live session (Office era 100, 25 Sep 2026, 15:55–16:20).
Ordered by priority. **Bugs** are things that went wrong or will go wrong; the rest is roadmap.

---

## 1. Bugs

Fixed 25 Sep 2026 (tests in `tests/test_dj_mock.py`; the mock now reproduces Sonos's reset-to-item-1 behaviour):

- [x] **Re-picking cut off the current song and restarted the queue from the top** (seen live 16:17:49).
  Now: track lengths are measured with ffprobe (Sonos reports 0:00 for these streams); no queue edits in
  the last 35 s of a track (retried after it changes); removal re-reads the live position and never deletes
  the playing item; if the length is unknown the next track is left alone; knobs settle 4 s before re-picking.
- [x] **Stall recovery replayed old tracks.** Recovery now jumps to the first *unplayed* DJ track by URI. If an
  old track starts playing (queue reset), the DJ skips forward to new music.
- [x] **Wrong track blamed/recorded after a reset.** Plays count only once the speaker reports PLAYING; failures only
  when a track never got going (< 3 s).
- [x] **Stop in the Sonos app got overridden.** A mid-song stop now pauses the DJ ("press play to resume");
  a pause in the Sonos app is mirrored too.
- [x] **Mock runs wrote into live state.** Mock uses `data/mock_state.json`. Live history was cleaned
  (backup: `data/state.backup-2026-09-25.json`): 11 real plays kept, the reset "replays" removed.
- [x] **Trims never expired.** Knob trims reset when the real clock enters a new daypart (`reset_trims_on_daypart`).
- [x] **Queued songs ignored the clock.** Daypart changes now trigger a re-pick.
- [x] **`/api/state` crashed with "Set changed size during iteration"** (27 Sep 2026, Python 3.14): the prefetch
  thread added to `Cache.names` while `usage_mb()` summed it. It now iterates a snapshot and tolerates evicted files.
- [x] **Rooms** (27 Sep 2026): pick the main room from the controller; switching the main room off hands over to
  another room in the group; per-room volume knob (5 detents) relative to the main volume; per-room mute switch.
  Not yet tried on the real speakers.
- [x] Analysis failures were permanent; they now retry after 24 h.
- [x] LAN IP change broke queued cached tracks; the health loop now notices and re-queues.
- [x] Downvoting one "up next" track re-picked both; it now swaps just that one (or skips it on arrival if it's too late to edit).
- [x] Double-taps on skip / not this: 3 s cooldown.
- [x] **Restart = silence two songs later.** The DJ now saves that it's running and, on restart, takes back its own queue
  (only if the speaker is really playing it, not e.g. Spotify).

Still open:

- [ ] **Party mode takes over rooms playing something else.** Seen live 16:28: party mode pulled all five rooms into the
  DJ group, then a Spotify stream took over the whole building and the DJ stood down (correctly). The controller
  now asks for confirmation before party mode / joining a room. Better: show what each room is playing and offer
  "join only idle rooms", and remember each room's previous volume/group to restore it when leaving.
- [x] **Play failed while Spotify was playing** (UPnP 712: play mode can't be set on a Spotify Connect stream).
  Start now switches to the queue first; play mode / crossfade are best-effort. The controller shows "spotify is playing"
  and asks before taking over (listing the rooms that will switch). Errors now appear in the DJ log.
- [x] **Skip could cascade through several tracks** (seen live 17:28: one skip, then four "queue jumped back" jumps).
  Right after a skip Sonos reports the new position with the old URI; the DJ trimmed the starting track from "up next",
  then treated it as a reset. The list is now only reconciled when position and track agree, and a track counts as "jumped back"
  only if it has already been played.
- [x] Phone layout (iPhone 320–430 pt): readable screen text, transport first, 16 px inputs (no iOS zoom), double-tap to reset knobs,
  44 px vote buttons, safe-area padding, Add to Home Screen metadata.
- [x] **Skips left a silent gap.** Most queued tracks streamed straight from chillify.me (only already-cached picks got the
  laptop's URL), so a skip had to buffer from the internet. Every track is now queued with the laptop's URL, and the server
  serves the cached file or passes the stream through (range requests supported) until the prefetcher has cached it.
  Skips/"not this" also fade the group volume down, skip, and fade back up once the next track plays (`skip_fade`).
- [ ] Verify the soft skip by ear on the real speakers; tune fade length if it feels slow.
- [ ] Offer "take over only the main room" when the group is playing something else (ungroup first).
- [ ] The Sonos queue grows by ~15-20 tracks an hour and is only cleared when you press play from standby.
  Trim played items occasionally, e.g. once an hour, never within the edit guard window.
- [ ] Crossfade is switched on for the main room (`crossfade` in config) and stays on after the DJ stops. Restore previous settings on stand-down.
- [x] **A song cut out ~36 s before its end** (live 23:11). An internet blip triggered a re-pick that removed the next queue item,
  which the Sonos had already pre-loaded (it pre-loads within seconds when files come from this laptop). The 35 s guard wasn't enough:
  the playing item and the next one are now never removed (enforced in `player.py`), and mood changes apply from the track after next.
- [x] Right after a skip, the stale Sonos status could get a track dropped from "up next" and queued twice. Up next now only advances
  on a real track change (500 randomised runs, 0 failures).
- [ ] If a song is cut short anyway, resume it where it stopped (play its queue index + seek) instead of moving on.

## 2. Tuning (from how it was used)

- [x] **Loudness normalisation** (28 Sep 2026): cached files are levelled to −16 LUFS (`normalise` in config.json).
  The first 25 Chillify tracks measured −13.3…−15.5 LUFS, so the target keeps the tuned volumes about where they were.
- [ ] Level tracks that play before they're cached (the server passes the stream through as it is), e.g. per-track volume compensation.
- [ ] Level `library/<genre>/` files too (into the cache, leaving the originals alone).
- [ ] Listen for the limiter on quiet tracks boosted by several dB; lower `max_boost_db` if they sound squashed.

- [x] One-time calibration walk-through (6 scenarios, adjust by ear, fit volume/energy factors per venue).
- [x] Per-venue volume cap (Newtown 75; the old global 60 was too quiet for a multi-floor venue).
- [x] Venue auto-detected from the Sonos household ID (Newtown recorded), falling back to public IP.
- [ ] Run **calibrate** at Newtown, ideally when the room is in a typical state; re-run after a week of use.
- [ ] Let calibration also learn genre preferences per scenario (e.g. more jazz when raining).

- [x] The first session ran at the minimum energy trim and volume −7…−30, so the curve was lowered:
  afternoon 0.70/42 → 0.45/36, evening 0.38/28 → 0.25/26, late night 0.25/22 → 0.18/20. The energy trim range is now ±0.5.
  Saved trims were reset to 0.
- [ ] Re-check by ear over a few days and adjust `daypart_curve`; ideally log trims so the defaults can be learned.
- [ ] Check the energy scale by ear: listen to 5 tracks near 0.1 and 5 near 0.9 per genre. The tempo estimator
  may double/halve some lo-fi tempos; votes will show which way.
- [ ] Per-room offsets (`room_volume_offsets`) once party mode is used: entrance louder, study floors quieter.
  (The Storage room was at 58 while the others were at 38 after the Spotify takeover.)

## 3. Before staff use it

- [ ] **Access control.** Anyone on the Wi-Fi can open the controller. Check whether customers share this network
  (the speakers are on 192.168.0.x/1.x). Minimum: a staff PIN; better: sign-in tied to the O3 app.
- [ ] **Staff view vs admin view.** Staff: now playing, up next, 👍 / 👎 / skip, "quieter please". Admin: knobs, rooms, genres.
- [ ] **Run as a service.** launchd job with auto-restart, `caffeinate` so the laptop doesn't sleep (the speakers stream
  cached files from it), log to file. Longer term, a dedicated box (Mac mini / Raspberry Pi) per venue.
- [x] **Resume after a restart.** Done (see bugs).
- [ ] Real web server (waitress/gunicorn) instead of Flask's dev server.
- [ ] Rate-limit skips/downvotes per person (there's only a global 3 s double-tap guard), so one person can't skip the whole playlist.

## 4. Roadmap: live data instead of manual toggles

- [x] DEMO / LIVE switch, override → live → neutral layering, 15 min staleness, venue timezone, Open-Meteo weather,
  configurable read-only Supabase occupancy (`count` or `rpc`), screen badges. See `docs/live-mode-plan.md`.
- [ ] **Configure occupancy**: find the check-ins table (or an existing live-count function) and a read-only key; set venue
  `timezone`, `lat`/`lon`, `capacity`. Needs the O3 app repo or whoever knows the schema.

All of these only need to POST to `/api/inputs`.
- [ ] **Weather feed.** Open-Meteo (free, no key) or BOM per venue; map rain/cloud → the weather keys. Keep the manual override.
- [ ] **Occupancy feed.** Check-ins/bookings from the O3 app, a door counter, or Wi-Fi client count as a cheap proxy.
- [ ] **Ambient noise.** A mic level reading; raise the music slightly to mask chatter, drop it when the room is silent-study quiet.
- [ ] **Schedules.** Quiet hours / exam-season mode, "focus blocks" (e.g. calmer on the hour for pomodoro sessions).
- [ ] **Per-venue profiles.** Newtown, Southbank, Brunswick and Auckland each with their own curve, rooms, genres and timezone.

## 5. Suno music + mood strategy (28 Sep 2026)

Thesis: music anchors the room (energised, focused, good mood); chill is the centre. Volume counters occupancy,
genre complements time/weather. Built: `moods` matrix + **auto** key, occupancy `bands` (energy/BPM/instrumental),
genre BPM ranges + tempo matching, Suno sidecars, Chillify fallback while a Suno genre has < `min_tracks`,
`tools/suno_generate.py` + `suno/styles.json`. Tested in `--mock` and tests only.

- [ ] **Pilot (Phase 0)**: run `--pilot` on a Pro/Premier plan against the 5 starred references; score 1–5 by ear;
  continue only if 3 of 5 styles score 4+. `_QqabGYTSpQ` has embedding disabled, so its prompt was written from the label
  "whimsy / mellow fantasy" without the title; listen to it and refine. `vHzykyvBt-Q` is actually vintage American lofi
  (filed under whimsy fantasy); `GT_J6k5Dyqg` is late-night smooth jazz (used for ambient dream and listed for evening).
- [ ] Set up gcui-art/suno-api and confirm the endpoints still match (`/api/custom_generate`, `/api/generate`, `/api/get`,
  `/api/get_limit`); the tool has only been tested against a fake backend.
- [ ] Confirm the Suno plan and keep proof of subscription for the period songs were generated.
- [ ] Fill each genre to ≥ 8 tracks (then ~30+ so repeats aren't noticeable); tweak `suno/styles.json` from what the pilot shows.
- [ ] Tune `moods` weights and the `bands` by ear; the +0.15 energy for an empty room may be a lot on top of the afternoon peak.
- [ ] "dark academia" is clipped on its genre key; the genre row now has 11 keys and wraps on phones.
- [ ] Ask whether staff genre picks should also turn **auto** off automatically.
- [x] First listen (30 Sep 2026): prompts simplified; bossa 108→138 bpm climbing through the morning (`bpm_ramp`) and on cloudy
  mornings; jazz-hop up to 88–96; afternoon is now instrumental "Ghibli Lofi" 86–94 (no rap, no voices); whimsy softened;
  evening / dark academia / ambient kept. No four-on-the-floor kicks (they feel anxious). Notes are in `suno/styles.json`.
- [x] New music is peppered in (5 Oct 2026): `new_music` share starts at 0.4 and reaches 1.0 as each slot's genres hit 30 songs; Chillify is the fallback.
- [ ] Final listen: one Suno song per timeslot made on the O3 Aus account (Pro), titled "O3 ...". Then connect the API.
- [x] Second batch (5 Oct): same prompts with Weirdness 65 / Style Influence 60 / Variety High. Bossa now 120→140 bpm across the morning;
  flute added to Exclude styles (sent as `negative_tags`; check the real API honours it).
- [x] Afternoon cloudy (5 Oct): replaced whimsy with mellow celtic / elven folk fantasy (cello, harp, dulcimer; Frieren-inspired).
  Two approved prompts rotate; own exclude list (flute allowed, bright/epic/choir/drums excluded); 66-76 bpm.
  Suno settings per style (`suno_settings`) aren't sent by the generator yet: check whether the API supports them.
- [x] API (5 Oct): dropped the unofficial suno-api (needs 2Captcha to solve Suno's hCaptcha). Songs are made in the Suno web app
  and added with `tools/suno_import.py`; `export`/`unpack` move them to the live DJ device in one zip.
- [ ] Pro plan = 20 downloads a month (19 left on 5 Oct, resets 30 Oct). First pick: best 2-3 per timeslot.
- [ ] Get the new code onto the live DJ device (merge to main, `git pull` there) before unpacking songs.
- [x] A genre staff switch on plays as chosen even with few songs; only genres the moods matrix adds fall back to Chillify.
- [ ] Decide where Electronic Lofi (6 Oct, "Cloudy Afternoon Soul") sits in `moods`; it's a genre key only for now.
- [ ] Try classical and Ghibli-style music as extra genres.
- [ ] Busy afternoons: staff want more upbeat, but the 70–90% band lowers energy/BPM. Decide which wins.
- [ ] Future: generate music on the fly for the current slot; more parameters (key, instruments); lighting / further mood control.

## 6. Roadmap: the DJ itself

- [ ] Smooth transitions: pick the next track to be close in tempo/key to the current one, not just to the target.
- [ ] "Adjust what the DJ chose": pin a track to play next, lock a genre for an hour, "more like this" from a track.
- [ ] Learn from votes per daypart (a track loved at 10am may not suit 11pm), not just globally.
- [ ] Better energy model: add a proper tempo library (librosa/essentia) or embeddings, and compare against votes.
- [ ] Artwork metadata so the Sonos app and screen show full info (duration is now measured).
- [ ] More sources: licensed business music services (Soundtrack Your Brand, etc.), Free Music Archive (check licences),
  a shared Google Drive folder synced into `library/`.
- [ ] Analytics: what played when, skips/downvotes by daypart, hours of music per room; later, correlate with dwell time/sessions.

## 7. Housekeeping

- [x] Controller (28 Sep 2026): knobs replaced by faders (drag/scroll, relative to where you grab); rooms are channel strips
  with the name printed on the panel and an **on** switch instead of a room-sized button; long song names scroll on the screen.
  Mock rooms now use the real names ("Mock 2nd Floor Painting Corner") so layouts get tested with long names.
- [x] **listen** switch on the unit's left side (28 Sep 2026): hear what's playing on the controller device, synced to the speaker's position
  On by default in demo mode. Fixed the same day: it re-seeked back 3 s whenever the (2 s old, whole-second) status lagged,
  which sounded like an echo; status now carries a precise position and its age, and small drift is corrected by speed.
  The main room's mute / offset no longer silences it.
  A page that's on only by the demo default goes quiet while it's in the background (another tab or window), so a
  forgotten tab can't keep playing; switched on by hand, it keeps playing (phone locked).
  Mock songs now last their real length (they were cut at `mock_track_seconds`, 45 s, which sounded like random skips).
- [ ] listen on real Sonos: positions are whole seconds and the speaker's own buffering delay is unknown; check by ear
  standing next to a speaker, and add a fixed latency offset if the device is consistently early.
- [ ] listen: a Sonos crossfade isn't reproduced (the device cuts to the next track); only our own music can be heard, not Spotify.
- [ ] Re-take `docs/screenshot.png` (it still shows the knob version).
- [ ] Scrolling the page with a mouse wheel or trackpad over a fader moves the fader (same as the old knobs). Fine on phones
  (touch scrolls the page); consider ignoring wheel events for ~300 ms after the page scrolls.

- [x] Tests for queue edge cases (edit near the end, stop vs stall, reset recovery, adopt after restart).
- [ ] Mock doesn't simulate crossfade/TRANSITIONING; add it if transition bugs show up again.
- [ ] Cache: only warm the selected genres heavily; the others need fewer tracks.
- [ ] Confirm Chillify's licence allows commercial/venue playback before the pilot.
- [ ] `.claude/launch.json` preview config can't reach the speakers (macOS Local Network permission for the app);
  run live from a terminal.
- [ ] **listen on iPhone still hard-cuts between songs**: iOS Safari ignores `audio.volume`, so the listen crossfade
  (two players, equal-power, `XFADE_S` in `web/app.js`) only works on desktop/Android. A Web Audio gain would fix it but
  suspends when the phone locks.
