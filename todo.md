# O3 DJ — todo

Review of the test-bench build after the first live session (Office era 100, 25 Sep 2026, 15:55–16:20).
Ordered by priority. **Bugs** are things that went wrong or will go wrong; the rest is roadmap.

---

## 1. Bugs (fix first)

- [ ] **Re-picking "up next" can cut off the current song and restart the queue from the top.**
  Seen live at 16:17:49: *Vacuum Ink* (2:48 long, started 16:15:21) stopped ~20 s early while
  knob turns were triggering re-picks. The Sonos jumped to queue item 1 (*Sunset Groove*, the first
  track of the session), sat stopped, and the DJ recovered by playing item 2, a **repeat** of *Daydream Echoes*.
  Queue IDs (`Q:0/n`) match positions, so the removal index maths is right; the likely cause is
  editing the queue while the Sonos is pre-loading the next track for crossfade.
  Fix ideas (do several):
  - never touch index+1 in the last ~30 s of a track. This needs track duration, which Sonos reports as
    `0:00:00` for these streams, so store duration from ffprobe during analysis/caching;
  - re-read the position right before removing, and refuse to remove the current URI;
  - debounce re-picks harder while a knob is moving (currently 2.5 s after the last change);
  - consider keeping index+1 fixed and only re-picking index+2 onwards (mood lags one song, but it's safe).
- [ ] **Stall recovery replays old tracks.** When the Sonos stops, `tick()` plays `index + 1`, which after a queue
  reset means replaying from the start. Recovery should jump to the first *unplayed* DJ track by URI
  (or top up and play the new last item), never an earlier one.
- [ ] **Wrong track blamed/recorded after a reset.** The same event logged a fake play *and* a playback
  failure against *Sunset Groove*. Only record a play when state is PLAYING, and only count a failure
  against a track that actually tried to play (elapsed ≈ 0 on a fresh URI).
- [ ] **Staff pressing Stop in the Sonos app gets overridden.** STOPPED is treated as a stall, so the DJ restarts
  music within ~4 s. Distinguish "stopped by a person" (Sonos app, mid-track) from "stream failed"
  (STOPPED at 0:00), and stand down on the former.
- [ ] **Mock runs write into live state.** `--mock` shares `data/state.json`; 45-second mock "plays" are now in the
  live play counts/history (59 plays logged across 57 tracks, some from mock). Give mock its own `data/mock/`.
  Optionally reset `data/state.json` votes once.
- [ ] **Trims never expire.** Knob trims are saved forever. With the trims from today's session
  (energy −0.40, volume −7) the DJ would be at 0.18 energy by 19:00 and the floor (0.05) by 22:00.
  Options: trims decay back to 0 over ~1 h, reset at each daypart change, or show an obvious "trims active" badge.
- [ ] **Queued songs ignore the clock.** Upcoming picks only refresh on manual changes, not when the real clock
  crosses into a new daypart (afternoon → early evening). Add a re-pick on daypart change (~3 lines in `tick`).
- [ ] Analysis failures are permanent (`{"failed": true}` in `track_meta.json`; 1 of 909 so far, a network blip).
  Retry after a day.
- [ ] `base_url` (this laptop's LAN IP) is fixed at startup. If DHCP hands out a new IP, every queued cached
  track 404s. Re-check the IP in the health loop and re-queue if it changes.
- [ ] Downvoting one "up next" track re-picks *both* upcoming tracks; replace just that one.

## 2. Tuning (from how it was used)

- [ ] The session ran with energy trim at its **minimum (−0.40)** and volume −7 in the afternoon, so the default
  curve is too energetic/loud for a study cafe. Lower `daypart_curve` (afternoon energy 0.70 → ~0.45,
  volume 42 → ~35) and widen the energy trim range, then re-test.
- [ ] Check the energy scale by ear: listen to 5 tracks near 0.1 and 5 near 0.9 per genre. The tempo estimator
  may double/halve some lo-fi tempos; votes will show which way.
- [ ] Per-room offsets (`room_volume_offsets`) once party mode is used: entrance louder, study floors quieter.

## 3. Before staff use it

- [ ] **Access control.** Anyone on the Wi-Fi can open the controller. Check whether customers share this network
  (the speakers are on 192.168.0.x/1.x). Minimum: a staff PIN; better: sign-in tied to the O3 app.
- [ ] **Staff view vs admin view.** Staff: now playing, up next, 👍 / 👎 / skip, "quieter please". Admin: knobs, rooms, genres.
- [ ] **Run as a service.** launchd job with auto-restart, `caffeinate` so the laptop doesn't sleep (the speakers stream
  cached files from it), log to file. Longer term, a dedicated box (Mac mini / Raspberry Pi) per venue.
- [ ] **Resume after a restart.** `running` isn't persisted, so a crash means silence two songs later. Persist it and resume.
- [ ] Real web server (waitress/gunicorn) instead of Flask's dev server.
- [ ] Rate-limit skips/downvotes, so one person can't skip the whole playlist.

## 4. Roadmap: live data instead of manual toggles

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
- [ ] Duration + artwork metadata so the Sonos app and screen show full info.
- [ ] More sources: licensed business music services (Soundtrack Your Brand, etc.), Free Music Archive (check licences),
  a shared Google Drive folder synced into `library/`.
- [ ] Analytics: what played when, skips/downvotes by daypart, hours of music per room; later, correlate with dwell time/sessions.

## 6. Housekeeping

- [ ] Tests for the queue edge cases above (edit during transition, stop vs stall, reset recovery) using the MockPlayer
  (add transition/crossfade and "Sonos resets to item 1" behaviour to the mock).
- [ ] Cache: only warm the selected genres heavily; the others need fewer tracks.
- [ ] Confirm Chillify's licence allows commercial/venue playback before the pilot.
- [ ] `.claude/launch.json` preview config can't reach the speakers (macOS Local Network permission for the app);
  run live from a terminal.
