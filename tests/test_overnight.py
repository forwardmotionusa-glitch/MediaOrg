"""
The overnight chain through the real Api: scan -> analyze -> plan -> report, then again with apply, on a throwaway
folder and a fake Ollama. Frame extraction is stubbed (no ffmpeg needed).   Run:  python tests/test_overnight.py
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_analyze as ta  # noqa: E402  (fake Ollama server + stub helpers)
import analyze  # noqa: E402
import mediaorg as mo  # noqa: E402


def wait(api, what):
    t0 = time.time()
    while api._job["running"]:
        assert time.time() - t0 < 120, f"{what} did not finish"
        time.sleep(0.2)


def main():
    from http.server import HTTPServer
    srv = HTTPServer(("127.0.0.1", 0), ta.Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        mo.DATA_DIR = td / "data"
        mo.REPORT_DIR = td / "reports"
        mo.CONFIG_PATH = mo.DATA_DIR / "config.json"
        mo.find_tool = lambda name: "ffmpeg-stub" if name == "ffmpeg" else None
        analyze.extract_frames = lambda ff, src, dur, n, out, hw=False: [ta._jpg(Path(out) / f"f{i}.jpg") for i in range(n)]
        import app
        drive = td / "drive"
        drive.mkdir()
        for n in ("VID_20240828_100000.mp4", "VID_20240828_130000.mp4"):
            (drive / n).write_bytes(b"x" * 2048)
        api = app.Api()
        api._cfg.update(ollama_url=f"http://127.0.0.1:{srv.server_port}", transcribe=False, frames_per_clip=2)
        assert api.set_drive(str(drive))["ok"]

        assert api.overnight_start(False)["ok"]
        wait(api, "overnight")
        s = api.state()
        last = s["overnight"]["last"]
        assert last["status"] == "complete", json.dumps(last, indent=1)
        assert [x["name"] for x in last["steps"]] == ["Scan", "Analyze", "Plan", "Report"], last["steps"]
        assert all(x["ok"] for x in last["steps"]), last["steps"]
        assert last["counts"]["described"] == 2 and last["counts"]["days_named"] == 1, last["counts"]
        assert last["counts"]["plan_moves"] == 2 and not last["counts"]["applied"], last["counts"]
        assert not s["overnight"]["pending"]
        assert (drive / "VID_20240828_100000.mp4").exists(), "must not move without apply"

        assert api.overnight_start(True)["ok"]
        wait(api, "overnight+apply")
        last = api.state()["overnight"]["last"]
        assert last["status"] == "complete" and last["counts"]["applied"], last
        moved = list(drive.rglob("*.mp4"))
        assert len(moved) == 2 and all("2024-08-28 Beach day" in str(p) for p in moved), moved

        api.undo()
        wait(api, "undo")
        assert (drive / "VID_20240828_100000.mp4").exists(), "undo should restore"

        # Ollama down: the run still finishes, with the analyze problem recorded for the morning
        api._cfg["ollama_url"] = "http://127.0.0.1:9"
        assert api.overnight_start(False)["ok"]
        wait(api, "overnight without ollama")
        last = api.state()["overnight"]["last"]
        an = next(x for x in last["steps"] if x["name"] == "Analyze")
        assert not an["ok"] and "Ollama" in an["note"], an
        assert any(x["name"] == "Plan" and x["ok"] for x in last["steps"]), last["steps"]
    print("overnight test ok")


if __name__ == "__main__":
    main()
