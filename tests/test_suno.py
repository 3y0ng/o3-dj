"""Suno generator (tools/suno_generate.py) against a fake backend, and sidecar reading in the library."""

import json
import random
import sys

from o3dj.config import ROOT
from o3dj.library import FolderSource

sys.path.insert(0, str(ROOT / "tools"))
import suno_generate as sg  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text())
STYLES = json.loads((ROOT / "suno" / "styles.json").read_text())


class FakeSuno(sg.Backend):
    def __init__(self, credits=100, renders_after=1):
        self.left, self.calls, self.n = credits, [], 0
        self.polls = {}
        self.renders_after = renders_after

    def generate(self, tags, title, instrumental, description=None, exclude=""):
        self.calls.append({"tags": tags, "instrumental": instrumental, "description": description, "exclude": exclude})
        self.left -= 10
        ids = []
        for _ in range(2):
            self.n += 1
            ids.append(f"clip{self.n:04d}-abcdef")
        return ids

    def clips(self, ids):
        out = []
        for i in ids:
            self.polls[i] = self.polls.get(i, 0) + 1
            done = self.polls[i] > self.renders_after
            out.append({"id": i, "status": "complete" if done else "streaming",
                        "audio_url": f"https://cdn.test/{i}.mp3", "title": f"Song {i[:8]}", "duration": 150})
        return out

    def credits(self):
        return self.left


def fake_fetch(url, dst):
    dst.write_bytes(b"ID3fake " + url.encode())


def go(tmp_path, backend, key="dark_academia", count=4, plan="pro", **kw):
    jobs = sg.Jobs(tmp_path / "jobs.json")
    out = tmp_path / "library" / key
    saved, spent = sg.run(key, count, backend, CFG, STYLES, out, jobs, plan, fetch=fake_fetch, normalise=False,
                          sleep=lambda _: None, log=lambda *_: None, rng=random.Random(1), **kw)
    return saved, spent, jobs, out


def test_generates_downloads_and_writes_sidecars(tmp_path):
    saved, spent, jobs, out = go(tmp_path, FakeSuno())
    assert len(saved) == 4 and spent == 20 and not jobs.data["pending"]
    side = json.loads(saved[0].with_suffix(".json").read_text())
    lo, hi = STYLES["styles"]["dark_academia"]["bpm"]
    assert side["source"] == "suno" and side["genre"] == "dark_academia" and lo <= side["bpm"] <= hi
    assert side["vocals"] is False and side["commercial"] is True and side["suno_id"].startswith("clip")
    assert "instrumental" in side["tags"]


def test_credit_cap_and_suno_balance_stop_generation(tmp_path):
    b = FakeSuno()
    saved, spent, *_ = go(tmp_path, b, count=10, max_credits=25)
    assert spent == 20 and len(b.calls) == 2
    b2 = FakeSuno(credits=15)
    _, spent2, *_ = go(tmp_path / "b", b2, count=10)
    assert spent2 == 10


def test_rerun_collects_pending_instead_of_paying_again(tmp_path):
    b = FakeSuno(renders_after=10 ** 9)  # still rendering when the first run gives up
    out = tmp_path / "library" / "dark_academia"
    saved, _ = sg.run("dark_academia", 2, b, CFG, STYLES, out, sg.Jobs(tmp_path / "jobs.json"), "pro",
                      fetch=fake_fetch, normalise=False, sleep=lambda _: None, log=lambda *_: None, timeout=0)
    assert not saved and len(sg.Jobs(tmp_path / "jobs.json").data["pending"]) == 2
    b.renders_after = 0
    again = sg.collect(b, sg.Jobs(tmp_path / "jobs.json"), CFG, fetch=fake_fetch, normalise=False,
                       sleep=lambda _: None, log=lambda *_: None)
    assert len(b.calls) == 1 and len(again) == 2 and len(list(out.glob("*.mp3"))) == 2
    assert not sg.Jobs(tmp_path / "jobs.json").data["pending"]


def test_exclude_styles_are_sent(tmp_path):
    b = FakeSuno()
    go(tmp_path, b, key="bossa_house", count=2)
    assert "flute" in b.calls[0]["exclude"] and "four on the floor" in b.calls[0]["exclude"]


def test_fantasy_rotates_prompts_and_uses_its_own_exclude_list(tmp_path):
    b = FakeSuno()
    go(tmp_path, b, key="whimsy_fantasy", count=12)
    prompts = STYLES["styles"]["whimsy_fantasy"]["prompts"]
    used = {next(p for p in prompts if c["tags"].startswith(p)) for c in b.calls}
    assert used == set(prompts)
    assert "flute" not in b.calls[0]["exclude"] and "bright" in b.calls[0]["exclude"]


def test_vocal_style_asks_suno_to_write_lyrics(tmp_path, monkeypatch):
    b = FakeSuno()
    go(tmp_path, b, key="lofi_indie", count=2)
    assert b.calls[0]["instrumental"] is True  # staff don't want voices: every current style is instrumental
    sung = {**STYLES["styles"]["lofi_indie"], "vocals": "light", "description": "a soft song about a cafe"}
    monkeypatch.setitem(STYLES["styles"], "sung", sung)
    b2 = FakeSuno()
    go(tmp_path / "sung", b2, key="sung", count=2)
    assert b2.calls[0]["instrumental"] is False and b2.calls[0]["description"]


def test_library_reads_sidecars_and_skips_unlicensed(tmp_path):
    _, _, _, out = go(tmp_path, FakeSuno(), count=2)
    _, _, _, free = go(tmp_path / "free", FakeSuno(), key="dark_academia", count=2, plan="free")
    for f in free.glob("*"):
        f.rename(out / ("free-" + f.name))
    (tmp_path / "library" / "chill").mkdir()
    (tmp_path / "library" / "chill" / "mine.mp3").write_bytes(b"x")

    tracks = {t.id: t for t in FolderSource(tmp_path / "library").load()}
    suno = [t for t in tracks.values() if t.source == "suno"]
    assert len(suno) == 2  # the free-plan pair is skipped
    t = suno[0]
    assert t.genre == "dark_academia" and t.artist == "Suno" and t.bpm and t.vocals is False
    assert t.path.startswith("dark_academia/") and t.title.startswith("Song")
    local = tracks["folder:chill/mine.mp3"]
    assert local.source == "folder" and local.bpm is None and local.vocals is None
