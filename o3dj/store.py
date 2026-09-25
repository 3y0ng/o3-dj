"""Tiny JSON-file persistence with atomic writes."""

import json
import os
import threading


class JsonFile:
    def __init__(self, path, default):
        self.path = path
        self.lock = threading.RLock()
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = default

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
            os.replace(tmp, self.path)
