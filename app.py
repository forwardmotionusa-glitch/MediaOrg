#!/usr/bin/env python3
"""
MediaOrg desktop app: a native window (pywebview / WebView2) around the mediaorg engine.

The UI lives in ui/index.html and talks to the Api class below through window.pywebview.api.
Run:  python app.py            (or the packaged MediaOrg.exe)
      python app.py --selftest (import check used by the build)
"""
import argparse
import builtins
import collections
import csv
import datetime as dt
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import mediaorg as mo

try:
    import psutil
except ImportError:  # the dashboard still works, just without CPU/RAM stats
    psutil = None

try:
    import pynvml
except ImportError:
    pynvml = None

UI_DIR = (Path(getattr(sys, "_MEIPASS", "")) / "ui") if mo.FROZEN else Path(__file__).resolve().parent / "ui"
LOG = collections.deque(maxlen=300)
_real_print = builtins.print


def _log_print(*args, **kw):
    """Engine output goes to the Activity panel (and the console, when there is one)."""
    msg = " ".join(str(a) for a in args).strip()
    if msg:
        for line in msg.splitlines():
            if line.strip() and not line.strip().startswith(CLI_ONLY):
                LOG.append({"t": time.strftime("%H:%M:%S"), "m": line.strip()})
    if sys.stdout is not None:
        try:
            _real_print(*args, **kw)
        except Exception:
            pass


CLI_ONLY = ("Inspect:", "also:", "To reverse:", "Nothing has moved. Run 'apply'", "Undo log on the drive")
mo.print = _log_print  # module-level override: every print() inside mediaorg.py lands here


# --------------------------------------------------------------------------- hardware sampler
def cpu_name():
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "CPU"


