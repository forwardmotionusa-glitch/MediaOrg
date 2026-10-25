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
    cfg = dict(mo.DEFAULT_CONFIG, ollama_url=base, transcribe=False)     # (Whisper setup is tested separately below)

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
    # Programs we launch must not inherit the packaged app's folder on PATH (they'd lock its DLLs)
    old_path, old_mei = os.environ["PATH"], getattr(sys, "_MEIPASS", None)
    try:
        sys._MEIPASS = r"C:\Apps\MediaOrg\_internal"
        os.environ["PATH"] = os.pathsep.join([r"C:\Apps\MediaOrg\_internal", r"C:\Apps\MediaOrg\_internal\sub", r"C:\Windows"])
        env = components.clean_env()
        assert env["PATH"] == r"C:\Windows" and env["PYINSTALLER_RESET_ENVIRONMENT"] == "1", env["PATH"]
    finally:
        os.environ["PATH"] = old_path
        if old_mei is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = old_mei

    # Whisper: the newest *release with Windows binaries* is chosen (the plain v-tags can have none), GPU build preferred
    rel = lambda tag, names: {"tag_name": tag, "assets": [{"name": n, "browser_download_url": "u/" + n, "size": 1,
                                                          "digest": "sha256:abc"} for n in names]}
    pick = components.pick_assets([rel("v1.9.5", []), rel("b5454", ["whisper-bin-x64.zip", "whisper-bin-win-cuda-12.4.0-x64.zip",
                                                                   "whisper-bin-win-cuda-11.8.0-x64.zip"]),
                                   rel("b1", ["whisper-bin-x64.zip"])])
    assert pick["tag"] == "b5454" and pick["gpu"]["name"] == "whisper-bin-win-cuda-12.4.0-x64.zip" and pick["cpu"]["sha256"] == "abc", pick
    assert components.pick_assets([rel("v1", [])]) is None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        sub = Path(td) / "Release"
        sub.mkdir()
        (sub / "whisper-cli.exe").write_bytes(b"x")
        assert components.find_whisper_cli_in(td) == str(sub / "whisper-cli.exe")
        # a GPU build that doesn't run is dropped and the CPU build kept
        import io
        import zipfile
        calls = []
        components.download = lambda url, dest, *a, **k: (zipfile.ZipFile(dest, "w").writestr("Release/whisper-cli.exe", "x") if (os.makedirs(os.path.dirname(dest), exist_ok=True) or True) else 0, dest)[1]
        components._has_nvidia = lambda: True
        components.WHISPER_DIR = Path(td) / "w"
        components.mo.APP_DIR = Path(td)
        a = {"gpu": {"name": "g.zip", "url": "u", "size": 1, "sha256": None}, "cpu": {"name": "c.zip", "url": "u", "size": 1, "sha256": None},
             "tag": "t"}
        variant = components.install_whisper_binary(a, "m.bin", selftest=lambda cli, m: calls.append(cli) or "cpu" in cli)
        assert variant == "CPU" and len(calls) == 2, (variant, calls)
        assert json.loads((components.WHISPER_DIR / "info.json").read_text())["variant"] == "CPU"
        assert not (components.WHISPER_DIR / "gpu").exists(), "failed GPU build should be removed"
    print("components test ok")


if __name__ == "__main__":
    main()
