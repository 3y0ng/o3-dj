"""Add songs made in the Suno web app to the DJ's library, and move the library to another device.

    # 1. on the laptop where you downloaded them (titles like "O3 Dark Academia 66", see below)
    python3 tools/suno_import.py add ~/Downloads/O3*.mp3
    python3 tools/suno_import.py add --style dark_academia ~/Downloads/rain.mp3   # if the title doesn't say

    # 2. pack every Suno song (with its metadata) into one file: ~/Downloads/o3-music-<date>.zip
    python3 tools/suno_import.py export
    #    AirDrop / USB / Drive it into the DJ device's Downloads folder

    # 3. on the DJ device, from the o3-dj folder: pull the latest code, unpack the newest zip, restart
    ./dj.sh update

Titles: name each song "O3 <style label> <bpm>", e.g. "O3 Bossa Nova 128". `add` finds the style from the label
(suno/styles.json) and the BPM from the number. Without them use --style / --bpm (the BPM defaults to the middle of
the style's range).

`add` copies each file to library/<style>/suno-<hash>.<ext> with a sidecar .json (the DJ reads BPM, vocals and the
licence from it) and levels its loudness. Suno rewrites the Styles text before generating; the sidecar keeps that
rewrite as `suno_style` (from the MP3's genre tag) and `prompt` is whichever of the style's prompts it came from. Files already in the library are skipped, so running it twice is safe.
Only songs made on a paid Suno plan (Pro/Premier) may be played in the cafe: --plan defaults to pro.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from o3dj import config  # noqa: E402
from o3dj.library import AUDIO_EXTS  # noqa: E402

STYLES = ROOT / "suno" / "styles.json"
PAID_PLANS = {"pro", "premier"}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def guess_style(name, styles):
    """The style whose label (or key) appears in the file name; the longest match wins."""
    n = f" {_norm(name)} "
    hits = []
    for key, s in styles["styles"].items():
        for label in {s.get("label", key), key.replace("_", " ")}:
            if f" {_norm(label)} " in n:
                hits.append((len(label), key))
    return max(hits)[1] if hits else None


def guess_bpm(name, lo=40, hi=200):
    """The last number in the name that looks like a tempo ("O3 Bossa Nova 128 (1)" -> 128)."""
    nums = [int(x) for x in re.findall(r"(?<![\d.])(\d{2,3})(?![\d.])", name)]
    nums = [x for x in nums if lo <= x <= hi]
    return nums[-1] if nums else None


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def known_hashes(library):
    out = set()
    for side in library.glob("*/suno-*.json"):
        try:
            h = json.loads(side.read_text()).get("hash")
        except (OSError, json.JSONDecodeError):
            continue
        if h:
            out.add(h)
    return out


def suno_style(path):
    """The style text Suno actually generated from. Suno rewrites the Styles box into its own longer description
    before generating, and puts that in the downloaded MP3's genre tag. "" if the file has none."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=genre", "-of",
                              "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def sent_prompt(st, suno):
    """Which of the style's prompts this song was made from: the one sharing the most words with Suno's rewrite."""
    prompts = (st.get("prompts") or [st["prompt"]]) + st.get("minor_prompts", [])
    words = set(re.findall(r"[a-z]{4,}", suno.lower()))
    return max(prompts, key=lambda p: len(words & set(re.findall(r"[a-z]{4,}", p.lower())))) if words else st["prompt"]


def backfill(library, log=print):
    """Add suno_style (from each file's genre tag) to sidecars that don't have it. Returns how many changed."""
    n = 0
    for side in sorted(library.glob("*/suno-*.json")):
        data = json.loads(side.read_text())
        audio = next((f for f in side.parent.glob(side.stem + ".*") if f.suffix.lower() in AUDIO_EXTS), None)
        if "suno_style" in data or not audio:
            continue
        style = suno_style(audio)
        if style:
            data["suno_style"] = style
            side.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            n += 1
    log(f"{n} sidecars got suno_style")
    return n