class Hardware:
    """Samples CPU, RAM, GPU and this app's disk read speed once a second, keeping 60 s of history."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hist = {"cpu": collections.deque(maxlen=60), "gpu": collections.deque(maxlen=60),
                     "read": collections.deque(maxlen=60)}
        self.now = {"cpu_name": cpu_name(), "threads": os.cpu_count() or 0}
        self.gpu = None
        if pynvml:
            try:
                pynvml.nvmlInit()
                if pynvml.nvmlDeviceGetCount() > 0:
                    self.gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
                    name = pynvml.nvmlDeviceGetName(self.gpu)
                    self.now["gpu_name"] = name.decode() if isinstance(name, bytes) else name
            except Exception:
                self.gpu = None
        self.proc = psutil.Process() if psutil else None
        self._last_read = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _read_bytes(self):
        try:
            return self.proc.io_counters().read_bytes
        except Exception:
            return None

    def _loop(self):
        if psutil:
            psutil.cpu_percent(percpu=True)
        last_t = time.time()
        self._last_read = self._read_bytes() if self.proc else None
        while True:
            time.sleep(1)
            snap = {}
            if psutil:
                per = psutil.cpu_percent(percpu=True)
                snap["cpu"] = round(sum(per) / max(len(per), 1))
                snap["cores"] = [round(x) for x in per]
                vm = psutil.virtual_memory()
                snap["ram_used"], snap["ram_total"] = vm.used, vm.total
            t = time.time()
            rb = self._read_bytes() if self.proc else None
            if rb is not None and self._last_read is not None:
                snap["read_mbs"] = round((rb - self._last_read) / max(t - last_t, 0.001) / 1e6, 1)
            self._last_read, last_t = rb, t
            if self.gpu is not None:
                g = {}
                for key, fn in (
                        ("util", lambda h: pynvml.nvmlDeviceGetUtilizationRates(h).gpu),
                        ("mem", lambda h: pynvml.nvmlDeviceGetMemoryInfo(h)),
                        ("temp", lambda h: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)),
                        ("power", lambda h: round(pynvml.nvmlDeviceGetPowerUsage(h) / 1000)),
                        ("dec", lambda h: pynvml.nvmlDeviceGetDecoderUtilization(h)[0])):
                    try:
                        g[key] = fn(self.gpu)
                    except Exception:
                        pass
                if "mem" in g:
                    g["mem_used"], g["mem_total"] = g["mem"].used, g["mem"].total
                    del g["mem"]
                snap["gpu"] = g
            with self.lock:
                self.now.update(snap)
                self.hist["cpu"].append(snap.get("cpu", 0))
                self.hist["gpu"].append((snap.get("gpu") or {}).get("util", 0))
                self.hist["read"].append(snap.get("read_mbs", 0))

    def state(self):
        with self.lock:
            out = dict(self.now)
            out["hist"] = {k: list(v) for k, v in self.hist.items()}
            out["has_gpu"] = self.gpu is not None
            return out


# --------------------------------------------------------------------------- helpers
REVIEW_GROUPS = {
    "projects": ["editing project inside"],
    "dates": ["folder has no reliable dates", "only file-modified date"],
    "clash": ["name clash at destination", "destination folder already exists"],
    "other": ["non-media file left in place", "no media in folder", "read error", "move failed - skipped"],
}
GROUP_OF = {r: g for g, rs in REVIEW_GROUPS.items() for r in rs}


def human(n):
    n = float(n or 0)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def volume_label(root):
    if os.name != "nt":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        drive = os.path.splitdrive(root)[0] + "\\"
        if ctypes.windll.kernel32.GetVolumeInformationW(drive, buf, 261, None, None, None, None, 0):
            return buf.value
    except Exception:
        pass
    return ""


def camera_of(r):
    n = r["name"].upper()
    if r["kind"] == "360video" or r["insta_key"]:
        return "360"
    if re.match(r"^(GX|GH|GOPR|GP)\d", n):
        return "GoPro"
    if n.startswith("DJI_"):
        return "DJI"
    if re.match(r"^(PXL_|IMG_|VID_|MVIMG|SCREEN)", n) or r["ext"] in ("mov", "heic"):
        return "Phone"
    return "Video"


def fmt_dur(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- API exposed to the UI
class Api:
    def __init__(self):
        self._cfg = mo.load_config()
        self._db_path = str(mo.DATA_DIR / "mediaorg.db")
        mo.open_db(self._db_path).close()
        self._hw = Hardware()
        self._job = {"name": None, "running": False, "error": None, "finished": None}
        self._window = None

    # ---- plumbing
    def _con(self):
        con = sqlite3.connect(self._db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _drive(self):
        f = mo.DATA_DIR / "drive.txt"
        return f.read_text(encoding="utf-8").strip() if f.exists() else ""

    def _ns(self, **kw):
        base = dict(db=self._db_path, root=None, workers=None, insta360=None, allow_mtime=False,
                    yes=True, prune_empty=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def _run(self, name, fn):
        if self._job["running"]:
            return {"ok": False, "error": f"Already busy: {self._job['name']}"}
        mo.STOP.clear()
        self._job.update(name=name, running=True, error=None, finished=None)
        LOG.append({"t": time.strftime("%H:%M:%S"), "m": f"--- {name} started"})

        def work():
            try:
                fn()
            except SystemExit as e:
                if e.code not in (None, 0):
                    self._job["error"] = str(e.code)
                    LOG.append({"t": time.strftime("%H:%M:%S"), "m": str(e.code)})
            except Exception as e:
                self._job["error"] = f"{type(e).__name__}: {e}"
                LOG.append({"t": time.strftime("%H:%M:%S"), "m": "ERROR " + self._job["error"]})
                traceback.print_exc()
            finally:
                self._job.update(running=False, finished=time.time())
                if mo.PROGRESS.get("phase") in ("walking", "indexing", "applying"):
                    mo.set_progress(phase="stopped" if mo.STOP.is_set() else "idle")
                LOG.append({"t": time.strftime("%H:%M:%S"), "m": f"--- {name} finished"})

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    # ---- state for the dashboard (polled every second)
    def state(self):
        drive = self._drive()
        out = {"version": mo.VERSION, "drive": drive, "job": dict(self._job), "hw": self._hw.state(),
               "progress": dict(mo.PROGRESS), "log": list(LOG)[-60:][::-1], "data_dir": str(mo.APP_DIR)}
        p = out["progress"]
        p["pct"] = round(100 * p["done"] / p["total"], 1) if p.get("total") else 0
        if drive and os.path.isdir(drive):
            try:
                du = shutil.disk_usage(drive)
                out["drive_info"] = {"label": volume_label(drive), "used": human(du.used), "total": human(du.total),
                                     "pct": round(100 * du.used / du.total), "ok": True}
            except OSError:
                out["drive_info"] = {"ok": False}
        else:
            out["drive_info"] = {"ok": False}
        con = self._con()
        try:
            out["files"] = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            out["last_index"] = mo.meta_get(con, "last_index")
            plan = con.execute("SELECT * FROM plans ORDER BY plan_id DESC LIMIT 1").fetchone()
            if plan:
                c = dict(con.execute("SELECT op, COUNT(*) FROM plan_ops WHERE plan_id=? GROUP BY op",
                                     (plan["plan_id"],)).fetchall())
                last = con.execute("SELECT COUNT(*), SUM(undone) FROM moves WHERE batch=(SELECT MAX(batch) FROM moves)").fetchone()
                undone = bool(plan["applied"] and last[0] and last[1] == last[0])
                out["plan"] = {"id": plan["plan_id"], "applied": bool(plan["applied"]) and not undone, "undone": undone,
                               "created": plan["created"], "dirs": c.get("dir", 0), "files": c.get("file", 0)}
                rows = con.execute("""SELECT r.reason, d.action FROM review r LEFT JOIN decisions d ON d.rel = r.rel
                                      WHERE r.plan_id=?""", (plan["plan_id"],)).fetchall()
                counts = collections.Counter()
                open_ = collections.Counter()
                for r in rows:
                    g = GROUP_OF.get(r["reason"], "other")
                    counts[g] += 1
                    if not r["action"] and g != "other":
                        open_[g] += 1
                out["review"] = {"counts": dict(counts), "open": dict(open_), "open_total": sum(open_.values())}
            dec_n = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            newer = con.execute("SELECT COUNT(*) FROM decisions WHERE ts > ?",
                                (plan["created"] if plan else "",)).fetchone()[0]
            out["decisions"] = {"total": dec_n, "since_plan": newer}
            dups = con.execute("""SELECT COUNT(*), COALESCE(SUM(waste),0) FROM (SELECT fp, (COUNT(*)-1)*MAX(size) AS waste
                                  FROM files WHERE fp IS NOT NULL AND kind IN ('video','photo','raw','360video','360photo')
                                  GROUP BY fp HAVING COUNT(*)>1)""").fetchone()
            out["dups"] = {"sets": dups[0], "waste": human(dups[1])}
        finally:
            con.close()
        return out

    # ---- drive
    def pick_drive(self):
        import webview
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG) if self._window else None
        if res:
            return self.set_drive(res[0] if isinstance(res, (list, tuple)) else res)
        return {"ok": False}

    def set_drive(self, path):
        path = (path or "").strip().strip('"')
        if re.fullmatch(r"[A-Za-z]:\\?", path):
            path = path[:2] + os.sep
        if not os.path.isdir(path):
            return {"ok": False, "error": f"Can't find {path}. Is the drive plugged in?"}
        (mo.DATA_DIR / "drive.txt").write_text(path, encoding="utf-8")
        return {"ok": True, "drive": path}

    # ---- jobs
    def start_scan(self):
        drive = self._drive()
        if not drive:
            return {"ok": False, "error": "Choose a drive first."}

        def go():
            if mo.cmd_index(self._ns(root=drive), self._cfg) == "stopped":
                return
            mo.cmd_plan(self._ns(), self._cfg)
            mo.cmd_report(self._ns(), self._cfg)
        return self._run("Scan", go)

    def make_plan(self):
        def go():
            mo.cmd_plan(self._ns(), self._cfg)
            mo.cmd_report(self._ns(), self._cfg)
        return self._run("Plan", go)

    def apply_plan(self, prune=True):
        def go():
            mo.cmd_apply(self._ns(prune_empty=bool(prune)), self._cfg)
            mo.cmd_report(self._ns(), self._cfg)
        return self._run("Apply", go)

    def undo(self):
        def go():
            mo.cmd_undo(self._ns(), self._cfg)
            mo.cmd_report(self._ns(), self._cfg)
        return self._run("Undo", go)

    def stop(self):
        mo.STOP.set()
        return {"ok": True}

    # ---- action items
    def review(self):
        con = self._con()
        try:
            root = mo.meta_get(con, "root") or self._drive()
            plan = con.execute("SELECT * FROM plans ORDER BY plan_id DESC LIMIT 1").fetchone()
            out = {k: [] for k in ("projects", "dates", "clash", "other")}
            if plan:
                dec = {r["rel"]: dict(r) for r in con.execute("SELECT * FROM decisions")}
                for r in con.execute("SELECT * FROM review WHERE plan_id=? ORDER BY rel", (plan["plan_id"],)):
                    g = GROUP_OF.get(r["reason"], "other")
                    item = {"rel": r["rel"], "reason": r["reason"], "detail": r["detail"] or "",
                            "is_dir": r["rel"].endswith(os.sep), "decision": dec.get(r["rel"])}
                    if item["is_dir"] or g in ("projects", "dates"):
                        prefix = r["rel"] if item["is_dir"] else r["rel"]
                        if item["is_dir"]:
                            p = r["rel"]
                            rows = con.execute("SELECT name, kind, size, mtime, date_taken FROM files "
                                               "WHERE substr(rel,1,?)=?", (len(p), p)).fetchall()
                        else:
                            rows = con.execute("SELECT name, kind, size, mtime, date_taken FROM files WHERE rel=?",
                                               (prefix,)).fetchall()
                        item["count"] = len(rows)
                        item["size"] = human(sum(x["size"] or 0 for x in rows))
                        ds = sorted(x["date_taken"][:10] for x in rows if x["date_taken"])
                        item["dates"] = f"{ds[0]} – {ds[-1]}" if ds and ds[0] != ds[-1] else (ds[0] if ds else "")
                        item["projects"] = [x["name"] for x in rows if x["kind"] == "project"][:3]
                        mt = [x["mtime"] for x in rows if x["mtime"]]
                        item["suggest"] = dt.datetime.fromtimestamp(min(mt)).strftime("%Y-%m-%d") if mt else ""
                    out[g].append(item)
            # duplicates (largest first)
            dups = []
            for fp, n, size in con.execute(
                    """SELECT fp, COUNT(*), MAX(size) FROM files WHERE fp IS NOT NULL
                       AND kind IN ('video','photo','raw','360video','360photo')
                       GROUP BY fp HAVING COUNT(*)>1 ORDER BY MAX(size) DESC LIMIT 200"""):
                paths = [x[0] for x in con.execute("SELECT rel FROM files WHERE fp=? LIMIT 10", (fp,))]
                dups.append({"size": human(size), "waste": human(size * (n - 1)), "paths": paths, "n": n})
            out["dups"] = dups
            # Insta360 sets missing a lens file
            sets = collections.defaultdict(list)
            for r in con.execute("SELECT rel, name, insta_key FROM files WHERE insta_key IS NOT NULL"):
                sets[(os.path.dirname(r["rel"]), r["insta_key"])].append(r["name"])
            lonely = []
            for (folder, key), names in sorted(sets.items()):
                has00 = any(re.search(r"_00_\d{3}\.insv$", n, re.I) for n in names)
                has10 = any(re.search(r"_10_\d{3}\.insv$", n, re.I) for n in names)
                if has00 and not has10:
                    lonely.append({"folder": folder, "key": key, "names": names})
            out["insta"] = lonely
            out["root"] = root
            return out
        finally:
            con.close()

    def decide(self, rel, action, value=None):
        con = self._con()
        try:
            if action == "clear":
                con.execute("DELETE FROM decisions WHERE rel=?", (rel,))
            else:
                if action == "date":
                    dt.date.fromisoformat(value)  # validate
                con.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?)",
                            (rel, action, value, dt.datetime.now().isoformat(timespec="seconds")))
            con.commit()
            return {"ok": True}
        except ValueError:
            return {"ok": False, "error": "Pick a valid date."}
        finally:
            con.close()

    def decide_all(self, group, action):
        items = self.review().get(group, [])
        for it in items:
            if not it["decision"]:
                self.decide(it["rel"], action)
        return {"ok": True, "count": len(items)}

    # ---- plan screen
    def plan_view(self):
        con = self._con()
        try:
            plan = con.execute("SELECT * FROM plans ORDER BY plan_id DESC LIMIT 1").fetchone()
            out = {"plan": None, "history": []}
            if plan:
                ops = con.execute("SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan["plan_id"],)).fetchall()
                groups = {o["grp"] for o in ops}
                sample, seen_kinds = [], collections.Counter()
                for o in ops:  # a varied sample: a few of each kind of move
                    k = o["grp"].split(":", 1)[0]
                    if seen_kinds[k] < 4:
                        seen_kinds[k] += 1
                        sample.append({"src": o["src"] + (os.sep if o["op"] == "dir" else ""),
                                       "dst": o["dst"] + (os.sep if o["op"] == "dir" else ""), "op": o["op"]})
                out["plan"] = {
                    "id": plan["plan_id"], "created": plan["created"], "applied": bool(plan["applied"]),
                    "settings": json.loads(plan["settings"] or "{}"),
                    "dirs": sum(1 for o in ops if o["op"] == "dir"),
                    "files": sum(1 for o in ops if o["op"] == "file"),
                    "insta": sum(1 for g in groups if g.startswith("insta:")),
                    "total": len(ops), "sample": sample,
                    "review": con.execute("SELECT COUNT(*) FROM review WHERE plan_id=?", (plan["plan_id"],)).fetchone()[0],
                }
            for b in con.execute("""SELECT batch, COUNT(*) n, MIN(ts) ts, SUM(undone) undone
                                    FROM moves GROUP BY batch ORDER BY batch DESC LIMIT 10"""):
                out["history"].append(dict(b))
            out["undoable"] = next((h["batch"] for h in out["history"] if h["undone"] < h["n"]), None)
            out["root"] = mo.meta_get(con, "root") or self._drive()
            return out
        finally:
            con.close()

    # ---- edit queue
    def edit_queue(self, camera="all"):
        con = self._con()
        try:
            root = mo.meta_get(con, "root") or self._drive()
            rows = [dict(r) for r in con.execute(
                "SELECT rel, name, ext, kind, insta_key, date_taken, duration, width, height, size FROM files "
                "WHERE kind IN ('video','360video') AND date_taken IS NOT NULL ORDER BY date_taken")]
        finally:
            con.close()
        gap = dt.timedelta(hours=self._cfg["session_gap_hours"])
        sessions, last = [], None
        for r in rows:
            d = dt.datetime.fromisoformat(r["date_taken"])
            if last is None or d - last > gap:
                sessions.append({"num": len(sessions) + 1, "start": d, "end": d, "clips": []})
            s = sessions[-1]
            s["end"] = d
            last = d
            r["cam"] = camera_of(r)
            r["time"] = d.strftime("%H:%M")
            r["dur"] = fmt_dur(r["duration"])
            s["clips"].append(r)
        out = []
        for s in reversed(sessions):
            clips = s["clips"] if camera == "all" else [c for c in s["clips"] if c["cam"] == camera]
            if not clips:
                continue
            cams = collections.Counter(c["cam"] for c in s["clips"])
            folders = collections.Counter(os.path.dirname(c["rel"]) for c in s["clips"])
            total = sum(c["duration"] or 0 for c in s["clips"])
            out.append({
                "num": s["num"], "when": f"{s['start']:%a %b %d, %Y} · {s['start']:%I:%M %p} – {s['end']:%I:%M %p}",
                "title": folders.most_common(1)[0][0] or "(drive root)",
                "meta": f"{len(s['clips'])} clip{'s' if len(s['clips']) != 1 else ''} · {fmt_dur(total)} · " + " + ".join(cams) + " · "
                        + human(sum(c["size"] or 0 for c in s["clips"])),
                "clips": [{"rel": c["rel"], "name": c["name"], "time": c["time"], "dur": c["dur"], "cam": c["cam"]}
                          for c in clips[:24]],
                "more": max(len(clips) - 24, 0),
                "folder": folders.most_common(1)[0][0],
            })
            if len(out) >= 150:
                break
        return {"sessions": out, "root": root, "total_sessions": len(sessions), "total_clips": len(rows)}

    def copy_session(self, num):
        con = self._con()
        root = mo.meta_get(con, "root") or self._drive()
        con.close()
        q = self.edit_queue()
        s = next((x for x in q["sessions"] if x["num"] == num), None)
        if not s:
            return {"ok": False}
        text = "\r\n".join(os.path.join(root, c["rel"]) for c in s["clips"])
        if os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), creationflags=mo.NO_WINDOW)
        return {"ok": True, "count": len(s["clips"])}

    # ---- files and reports
    def open_path(self, rel, select=False):
        con = self._con()
        root = mo.meta_get(con, "root") or self._drive()
        con.close()
        p = rel if os.path.isabs(rel) else os.path.join(root, rel)
        p = p.rstrip("\\/") or p
        if not os.path.exists(p):
            return {"ok": False, "error": "That file or folder isn't there any more."}
        if os.name == "nt":
            if select:
                subprocess.Popen(["explorer", "/select,", p])
            else:
                os.startfile(p)
        return {"ok": True}

    def open_report(self):
        mo.open_path(mo.REPORT_DIR / "LATEST_REPORT.txt")
        return {"ok": True}

    def open_reports_folder(self):
        mo.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        mo.open_path(mo.REPORT_DIR)
        return {"ok": True}

    def report_text(self):
        p = mo.REPORT_DIR / "LATEST_REPORT.txt"
        if not p.exists():
            return {"ok": False, "text": ""}
        return {"ok": True, "text": p.read_text(encoding="utf-8"),
                "when": dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d, %I:%M %p")}

    def prompt_text(self):
        t = self.report_text().get("text", "")
        i = t.find("## Prompt for Claude")
        return {"text": t[i:] if i >= 0 else ""}

    def copy_prompt(self):
        text = self.prompt_text()["text"]
        if not text:
            return {"ok": False, "error": "No report yet. Run a scan first."}
        if os.name == "nt":
            subprocess.run(["clip"], input=text.replace("\n", "\r\n").encode("utf-16-le"), creationflags=mo.NO_WINDOW)
        return {"ok": True}

    def regenerate_report(self):
        return self._run("Report", lambda: mo.cmd_report(self._ns(), self._cfg))

    # ---- settings
    def get_settings(self):
        return {"cfg": self._cfg, "path": str(mo.CONFIG_PATH), "data_dir": str(mo.APP_DIR)}

    def save_settings(self, new):
        cfg = dict(self._cfg)
        try:
            cfg["organized_folder_name"] = mo.safe_name(str(new["organized_folder_name"]).strip()) or "_Organized"
            cfg["insta360_mode"] = "separate" if new.get("insta360_mode") == "separate" else "together"
            cfg["move_low_confidence_dates"] = bool(new.get("move_low_confidence_dates"))
            cfg["session_gap_hours"] = max(0.25, float(new["session_gap_hours"]))
            cfg["wide_span_days"] = max(1, int(new["wide_span_days"]))
            cfg["workers"] = min(32, max(1, int(new["workers"])))
            names = [x.strip().lower() for x in str(new.get("generic_folder_names", "")).splitlines() if x.strip()]
            if names:
                cfg["generic_folder_names"] = sorted(set(names))
        except (KeyError, ValueError, TypeError) as e:
            return {"ok": False, "error": f"Check the values ({e})."}
        mo.CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self._cfg = cfg
        mo.is_generic.__defaults__[0].clear()  # folder-name cache
        return {"ok": True}


# --------------------------------------------------------------------------- startup helpers
def unblock_bundle():
    """Windows marks files extracted from a downloaded zip as "from the internet" (the Zone.Identifier
    stream). .NET then refuses to load the window's DLLs. Clear that mark from MediaOrg's own bundled
    files only - the same thing as ticking "Unblock" on the zip. Returns how many files were unblocked."""
    if os.name != "nt" or not mo.FROZEN:
        return 0
    n = 0
    base = Path(sys.executable).resolve().parent
    for dirpath, _, files in os.walk(base):
        for f in files:
            if f.lower().endswith((".dll", ".exe", ".pyd", ".config")):
                try:
                    os.remove(os.path.join(dirpath, f) + ":Zone.Identifier")
                    n += 1
                except OSError:
                    pass
    return n


def fatal_dialog(title, text):
    """The packaged app has no console, so show startup problems in a normal Windows message box."""
    log = mo.APP_DIR / "startup_error.txt"
    try:
        mo.APP_DIR.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
    except OSError:
        pass
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{text}\n\nDetails saved to:\n{log}", title, 0x10)
    else:
        print(title, text, sep="\n")


UITEST_JS = r"""
(async () => {
  const r = {};
  r.bridge_global = window.chrome && window.chrome.webview ? "ok" : "ERR window.chrome.webview missing (a page global is shadowing window.chrome?)";
  const calls = [["state", []], ["review", []], ["plan_view", []], ["edit_queue", ["all"]],
                 ["get_settings", []], ["report_text", []]];
  for (const [m, a] of calls) {
    const t = performance.now();
    try { const v = await window.pywebview.api[m](...a); r[m] = "ok " + JSON.stringify(v).length + "B " + Math.round(performance.now() - t) + "ms"; }
    catch (e) { r[m] = "ERR " + (e && e.name) + ": " + (e && e.message); }
  }
  r.dashboard = document.getElementById("main").innerText.slice(0, 160);
  location.hash = "#settings";
  await new Promise(res => setTimeout(res, 2500));
  r.settings_page = document.getElementById("main").innerText.slice(0, 120);
  location.hash = "#review";
  await new Promise(res => setTimeout(res, 2500));
  r.review_page = document.getElementById("main").innerText.slice(0, 120);
  r.errors = window.__errors || [];
  window.__uitest = r;
})();
"""


def uitest():
    """Build check: open the real window, drive the UI through the real JS<->Python bridge, record results."""
    out = Path(os.environ.get("TEMP", "/tmp")) / "mediaorg_uitest.txt"
    import webview
    api = Api()
    win = webview.create_window("MediaOrg uitest", str(UI_DIR / "index.html"), js_api=api, width=1200, height=800)
    result = {}

    def drive():
        try:
            time.sleep(8)  # let the page load and boot
            win.evaluate_js(UITEST_JS)
            for _ in range(60):
                time.sleep(1)
                got = win.evaluate_js("JSON.stringify(window.__uitest || null)")
                if got and got != "null":
                    result.update(json.loads(got))
                    break
            else:
                result["error"] = "UI test did not finish"
        except Exception:
            result["error"] = traceback.format_exc()
        out.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        win.destroy()

    webview.start(drive, http_server=True)
    ok = (not result.get("error") and all(str(result.get(k, "")).startswith("ok") for k in
          ("bridge_global", "state", "review", "plan_view", "edit_queue", "get_settings", "report_text"))
          and "Settings" in result.get("settings_page", "") and not result.get("errors"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- main
def main():
    unblocked = unblock_bundle()
    if "--selftest" in sys.argv:
        # Used by the build: proves the GUI stack and UI files are bundled. Writes its result to a file
        # because the windowed exe has no console.
        out = Path(os.environ.get("TEMP", "/tmp")) / "mediaorg_selftest.txt"
        try:
            import webview  # noqa: F401
            if os.name == "nt":
                import webview.platforms.winforms  # noqa: F401  (pythonnet + WebView2 loader)
            assert (UI_DIR / "index.html").is_file(), f"UI missing at {UI_DIR}"
            api = Api()
            st = api.state()
            msg = (f"selftest ok {mo.VERSION} ffprobe={bool(mo.FFPROBE)} psutil={bool(psutil)} "
                   f"nvml={bool(pynvml)} unblocked={unblocked} cpu={st['hw'].get('cpu_name')}")
            out.write_text(msg, encoding="utf-8")
            print(msg)
            return 0
        except Exception:
            out.write_text("selftest FAILED\n" + traceback.format_exc(), encoding="utf-8")
            traceback.print_exc()
            return 1
    if "--uitest" in sys.argv:
        return uitest()
    try:
        import webview
        api = Api()
        api._window = webview.create_window(f"MediaOrg {mo.VERSION}", str(UI_DIR / "index.html"), js_api=api,
                                           width=1360, height=900, min_size=(900, 640), background_color="#0E1014")
        webview.start(http_server=True, debug="--debug" in sys.argv)
    except Exception:
        tb = traceback.format_exc()
        hint = "MediaOrg couldn't open its window."
        if "Python.Runtime" in tb or "0x80131515" in tb:
            hint += ("\n\nWindows is blocking files from the downloaded zip. Delete this folder, right-click the "
                     "zip > Properties > tick Unblock > OK, then extract it again.")
        elif "WebView2" in tb or "EdgeChromium" in tb:
            hint += ("\n\nThe Microsoft Edge WebView2 Runtime seems to be missing. Install it from "
                     "https://developer.microsoft.com/microsoft-edge/webview2/ and try again.")
        fatal_dialog("MediaOrg", f"{hint}\n\n{tb[-1500:]}")
        return 1
    mo.STOP.set()  # window closed: let a running scan stop at its next safe point
    return 0


if __name__ == "__main__":
    sys.exit(main())
