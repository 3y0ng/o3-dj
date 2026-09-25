"""HTTP API + static UI + media serving (the speakers stream cached files from here)."""

import logging
import re

from flask import Flask, jsonify, request, send_from_directory

from .config import CACHE_DIR, LIBRARY, WEB

log = logging.getLogger(__name__)


def create_app(dj):
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False  # keep genre order from config.json

    def ok():
        return jsonify(dj.snapshot())

    @app.errorhandler(Exception)
    def on_error(e):
        log.exception("request failed")
        if request.path.startswith("/api/"):
            dj.event("error: " + str(e).split(" from ")[0][:90])
        code = getattr(e, "code", 500)
        return jsonify({"error": str(e)}), code if isinstance(code, int) else 500

    @app.get("/")
    def index():
        return send_from_directory(WEB, "index.html")

    @app.get("/ui/<path:name>")
    def ui(name):
        return send_from_directory(WEB, name)

    @app.get("/api/state")
    def state():
        return ok()

    @app.post("/api/control")
    def control():
        body = request.get_json(force=True)
        action = body.get("action")
        if action == "play":
            dj.play()
        elif action == "pause":
            dj.pause()
        elif action == "skip":
            dj.skip()
        elif action in ("up", "down"):
            dj.vote(action, body.get("id"))
        elif action == "restart":
            dj.start()
        else:
            return jsonify({"error": f"unknown action {action!r}"}), 400
        dj.refresh_status()
        return ok()

    @app.post("/api/inputs")
    def inputs():
        dj.set_inputs(request.get_json(force=True))
        return ok()

    @app.post("/api/speakers")
    def speakers():
        body = request.get_json(force=True)
        dj.speaker_action(body.get("action"), body.get("ip"))
        return ok()

    @app.post("/api/tracks")
    def add_track():
        body = request.get_json(force=True)
        url, genre = (body.get("url") or "").strip(), body.get("genre")
        if not re.match(r"^https?://", url):
            return jsonify({"error": "URL must start with http:// or https://"}), 400
        if genre not in dj.library.genres():
            return jsonify({"error": "unknown genre"}), 400
        title = (body.get("title") or "").strip() or url.rsplit("/", 1)[-1]
        dj.library.urls.add(url, title, genre, body.get("artist", ""))
        dj.library.refresh()
        dj.event(f"added {title}")
        return ok()

    @app.get("/media/cache/<name>")
    def media_cache(name):
        return send_from_directory(CACHE_DIR, name, conditional=True)

    @app.get("/media/library/<path:rel>")
    def media_library(rel):
        return send_from_directory(LIBRARY, rel, conditional=True)

    return app