def add(files, styles, library, cfg, style=None, bpm=None, plan="pro", normalise=True, log=print):
    """Copy downloaded songs into the library with sidecars. Returns the new paths."""
    from suno_generate import level, tags_for
    seen = known_hashes(library)
    saved = []
    for f in map(Path, files):
        if f.suffix.lower() not in AUDIO_EXTS:
            log(f"skip {f.name}: not an audio file")
            continue
        key = style or guess_style(f.stem, styles)
        if key not in styles["styles"]:
            log(f"skip {f.name}: can't tell the style; name it 'O3 <style> <bpm>' or pass --style")
            continue
        st = styles["styles"][key]
        tempo = bpm or guess_bpm(f.stem) or round(sum(st["bpm"]) / 2)
        h = file_hash(f)
        if h in seen:
            log(f"skip {f.name}: already in the library")
            continue
        out = library / key
        out.mkdir(parents=True, exist_ok=True)
        dst = out / f"suno-{h}{f.suffix.lower()}"
        shutil.copy2(f, dst)
        suno = suno_style(dst)
        gain = level(dst, cfg) if normalise else 0.0
        instrumental = st.get("vocals") is False
        side = {"source": "suno", "title": f.stem, "genre": key, "style": key, "bpm": tempo,
                "vocals": not instrumental, "prompt": tags_for({**st, "prompt": sent_prompt(st, suno)}, tempo), "plan": plan,
                "commercial": plan.lower() in PAID_PLANS, "gain_db": gain, "hash": h, "imported_from": f.name,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if suno:
            side["suno_style"] = suno
        dst.with_suffix(".json").write_text(json.dumps(side, indent=1, ensure_ascii=False))
        seen.add(h)
        saved.append(dst)
        log(f"added {f.name} -> {dst.relative_to(library.parent) if dst.is_relative_to(library.parent) else dst} ({key}, {tempo} bpm)")
    return saved


def export(zip_path, library, log=print):
    """Pack every Suno song and its sidecar into one zip."""
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # audio is already compressed
        for side in sorted(library.glob("*/suno-*.json")):
            audio = [p for p in side.parent.glob(side.stem + ".*") if p.suffix.lower() in AUDIO_EXTS]
            if not audio:
                continue
            for p in (side, audio[0]):
                z.write(p, p.relative_to(library).as_posix())
            n += 1
    log(f"packed {n} songs into {zip_path}")
    return n


# Songs taken out of the library live here (never deleted); unpacking an older zip must not bring them back.
REJECTED_DIRS = [Path.home() / "o3-dj-rejected", config.DATA / "removed-songs"]


def rejected_songs(dirs=None):
    """(file stems, sidecar hashes) of every song in the rejected folders, e.g. {"suno-0c14674b61bd"}."""
    stems, hashes = set(), set()
    for d in REJECTED_DIRS if dirs is None else dirs:
        for f in Path(d).rglob("suno-*") if Path(d).is_dir() else []:
            stems.add(f.stem)
            if f.suffix == ".json":
                try:
                    h = json.loads(f.read_text()).get("hash")
                except (OSError, ValueError):
                    h = None
                if h:
                    hashes.add(h)
    return stems, hashes


def unpack(zip_path, library, log=print, rejected=None):
    """Unpack a zip from export() into the library, skipping songs that are already there and songs that were
    rejected (moved to ~/o3-dj-rejected/ or data/removed-songs/), so an older zip can't bring them back."""
    stems, hashes = rejected_songs(rejected)
    added = skipped = refused = 0
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        for name in names:  # a rejected song can also be recognised by its sidecar's hash (same song, other name)
            rel = Path(name)
            if rel.suffix == ".json" and rel.name.startswith("suno-") and hashes:
                try:
                    if json.loads(z.read(name)).get("hash") in hashes:
                        stems.add(rel.stem)
                except ValueError:
                    pass
        for name in names:
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2 or not rel.name.startswith("suno-"):
                continue  # only library/<style>/suno-* files
            if rel.stem in stems:
                refused += rel.suffix != ".json"
                continue
            dst = library / rel
            if dst.exists():
                skipped += dst.suffix != ".json"
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            added += dst.suffix != ".json"
    log(f"added {added} songs to {library} ({skipped} already there, {refused} rejected and left out)")
    return added


def newest_zip(folder):
    zips = sorted(folder.glob("o3-music*.zip"), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add downloaded songs to the library")
    a.add_argument("files", nargs="+")
    a.add_argument("--style", help="style key from suno/styles.json (default: from each file name)")
    a.add_argument("--bpm", type=int, help="tempo (default: from each file name)")
    a.add_argument("--plan", default="pro", help="Suno plan they were made on: pro | premier | free")
    a.add_argument("--no-level", action="store_true", help="don't level loudness")
    e = sub.add_parser("export", help="pack all Suno songs into one zip (default ~/Downloads/o3-music-<date>.zip)")
    e.add_argument("zip", nargs="?")
    u = sub.add_parser("unpack", help="unpack a zip from export into this device's library (default: newest in ~/Downloads)")
    u.add_argument("zip", nargs="?")
    sub.add_parser("backfill", help="add suno_style (Suno's rewrite of the prompt, from the MP3 tag) to existing sidecars")
    args = ap.parse_args()

    styles = json.loads(STYLES.read_text())
    library = config.LIBRARY
    if args.cmd == "add":
        cfg = config.load()
        saved = add(args.files, styles, library, cfg, args.style, args.bpm, args.plan, not args.no_level)
        print(f"\n{len(saved)} added. Next: python3 tools/suno_import.py export o3-music.zip")
    elif args.cmd == "backfill":
        backfill(library)
    elif args.cmd == "export":
        z = Path(args.zip) if args.zip else Path.home() / "Downloads" / f"o3-music-{datetime.now():%Y-%m-%d}.zip"
        export(z, library)
        print(f"Copy {z.name} into the DJ device's Downloads folder (AirDrop, USB or Drive), then run ./dj.sh update there.")
    else:
        z = Path(args.zip) if args.zip else newest_zip(Path.home() / "Downloads")
        if not z:
            sys.exit("no o3-music*.zip in ~/Downloads; pass the zip's path")
        print(f"unpacking {z}")
        unpack(z, library)


if __name__ == "__main__":
    main()
