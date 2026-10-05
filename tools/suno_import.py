"""Add songs made in the Suno web app to the DJ's library, and move the library to another device.

    # 1. on the laptop where you downloaded them (titles like "O3 Dark Academia 66", see below)
    python3 tools/suno_import.py add ~/Downloads/O3*.mp3
    python3 tools/suno_import.py add --style dark_academia ~/Downloads/rain.mp3   # if the title doesn't say

    # 2. pack every Suno song (with its metadata) into one file, and copy it to the DJ device
    python3 tools/suno_import.py export o3-music.zip

    # 3. on the DJ device, from the o3-dj folder
    python3 tools/suno_import.py unpack o3-music.zip
    ./dj.sh restart          # or wait ~10 min for the library rescan

Titles: name each song "O3 <style label> <bpm>", e.g. "O3 Bossa Nova 128". `add` finds the style from the label
(suno/styles.json) and the BPM from the number. Without them use --style / --bpm (the BPM defaults to the middle of
the style's range).

`add` copies each file to library/<style>/suno-<hash>.<ext> with a sidecar .json (the DJ reads BPM, vocals and the
licence from it) and levels its loudness. Files already in the library are skipped, so running it twice is safe.
Only songs made on a paid Suno plan (Pro/Premier) may be played in the cafe: --plan defaults to pro.
"""

import argparse
import hashlib
import json
import re
import shutil
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
        gain = level(dst, cfg) if normalise else 0.0
        instrumental = st.get("vocals") is False
        side = {"source": "suno", "title": f.stem, "genre": key, "style": key, "bpm": tempo,
                "vocals": not instrumental, "prompt": tags_for(st, tempo), "plan": plan,
                "commercial": plan.lower() in PAID_PLANS, "gain_db": gain, "hash": h, "imported_from": f.name,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
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


def unpack(zip_path, library, log=print):
    """Unpack a zip from export() into the library, skipping songs that are already there."""
    added = skipped = 0
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2 or not rel.name.startswith("suno-"):
                continue  # only library/<style>/suno-* files
            dst = library / rel
            if dst.exists():
                skipped += dst.suffix != ".json"
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            added += dst.suffix != ".json"
    log(f"added {added} songs to {library} ({skipped} already there)")
    return added


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add downloaded songs to the library")
    a.add_argument("files", nargs="+")
    a.add_argument("--style", help="style key from suno/styles.json (default: from each file name)")
    a.add_argument("--bpm", type=int, help="tempo (default: from each file name)")
    a.add_argument("--plan", default="pro", help="Suno plan they were made on: pro | premier | free")
    a.add_argument("--no-level", action="store_true", help="don't level loudness")
    e = sub.add_parser("export", help="pack all Suno songs into one zip")
    e.add_argument("zip")
    u = sub.add_parser("unpack", help="unpack a zip from export into this device's library")
    u.add_argument("zip")
    args = ap.parse_args()

    styles = json.loads(STYLES.read_text())
    library = config.LIBRARY
    if args.cmd == "add":
        cfg = config.load()
        saved = add(args.files, styles, library, cfg, args.style, args.bpm, args.plan, not args.no_level)
        print(f"\n{len(saved)} added. Next: python3 tools/suno_import.py export o3-music.zip")
    elif args.cmd == "export":
        export(Path(args.zip), library)
    else:
        unpack(Path(args.zip), library)


if __name__ == "__main__":
    main()
