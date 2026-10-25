#!/usr/bin/env python3
"""
Preview the MediaOrg UI in a normal browser (for development / testing).

    python devserver.py            -> open http://127.0.0.1:8765/index.html?dev=1

Serves ui/ and forwards /api/<method> calls to the same Api class the desktop app uses.
Listens on 127.0.0.1 only. Folder dialogs aren't available here (the UI asks for a path instead).
"""
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import app

API = app.Api()
UI = Path(__file__).resolve().parent / "ui"
ALLOWED = {m for m in dir(app.Api) if not m.startswith("_")}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI), **kw)

    def log_message(self, *a):
        pass

    def do_POST(self):
        name = self.path.removeprefix("/api/")
        if name not in ALLOWED:
            self.send_error(404)
            return
        args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
        try:
            res = getattr(API, name)(*args)
        except Exception as e:  # surface errors to the UI like the app would
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        body = json.dumps(res, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"MediaOrg UI preview: http://127.0.0.1:{port}/index.html?dev=1")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
