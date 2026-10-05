"""Generate tracks with Suno from the prompts in suno/styles.json. An offline tool: run it by hand;
the DJ never calls it. The finished tracks land in library/<genre>/ and the DJ picks them up on its next
library scan.

    python3 tools/suno_generate.py --list
    python3 tools/suno_generate.py --pilot                        # Phase 0: every starred reference
    python3 tools/suno_generate.py --pilot dark_academia --count 3
    python3 tools/suno_generate.py --style dark_academia --count 6 --max-credits 60

Not in use: Suno has no official API we can use, and the unofficial ones depend on a CAPTCHA-solving service.
Songs are made in the Suno web app and added with tools/suno_import.py instead. This tool stays as the frame for an
official API: implement `Backend` for it and pass it to `run()`.

Licence: songs are only licensed for commercial use (the cafe) if they were generated on a paid Suno plan
(Pro/Premier). Pass --plan (or SUNO_PLAN). Tracks made on another plan are recorded as commercial: false
and the DJ's library skips them.

Each track gets a sidecar <name>.json next to the audio: {source, title, genre, bpm, vocals, tags,
prompt, suno_id, created, plan, commercial}. Unfinished jobs are remembered in data/suno_jobs.json,
so a rerun collects them instead of paying for them again.
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from o3dj import config  # noqa: E402

STYLES = ROOT / "suno" / "styles.json"
PAID_PLANS = {"pro", "premier"}


class Backend:
    """What the generator needs from Suno. Swap in another class for an official or reseller API."""

    def generate(self, tags, title, instrumental, description=None, exclude=""):
        """Start one generation (exclude = Suno's "Exclude styles"). Returns the new clip ids (two per call)."""
        raise NotImplementedError

    def clips(self, ids):
        """[{id, status, audio_url, title, duration}], status 'complete' when downloadable, 'error' if failed."""
        raise NotImplementedError

    def credits(self):
        """Credits left, or None if unknown."""
        return None


class Jobs:
    """data/suno_jobs.json: clips asked for but not downloaded yet, and every clip already saved."""

    def __init__(self, path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {"pending": {}, "done": []}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))


def tags_for(style, bpm):
    parts = [style["prompt"], *style.get("modifiers", []), f"{bpm} bpm"]
    if style.get("vocals") is False:
        parts.append("instrumental")
    return ", ".join(parts)


