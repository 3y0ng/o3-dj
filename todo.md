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

- [x] Per-venue, per-daypart calibration: set knobs by ear → **save as <daypart> default** (`data/calibration.json`).
- [x] Per-venue volume cap (Newtown 75; the old global 60 was too quiet for a multi-floor venue).
- [x] Venue auto-detected from the Sonos household ID (Newtown recorded), falling back to public IP.
- [ ] Calibrate Newtown for each part of the day (morning, afternoon, early evening, evening, late night).

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

## 5. Roadmap: the DJ itself

- [ ] Smooth transitions: pick the next track to be close in tempo/key to the current one, not just to the target.
- [ ] "Adjust what the DJ chose": pin a track to play next, lock a genre for an hour, "more like this" from a track.
- [ ] Learn from votes per daypart (a track loved at 10am may not suit 11pm), not just globally.
- [ ] Better energy model: add a proper tempo library (librosa/essentia) or embeddings, and compare against votes.
- [ ] Artwork metadata so the Sonos app and screen show full info (duration is now measured).
- [ ] More sources: licensed business music services (Soundtrack Your Brand, etc.), Free Music Archive (check licences),
  a shared Google Drive folder synced into `library/`.
- [ ] Analytics: what played when, skips/downvotes by daypart, hours of music per room; later, correlate with dwell time/sessions.

## 6. Housekeeping

- [x] Tests for queue edge cases (edit near the end, stop vs stall, reset recovery, adopt after restart).
- [ ] Mock doesn't simulate crossfade/TRANSITIONING; add it if transition bugs show up again.
- [ ] Cache: only warm the selected genres heavily; the others need fewer tracks.
- [ ] Confirm Chillify's licence allows commercial/venue playback before the pilot.
- [ ] `.claude/launch.json` preview config can't reach the speakers (macOS Local Network permission for the app);
  run live from a terminal.
