"""Config loading: config.json, overridden by an optional (gitignored) config.local.json."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE_DIR = DATA / "cache"
LIBRARY = ROOT / "library"
WEB = ROOT / "web"


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load():
    cfg = json.loads((ROOT / "config.json").read_text())
    local = ROOT / "config.local.json"
    if local.exists():
        _merge(cfg, json.loads(local.read_text()))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY.mkdir(exist_ok=True)
    return cfg