def level(path, cfg):
    """Level a downloaded file to normalise.target_lufs (Suno output is usually loud)."""
    from o3dj.analysis import NORM_FORMATS, apply_gain, measure_lufs
    c = cfg.get("normalise", {})
    if not c.get("enabled", True) or path.suffix not in NORM_FORMATS:
        return 0.0
    lufs = measure_lufs(path)
    gain = max(-c.get("max_cut_db", 15), min(c.get("max_boost_db", 8), c.get("target_lufs", -16) - lufs))
    if abs(gain) < c.get("skip_within_db", 1.0) or lufs <= -50:
        return 0.0
    tmp = path.with_name(path.name + ".part")
    try:
        apply_gain(path, tmp, gain, c.get("bitrate", "192k"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return round(gain, 1)


def download(url, dst):
    tmp = dst.with_name(dst.name + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    os.replace(tmp, dst)


def collect(backend, jobs, cfg, fetch=download, normalise=True, poll=10, timeout=600, sleep=time.sleep, log=print):
    """Wait for pending clips, then download each finished one with its sidecar. Returns saved paths."""
    saved = []
    deadline = time.time() + timeout
    while jobs.data["pending"]:
        ids = list(jobs.data["pending"])
        for clip in backend.clips(ids):
            job = jobs.data["pending"].get(clip["id"])
            if not job:
                continue
            if clip.get("status") == "error":
                log(f"  {clip['id'][:8]} failed on Suno's side")
                jobs.data["pending"].pop(clip["id"])
            elif clip.get("status") == "complete" and clip.get("audio_url"):
                out = Path(job["out_dir"])
                out.mkdir(parents=True, exist_ok=True)
                dst = out / f"suno-{clip['id'][:8]}.mp3"
                fetch(clip["audio_url"], dst)
                gain = level(dst, cfg) if normalise else 0.0
                side = {**job["meta"], "title": clip.get("title") or job["meta"]["title"], "suno_id": clip["id"],
                        "duration": clip.get("duration"), "gain_db": gain,
                        "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                dst.with_suffix(".json").write_text(json.dumps(side, indent=1, ensure_ascii=False))
                jobs.data["pending"].pop(clip["id"])
                jobs.data["done"].append(clip["id"])
                saved.append(dst)
                log(f"  saved {dst.relative_to(ROOT) if dst.is_relative_to(ROOT) else dst}")
            jobs.save()
        if jobs.data["pending"]:
            if time.time() > deadline:
                log(f"  {len(jobs.data['pending'])} clip(s) still rendering; rerun later to collect them")
                break
            sleep(poll)
    return saved


def run(key, count, backend, cfg, styles, out_dir, jobs, plan, max_credits=None, pilot=False,
        rng=random, fetch=download, normalise=True, sleep=time.sleep, log=print, timeout=600):
    """Generate `count` new tracks for one style (or pilot entry). Returns (saved paths, credits spent)."""
    style = styles["pilot" if pilot else "styles"][key]
    genre = style.get("genre", key)
    per_call = styles.get("credits_per_call", 10)
    commercial = (plan or "").lower() in PAID_PLANS
    if not commercial:
        log(f"warning: plan {plan!r} is not Pro/Premier; tracks are saved as commercial: false and won't be played")

    saved = collect(backend, jobs, cfg, fetch, normalise, timeout=timeout, sleep=sleep, log=log)  # finish anything left over
    spent, made = 0, 0
    while made < count:
        if max_credits is not None and spent + per_call > max_credits:
            log(f"stopping: next call would pass --max-credits {max_credits}")
            break
        left = backend.credits()
        if left is not None and left < per_call:
            log(f"stopping: only {left} Suno credits left")
            break
        bpm = rng.randint(*style["bpm"])
        if style.get("prompts"):  # several approved prompts: rotate between them for variety
            style = {**style, "prompt": rng.choice(style["prompts"])}
        tags = tags_for(style, bpm)
        instrumental = style.get("vocals") is False
        title = f"{style.get('label', genre)} {bpm}"
        # a style's own exclude_only list replaces the shared one (e.g. fantasy allows flute); exclude adds to it
        exclude = ", ".join(style.get("exclude_only") or (styles.get("exclude", []) + style.get("exclude", [])))
        ids = backend.generate(tags, title, instrumental, style.get("description"), exclude)
        spent += per_call
        meta = {"source": "suno", "title": title, "genre": genre, "style": key, "bpm": bpm,
                "vocals": not instrumental, "tags": [t.strip() for t in tags.split(",")], "prompt": tags, "exclude": exclude,
                "plan": plan, "commercial": commercial, "pilot": pilot,
                "reference": style.get("reference") or (style.get("references") or [None])[0]}
        for cid in ids:
            if cid in jobs.data["done"]:
                continue
            jobs.data["pending"][cid] = {"out_dir": str(out_dir), "meta": meta}
        jobs.save()
        made += len(ids)
        log(f"{key}: asked Suno for {len(ids)} track(s) at {bpm} bpm ({spent} credits so far)")
        saved += collect(backend, jobs, cfg, fetch, normalise, timeout=timeout, sleep=sleep, log=log)
    return saved, spent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--style", help="genre key from suno/styles.json")
    ap.add_argument("--pilot", nargs="?", const="all", help="Phase 0: a pilot entry id, or all of them")
    ap.add_argument("--count", type=int, default=3, help="tracks to add per style (Suno makes 2 per call)")
    ap.add_argument("--max-credits", type=int, default=None, help="stop before spending more than this")
    ap.add_argument("--plan", default=os.environ.get("SUNO_PLAN"), help="your Suno plan: pro | premier | free")
    ap.add_argument("--collect", action="store_true", help="only download clips from earlier runs")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    styles = json.loads(STYLES.read_text())
    if args.list:
        for k, s in styles["styles"].items():
            print(f"{k:20} {s['bpm'][0]}-{s['bpm'][1]} bpm  {s['prompt'][:70]}")
        print("\npilot:")
        for k, s in styles["pilot"].items():
            if not k.startswith("_"):
                print(f"{k:20} -> {s['genre']:18} {s['reference']}")
        return

    cfg = config.load()
    backend = None  # no official Suno API yet; see the module docstring
    if backend is None:
        ap.error("no Suno API backend configured: make songs in the Suno web app and use tools/suno_import.py")
    jobs = Jobs(config.DATA / "suno_jobs.json")
    if args.collect:
        collect(backend, jobs, cfg)
        return
    if not args.plan:
        ap.error("say which Suno plan you're on with --plan (or SUNO_PLAN); only Pro/Premier songs may be played in the cafe")

    if args.pilot:
        keys = [k for k in styles["pilot"] if not k.startswith("_")] if args.pilot == "all" else [args.pilot]
        budget = args.max_credits
        for k in keys:
            _, spent = run(k, args.count, backend, cfg, styles, config.DATA / "suno_pilot" / k, jobs, args.plan,
                           budget, pilot=True)
            if budget is not None:
                budget -= spent
        print("\nListen in data/suno_pilot/ next to each reference and score 1-5 (see suno/styles.json 'pilot').")
    elif args.style:
        if args.style not in styles["styles"]:
            ap.error(f"unknown style {args.style}; try --list")
        run(args.style, args.count, backend, cfg, styles, config.LIBRARY / args.style, jobs, args.plan, args.max_credits)
    else:
        ap.error("pick --style, --pilot, --collect or --list")


if __name__ == "__main__":
    main()
