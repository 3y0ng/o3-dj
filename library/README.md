# Adding music

Three ways, all picked up automatically (folders are rescanned every ~10 min, or restart):

1. **Drop files** into `library/<genre>/`, e.g. `library/chill/foo.mp3`.
   Use an existing genre key (`chill`, `jazzy_cafe`, `asian`) or a new folder
   name to create a new genre key on the controller. mp3 / m4a / aac / flac / wav / ogg.
   Local files play even with no internet.
2. **Stream URLs**: use "add a track" on the controller, or copy
   `custom_tracks.example.json` to `custom_tracks.json` and edit it.
   Any direct audio URL the speakers can fetch works.
3. **A whole new service**: write a source class in `o3dj/library.py`
   (anything with `name` and `load() -> list[Track]`) and add it to `Library.sources`.

Only use music you're licensed to play in a commercial venue.

## Suno

Songs are made by hand in the Suno web app (O3 Aus account, Pro plan) from the approved prompts and settings in
`suno/styles.json`, downloaded, then added with `tools/suno_import.py`. The DJ reads each song's sidecar: the known BPM
is used for tempo matching, vocal tracks are avoided when the room is packed, and Suno songs are peppered in: the
slot's new genres start at 40% of picks and grow to 100% as they fill to 30 songs each (`new_music` in config.json).
Below `min_tracks` (8) a new genre hands its share to its Chillify fallback genre.

1. In Suno (Advanced): paste the style's prompt plus a BPM from its range, lyrics empty (instrumental), the style's
   Exclude styles, and its settings (`suno_settings`, default Style Influence 60 / Variety High; Weirdness varies 30-65).
   Title each song **"O3 <style label> <bpm>"**, e.g. `O3 Dark Academia 66`.
2. Download the keepers as MP3. Pro includes 20 downloads a month; pick the best.
3. `python3 tools/suno_import.py add ~/Downloads/O3*.mp3`: copies them into `library/<style>/` with a sidecar
   (style and BPM come from the title) and levels loudness. Running it twice adds nothing twice.
4. Move them to the DJ device: `python3 tools/suno_import.py export` writes `~/Downloads/o3-music-<date>.zip`. Copy it into
   the DJ device's Downloads folder (AirDrop, USB, Drive), then in the o3-dj folder there run `./dj.sh update`: it pulls the
   latest code, unpacks the newest `o3-music*.zip` and restarts the DJ if it's running. Only Suno songs travel, and songs
   already on the device are skipped, so the zip can always hold the whole Suno library.
5. Two takes of one prompt are named "... A" and "... B" (e.g. `O3 Bossa Nova 120 A`). Vary Weirdness between generations
   (`weirdness` in suno/styles.json: 30-65 for every style).
6. Check every generation before downloading it. Suno shows its own description under each song's title: it must name
   the style (bossa nova, jazz-hop, lofi, deep house, celtic/harp/cello, piano, ambient). A "post-genre", "orchestral"
   or "genre-blending" description means Suno got no style prompt; don't download it, remake it. On 10 Oct 2026 a script
   typed the prompt into Suno's "Ask anything" box instead of the Styles box and 156 songs came out as random music.
   When filling the form by script: use the textarea under the "Styles" heading, read the value back from that same box,
   stop the run at the first off-style result, and count new songs with Suno's search per title (scrolling the list skips rows).

- **Licence**: Suno songs may only be used commercially if they were made on a paid plan (Pro/Premier) at the time.
  `add --plan` defaults to pro; songs added with another plan get `"commercial": false` and the DJ skips them.
- **No automation**: the unofficial Suno API needs a CAPTCHA-solving service to work, so we don't use it.
  `tools/suno_generate.py` stays for an official API if Suno offers one (swap its `Backend`).
- Prompts describe the sound; Suno rejects artist names.
