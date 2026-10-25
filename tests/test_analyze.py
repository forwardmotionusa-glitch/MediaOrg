"""
End-to-end check of phase 2 against a fake Ollama server: analysis rows, event names, and the plan using them.
Uses real ffmpeg when it is installed (CI), otherwise stubs frame extraction.   Run:  python tests/test_analyze.py
"""
import argparse
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze  # noqa: E402
import mediaorg as mo  # noqa: E402

CALLS = {"vision": 0, "name": 0}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"models": [{"name": "qwen2.5vl:7b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msg = body["messages"][0]
        if msg.get("images"):
            CALLS["vision"] += 1
            assert all(len(i) > 100 for i in msg["images"]), "frames were not sent"
            self._send({"message": {"content": json.dumps({"description": "Two kids build a sandcastle on the beach.",
                                                           "tags": ["beach", "kids"]})}})
        else:
            CALLS["name"] += 1
            self._send({"message": {"content": json.dumps({"title": "Beach day", "summary": "A day at the beach.",
                                                           "confidence": 0.9})}})


def main():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = dict(mo.DEFAULT_CONFIG, ollama_url=f"http://127.0.0.1:{srv.server_port}", transcribe=False, frames_per_clip=2)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:   # cmd_plan leaves its db handle open on Windows
        td = Path(td)
        root = td / "drive"
        root.mkdir()
        real_ffmpeg = mo.find_tool("ffmpeg")
        if real_ffmpeg:
            for n in ("a.mp4", "b.mp4"):
                subprocess.run([real_ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
                                str(root / n)], check=True)
        else:
            print("ffmpeg not installed: stubbing frame extraction")
            for n in ("a.mp4", "b.mp4"):
                (root / n).write_bytes(b"x")
            mo.find_tool = lambda name: "ffmpeg-stub" if name == "ffmpeg" else None
            analyze.extract_frames = lambda ff, src, dur, n, out: [_jpg(Path(out) / f"f{i}.jpg") for i in range(n)]

        db = str(td / "t.db")
        con = mo.open_db(db)
        mo.meta_set(con, "root", str(root))
        for n in ("a.mp4", "b.mp4"):
            con.execute("INSERT INTO files(rel,name,ext,kind,size,mtime,date_taken,date_source,date_conf,duration) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (n, n, "mp4", "video", 1000, 0.0, "2024-08-28T10:00:00", "test", "high", 2.0))
        con.commit()
        con.close()

        st = analyze.setup_status(cfg)
        assert st["ready"], st
        counts = analyze.run(cfg, db, threading.Event())
        assert counts["analyzed"] == 2 and counts["errors"] == 0, counts
        assert counts["titled"] == 1, counts
        assert CALLS == {"vision": 2, "name": 1}, CALLS

        again = analyze.run(cfg, db, threading.Event())          # resume: nothing left to do
        assert again["todo"] == 0 and CALLS["vision"] == 2, again

        mo.REPORT_DIR = td / "reports"
        mo.REPORT_DIR.mkdir()
        mo.cmd_plan(argparse.Namespace(db=db, root=None, insta360=None, allow_mtime=False), cfg)
        con = mo.open_db(db)
        dsts = [r["dst"] for r in con.execute("SELECT dst FROM plan_ops")]
        assert all("2024-08-28 Beach day" in d for d in dsts) and len(dsts) == 2, dsts

        cfg2 = dict(cfg, ai_title_min_confidence=0.95)           # not confident enough: plain date folder + flagged
        mo.cmd_plan(argparse.Namespace(db=db, root=None, insta360=None, allow_mtime=False), cfg2)
        last = con.execute("SELECT MAX(plan_id) FROM plan_ops").fetchone()[0]
        dsts = [r["dst"] for r in con.execute("SELECT dst FROM plan_ops WHERE plan_id=?", (last,))]
        assert all("Beach" not in d for d in dsts), dsts
        assert any(f["kind"] == "name" for f in analyze.flags(con, cfg2))
        con.close()
    print("analyze test ok")


def _jpg(path):
    from PIL import Image
    Image.new("RGB", (320, 240), (40, 90, 160)).save(path, "JPEG")
    return str(path)


if __name__ == "__main__":
    main()
