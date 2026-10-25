"""
Setup downloads against a local server: resumable download, model pull progress/errors, and the "already installed" fast path.
Run:  python tests/test_components.py
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze  # noqa: E402
import components  # noqa: E402
import mediaorg as mo  # noqa: E402

BLOB = os.urandom(3_000_000)
PULLED = {"n": 0}
HAVE_MODEL = {"yes": False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            data = json.dumps({"models": [{"name": "qwen2.5vl:7b"}] if HAVE_MODEL["yes"] else []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        body = BLOB[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        PULLED["n"] += 1
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
        lines = ([{"status": "pulling manifest"}, {"status": "pulling", "total": 100, "completed": 40},
                  {"status": "pulling", "total": 100, "completed": 100}, {"status": "success"}]
                 if req["name"] == "qwen2.5vl:7b" else [{"error": "pull model manifest: file does not exist"}])
        for ln in lines:
            self.wfile.write(json.dumps(ln).encode() + b"\n")
        HAVE_MODEL["yes"] = req["name"] == "qwen2.5vl:7b"


def main():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    cfg = dict(mo.DEFAULT_CONFIG, ollama_url=base)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dest = os.path.join(td, "x.bin")
        with open(dest + ".part", "wb") as f:                       # an interrupted earlier download
            f.write(BLOB[:1_000_000])
        components.download(base + "/file", dest, "test")
        assert open(dest, "rb").read() == BLOB, "resumed download is corrupt"
        assert not os.path.exists(dest + ".part")

    components.pull_model(cfg)
    assert components.STATE["pct"] == 100, components.STATE
    try:
        components.pull_model(dict(cfg, vision_model="nope:1b"))
        raise AssertionError("a failed pull should raise")
    except RuntimeError as e:
        assert "file does not exist" in str(e)

    mo.print = lambda *a: None
    components.os.name  # noqa: B018
    ok = components.ensure(cfg)                                    # server up + model present after pull -> nothing to install
    assert ok and not components.STATE["error"] and components.STATE["step"] == "done", components.STATE
    n = PULLED["n"]
    assert components.ensure(cfg) and PULLED["n"] == n, "second run must not download again"
    HAVE_MODEL["yes"] = False
    assert components.ensure(cfg) and HAVE_MODEL["yes"], "missing model should be pulled"
    assert analyze._model_present(cfg, analyze._ollama(cfg, "/api/tags"))
    print("components test ok")


if __name__ == "__main__":
    main()
