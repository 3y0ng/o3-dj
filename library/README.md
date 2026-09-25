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
