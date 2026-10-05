"""tools/suno_import.py: adding downloaded Suno songs, and moving them between devices."""

import json
import sys
import zipfile

from o3dj.config import ROOT
from o3dj.library import FolderSource

sys.path.insert(0, str(ROOT / "tools"))
import suno_import as si  # noqa: E402

STYLES = json.loads((ROOT / "suno" / "styles.json").read_text())
CFG = json.loads((ROOT / "config.json").read_text())


def quiet(*_):
    pass


def song(d, name, data=b"ID3 audio"):
    p = d / name
    p.write_bytes(data + name.encode())
    return p


def test_guesses_style_and_bpm_from_the_title():
    assert si.guess_style("O3 Dark Academia 66", STYLES) == "dark_academia"
    assert si.guess_style("O3 Bossa Nova 128 (1)", STYLES) == "bossa_house"
    assert si.guess_style("o3-evening-lofi-74", STYLES) == "indie_lofi_fantasy"
    assert si.guess_style("random song", STYLES) is None
    assert si.guess_bpm("O3 Bossa Nova 128 (1)") == 128
    assert si.guess_bpm("O3 Dark Academia") is None


def test_add_writes_sidecars_skips_duplicates_and_the_dj_reads_them(tmp_path):
    dl, lib = tmp_path / "Downloads", tmp_path / "library"
    dl.mkdir()
    files = [song(dl, "O3 Dark Academia 66.mp3"), song(dl, "O3 Bossa Nova 132 (1).mp3"), song(dl, "notes.txt")]
    saved = si.add(files, STYLES, lib, CFG, normalise=False, log=quiet)
    assert len(saved) == 2
    side = json.loads(saved[1].with_suffix(".json").read_text())
    assert side["genre"] == "bossa_house" and side["bpm"] == 132 and side["commercial"] is True and side["vocals"] is False
    assert si.add(files, STYLES, lib, CFG, normalise=False, log=quiet) == []  # second run adds nothing

    tracks = FolderSource(lib).load()
    assert {(t.genre, t.bpm, t.source) for t in tracks} == {("dark_academia", 66, "suno"), ("bossa_house", 132, "suno")}


def test_free_plan_songs_are_kept_out_of_rotation(tmp_path):
    dl, lib = tmp_path / "Downloads", tmp_path / "library"
    dl.mkdir()
    si.add([song(dl, "x.mp3")], STYLES, lib, CFG, style="ambient_dream", plan="free", normalise=False, log=quiet)
    assert FolderSource(lib).load() == []


def test_export_and_unpack_move_the_library(tmp_path):
    dl, lib, other = tmp_path / "Downloads", tmp_path / "library", tmp_path / "dj-device" / "library"
    dl.mkdir()
    si.add([song(dl, "O3 Dark Academia 66.mp3"), song(dl, "O3 Evening Lofi 74.mp3")], STYLES, lib, CFG,
           normalise=False, log=quiet)
    (lib / "chill").mkdir()
    (lib / "chill" / "mine.mp3").write_bytes(b"not suno")  # only Suno songs travel
    z = tmp_path / "o3-music.zip"
    assert si.export(z, lib, log=quiet) == 2

    other.mkdir(parents=True)
    assert si.unpack(z, other, log=quiet) == 2
    assert si.unpack(z, other, log=quiet) == 0  # already there
    assert sorted(t.genre for t in FolderSource(other).load()) == ["dark_academia", "indie_lofi_fantasy"]


def test_unpack_ignores_paths_outside_the_library(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../evil/suno-x.mp3", b"x")
        f.writestr("/abs/suno-y.mp3", b"y")
        f.writestr("dark_academia/notes.txt", b"z")
    lib = tmp_path / "library"
    lib.mkdir()
    assert si.unpack(z, lib, log=quiet) == 0
    assert not any(lib.rglob("*")) and not (tmp_path / "evil").exists()
