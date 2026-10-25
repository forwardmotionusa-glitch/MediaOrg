#!/usr/bin/env python3
"""
MediaOrg - Phase 1: index, date-sort and organize a media drive IN PLACE.

  index   Scan the drive, read real capture dates, fingerprint files (resumable).
  plan    Propose moves. Nothing is touched. Writes plan.csv + review.csv.
  apply   Execute the plan (same-drive renames = instant). Logs every move.
  undo    Reverse the last applied batch.
  report  Markdown report + edit queue + "prompt for Claude" section.
  run     index + plan + report in one go (still no moves).

Rules:
  * Files are only ever renamed/moved on the same drive. Never copied, never deleted.
  * Anything needing a human decision is skipped and added to the review list.
  * Named folders (e.g. "Chariot Dump #1") are kept whole and moved as one unit.
  * Insta360 pairs (_00_/_10_) and their LRV proxies always stay together.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = "0.3.0"
FROZEN = getattr(sys, "frozen", False)           # running as the packaged MediaOrg.exe
if FROZEN:
    APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MediaOrg"   # writable, survives updates
    TOOLS_DIR = Path(sys.executable).resolve().parent / "tools"
else:
    APP_DIR = Path(__file__).resolve().parent
    TOOLS_DIR = APP_DIR / "tools"
DATA_DIR = APP_DIR / "data"
REPORT_DIR = APP_DIR / "reports"
CONFIG_PATH = DATA_DIR / "config.json"

# --------------------------------------------------------------------------- live state for the dashboard
STOP = threading.Event()          # set() asks a running index/apply to stop at the next safe point
PROGRESS = {"phase": "idle", "done": 0, "total": 0, "bytes": 0, "rate": 0.0, "eta": None,
            "current": "", "started": None}


def set_progress(**kw):
    PROGRESS.update(kw)


# --------------------------------------------------------------------------- optional libs
try:
    import xxhash

    def new_hasher():
        return xxhash.xxh3_128()
except ImportError:  # fall back to stdlib
    def new_hasher():
        return hashlib.blake2b(digest_size=16)

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HAVE_HEIF = True
except Exception:
    HAVE_HEIF = False

# --------------------------------------------------------------------------- file types
VIDEO_EXT = {"mp4", "mov", "m4v", "avi", "mkv", "mts", "m2ts", "wmv", "3gp", "webm", "mpg", "mpeg", "flv", "mxf"}
PHOTO_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp", "tif", "tiff"}
RAW_EXT = {"dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2"}
AUDIO_EXT = {"wav", "mp3", "m4a", "aac", "flac"}
SIDECAR_EXT = {"lrv", "thm", "xmp", "srt", "aae"}
PROJECT_EXT = {"prproj", "drp", "drt", "aep", "veg", "kdenlive", "mlt", "wfp", "tscproj", "camproj", "fcpxml", "insprj"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
SKIP_DIRS = {"$recycle.bin", "system volume information", ".trashes", ".spotlight-v100", ".fseventsd", ".temporaryitems", "_mediaorg_logs"}

MEDIA_KINDS = {"video", "photo", "raw", "360video", "360photo"}

# Insta360: VID_20240828_143012_00_005.insv, VID_..._10_005.insv, LRV_..._01_005.lrv, IMG_..._00_005.insp, PRO_VID_...
INSTA_RE = re.compile(r"^(?P<pre>PRO_VID|PRO_LRV|VID|LRV|IMG)_(?P<d>\d{8})_(?P<t>\d{6})_(?P<lens>\d{2})_(?P<seq>\d{3})", re.I)

DEFAULT_CONFIG = {
    "organized_folder_name": "_Organized",
    "insta360_mode": "together",          # together = YYYY/date/360/   separate = 360/YYYY/date/
    "move_low_confidence_dates": False,   # files dated only by "modified" time go to review
    "session_gap_hours": 3,
    "wide_span_days": 60,
    "workers": 4,                         # HDD-friendly; raise for NVMe
    # phase 2: local analysis (Ollama vision model + optional Whisper), see analyze.py
    "use_ai_titles": True,                # name loose-file day folders "2024-08-28 Beach day" when confident
    "ai_title_min_confidence": 0.6,       # lower-confidence names are flagged for review instead of applied
    "vision_model": "qwen2.5vl:7b",
    "ollama_url": "http://127.0.0.1:11434",
    "frames_per_clip": 4,
    "transcribe": True,                   # Whisper transcripts, when faster-whisper or whisper-cli is installed
    "whisper_model": "small",
    "analyze_photos": False,
    "auto_setup": True,                   # on start, download Ollama and the vision model if they're missing
    "generic_folder_names": [
        "dcim", "camera", "camera roll", "camera uploads", "photos", "pictures", "videos", "video",
        "movies", "images", "img", "media", "footage", "raw", "download", "downloads", "new folder",
        "untitled folder", "misc", "backup", "backups", "sd card", "sdcard", "card", "insta360",
        "insta360 one x2", "insta360 x3", "insta360 x4", "dji", "dji album", "gopro", "iphone",
        "android", "whatsapp", "whatsapp images", "whatsapp video", "screenshots", "screen recordings",
        "export", "exports", "imports", "import", "dump", "temp", "tmp", "stuff", "files", "archive",
        "external", "drive", "hdd", "phone", "camera01", "100media", "private", "m4root", "clip",
        "avchd", "bdmv", "stream"
    ],
    # regexes (case-insensitive) that also mark a folder as generic
    "generic_folder_patterns": [
        r"^\d{3}[a-z0-9_]{5}$",                     # 100GOPRO, 100CANON, 100MEDIA
        r"^(19|20)\d{2}([-_. ]?\d{2}){0,2}$",       # 2024, 2024-08, 2024_08_28
        r"^new folder( \(\d+\))?$",
        r"^(camera|card|disk|drive|backup|dump)[ _-]?\d+$",
    ],
}


def load_config():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"! config.json unreadable ({e}); using defaults")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return cfg


# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  rel TEXT PRIMARY KEY, name TEXT, ext TEXT, kind TEXT, size INTEGER, mtime REAL,
  date_taken TEXT, date_source TEXT, date_conf TEXT,
  duration REAL, width INTEGER, height INTEGER, codec TEXT,
  fp TEXT, insta_key TEXT, error TEXT, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS plans(plan_id INTEGER PRIMARY KEY, created TEXT, settings TEXT, applied INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS plan_ops(plan_id INTEGER, seq INTEGER, op TEXT, src TEXT, dst TEXT, grp TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS review(plan_id INTEGER, rel TEXT, reason TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS decisions(rel TEXT PRIMARY KEY, action TEXT, value TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS moves(batch INTEGER, seq INTEGER, op TEXT, src TEXT, dst TEXT, ts TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS analysis(rel TEXT PRIMARY KEY, status TEXT, description TEXT, tags TEXT, transcript TEXT,
  error TEXT, tries INTEGER DEFAULT 0, model TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS events(day TEXT PRIMARY KEY, title TEXT, summary TEXT, confidence REAL, clips INTEGER, ts TEXT);
CREATE INDEX IF NOT EXISTS ix_files_fp ON files(fp);
"""


def open_db(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def meta_get(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))


def get_root(con, given=None):
    if given:
        given = given.strip().strip('"')           # cmd.exe turns  "E:\"  into  E:"
        if re.fullmatch(r"[A-Za-z]:\\?", given):
            given = given[:2] + os.sep
        root = os.path.abspath(given)
        old = meta_get(con, "root")
        if old and os.path.normcase(old) != os.path.normcase(root):
            print(f"  Note: root changed {old} -> {root} (paths are stored relative, so this is fine "
                  f"if it's the same drive under a new letter).")
        meta_set(con, "root", root)
        con.commit()
        return root
    root = meta_get(con, "root")
    if not root:
        sys.exit("No drive indexed yet. Run:  mediaorg.py index E:\\")
    return root


# --------------------------------------------------------------------------- helpers
def kind_of(name, ext):
    low = name.lower()
    if low in JUNK_NAMES or low.startswith("._"):
        return "junk"
    if ext == "insv":
        return "360video"
    if ext == "insp":
        return "360photo"
    if ext == "lrv" and INSTA_RE.match(name):
        return "360proxy"
    if ext in VIDEO_EXT:
        return "video"
    if ext in RAW_EXT:
        return "raw"
    if ext in PHOTO_EXT:
        return "photo"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in SIDECAR_EXT:
        return "sidecar"
    if ext in PROJECT_EXT:
        return "project"
    return "other"


def insta_key(name):
    m = INSTA_RE.match(name)
    if not m:
        return None
    return f"{m['d']}_{m['t']}_{m['seq']}"


MIN_YEAR = 2000


def valid_date(d):
    return d is not None and MIN_YEAR <= d.year and d <= dt.datetime.now() + dt.timedelta(days=2)


FN_PATTERNS = [
    # 20240828_143012 / 20240828143012 / PXL_20240828_143012345 / DJI_20240828143012_0001
    (re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_\-T ]?(\d{2})(\d{2})(\d{2})\d{0,3}(?!\d)"), "datetime"),
    # 2024-08-28 14.30.12 / Screenshot 2024-08-28 at 2.30.12 PM
    (re.compile(r"(20\d{2})[-_.](\d{2})[-_.](\d{2})[ _T\-]+(?:at )?(\d{1,2})[.\-_:](\d{2})[.\-_:](\d{2})\s*([AP]M)?", re.I), "datetime"),
    # IMG-20240828-WA0001 / 2024-08-28
    (re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)"), "date"),
]


def date_from_filename(name):
    for rx, kind in FN_PATTERNS:
        m = rx.search(name)
        if not m:
            continue
        g = m.groups()
        try:
            if kind == "datetime":
                h = int(g[3])
                if len(g) > 6 and g[6]:
                    h = h % 12 + (12 if g[6].upper() == "PM" else 0)
                d = dt.datetime(int(g[0]), int(g[1]), int(g[2]), h, int(g[4]), int(g[5]))
            else:
                d = dt.datetime(int(g[0]), int(g[1]), int(g[2]))
        except ValueError:
            continue
        if valid_date(d):
            return d, f"filename_{kind}"
    return None, None


def parse_exif_date(s):
    if not s:
        return None
    s = str(s).strip().replace("\x00", "")
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    try:
        d = dt.datetime(*map(int, m.groups()))
    except ValueError:
        return None
    return d if valid_date(d) else None


def parse_utc_to_local(s):
    """ISO creation_time from video containers is UTC; convert to this PC's local time."""
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        d = d.astimezone().replace(tzinfo=None)
    except ValueError:
        return None
    return d if valid_date(d) else None


def fingerprint(path, size):
    """Fast fingerprint: size + first 1 MB + last 1 MB. Good for spotting duplicates on a slow HDD."""
    h = new_hasher()
    h.update(str(size).encode())
    chunk = 1 << 20
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > 2 * chunk:
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    return h.hexdigest()


# --------------------------------------------------------------------------- metadata readers
def find_tool(name):
    """Prefer the copy bundled in the tools folder, then whatever is on PATH."""
    for cand in (TOOLS_DIR / f"{name}.exe", TOOLS_DIR / name):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


FFPROBE = find_tool("ffprobe")
EXIFTOOL_BIN = find_tool("exiftool")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ffprobe_info(path):
    if not FFPROBE:
        return {}
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_entries", "format=duration:format_tags=creation_time:stream=codec_type,codec_name,width,height",
           path]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        j = json.loads(out.stdout or b"{}")
    except Exception:
        return {}
    info = {}
    fmt = j.get("format", {})
    try:
        info["duration"] = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    info["creation_time"] = (fmt.get("tags") or {}).get("creation_time")
    for s in j.get("streams", []):
        if s.get("codec_type") == "video" and s.get("width"):
            info.setdefault("width", s.get("width"))
            info.setdefault("height", s.get("height"))
            info.setdefault("codec", s.get("codec_name"))
    return info


class ExifTool:
    """One persistent exiftool process (-stay_open) so thousands of RAW/HEIC files don't each pay startup cost."""

    def __init__(self, exe):
        self.lock = threading.Lock()
        self.p = subprocess.Popen([exe, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)

    def read(self, path):
        with self.lock:
            args = ["-json", "-fast2", "-charset", "filename=utf8", "-api", "QuickTimeUTC",
                    "-DateTimeOriginal", "-CreateDate", "-ImageWidth", "-ImageHeight", path, "-execute"]
            self.p.stdin.write(("\n".join(args) + "\n").encode("utf-8"))
            self.p.stdin.flush()
            buf = b""
            while not buf.rstrip().endswith(b"{ready}"):
                line = self.p.stdout.readline()
                if not line:
                    break
                buf += line
        text = buf.decode("utf-8", "replace").rsplit("{ready}", 1)[0].strip()
        try:
            return json.loads(text)[0] if text else {}
        except Exception:
            return {}

    def close(self):
        try:
            self.p.stdin.write(b"-stay_open\nFalse\n")
            self.p.stdin.flush()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


EXIFTOOL = None


def photo_info(path, ext):
    info = {}
    if HAVE_PIL and (ext not in RAW_EXT) and (ext not in {"heic", "heif"} or HAVE_HEIF):
        try:
            with Image.open(path) as im:
                info["width"], info["height"] = im.size
                ex = im.getexif()
                d = parse_exif_date(ex.get_ifd(0x8769).get(36867)) or parse_exif_date(ex.get(306))
                if d:
                    info["date"] = d
                    return info
        except Exception:
            pass
    if EXIFTOOL is not None:
        j = EXIFTOOL.read(path)
        d = parse_exif_date(j.get("DateTimeOriginal")) or parse_exif_date(j.get("CreateDate"))
        if d:
            info["date"] = d
        info.setdefault("width", j.get("ImageWidth"))
        info.setdefault("height", j.get("ImageHeight"))
    return info


def analyze_file(root, rel, size, mtime):
    path = os.path.join(root, rel)
    name = os.path.basename(rel)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    kind = kind_of(name, ext)
    rec = {"rel": rel, "name": name, "ext": ext, "kind": kind, "size": size, "mtime": mtime,
           "date_taken": None, "date_source": None, "date_conf": None, "duration": None,
           "width": None, "height": None, "codec": None, "fp": None, "insta_key": insta_key(name),
           "error": None, "indexed_at": dt.datetime.now().isoformat(timespec="seconds")}
    if kind == "junk":
        return rec
    try:
        d, src, conf = None, None, None
        if kind in {"photo", "raw", "360photo"}:
            pi = photo_info(path, ext if ext != "insp" else "jpg")
            rec["width"], rec["height"] = pi.get("width"), pi.get("height")
            if pi.get("date"):
                d, src, conf = pi["date"], "exif", "high"
        elif kind in {"video", "360video"}:
            vi = ffprobe_info(path)
            rec["duration"], rec["width"], rec["height"], rec["codec"] = (
                vi.get("duration"), vi.get("width"), vi.get("height"), vi.get("codec"))
            ct = parse_utc_to_local(vi.get("creation_time"))
            fd, fsrc = date_from_filename(name)
            if fd and fsrc == "filename_datetime":       # camera filenames are local time: most trustworthy
                d, src, conf = fd, fsrc, "high"
            elif ct:
                d, src, conf = ct, "video_metadata", "high"
        if d is None and kind in MEDIA_KINDS | {"360proxy", "sidecar", "audio"}:
            fd, fsrc = date_from_filename(name)
            if fd:
                d, src, conf = fd, fsrc, "high" if fsrc == "filename_datetime" else "medium"
        if d is None and kind in MEDIA_KINDS | {"360proxy", "audio"}:
            md = dt.datetime.fromtimestamp(mtime)
            if valid_date(md):
                d, src, conf = md, "file_modified", "low"
        if d:
            rec["date_taken"], rec["date_source"], rec["date_conf"] = d.isoformat(timespec="seconds"), src, conf
        if kind != "other" and size > 0:
            rec["fp"] = fingerprint(path, size)
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    return rec


# --------------------------------------------------------------------------- INDEX
def cmd_index(args, cfg):
    global EXIFTOOL
    con = open_db(args.db)
    root = get_root(con, args.root)
    if not os.path.isdir(root):
        sys.exit(f"Can't find {root}. Is the drive plugged in?")
    print(f"MediaOrg {VERSION} - indexing {root}")
    print(f"  ffprobe: {'yes' if FFPROBE else 'NOT FOUND (videos will lack dates/duration)'}   "
          f"exiftool: {'yes' if EXIFTOOL_BIN else 'no (optional, helps RAW/HEIC)'}   "
          f"HEIC support: {'yes' if HAVE_HEIF or EXIFTOOL_BIN else 'no'}")
    if EXIFTOOL_BIN:
        EXIFTOOL = ExifTool(EXIFTOOL_BIN)

    known = {r["rel"]: (r["size"], r["mtime"]) for r in con.execute("SELECT rel,size,mtime FROM files")}
    seen = set()
    todo = []
    t0 = time.time()
    n_walk = 0
    set_progress(phase="walking", done=0, total=0, bytes=0, rate=0.0, eta=None, current="", started=time.time())
    for dirpath, dirnames, filenames in os.walk(root):
        if STOP.is_set():
            break
        set_progress(done=n_walk, current=os.path.relpath(dirpath, root))
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith("$")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                st = os.stat(full)
            except OSError:
                continue
            seen.add(rel)
            n_walk += 1
            if known.get(rel) == (st.st_size, st.st_mtime):
                continue
            todo.append((rel, st.st_size, st.st_mtime))
        if n_walk and n_walk % 5000 < len(filenames):
            print(f"  scanned {n_walk:,} files...", flush=True)
    if STOP.is_set():
        if EXIFTOOL:
            EXIFTOOL.close()
        set_progress(phase="stopped")
        print("  Stopped while listing files. Nothing lost - scan again any time.")
        return "stopped"
    print(f"  {n_walk:,} files on drive: {len(todo):,} new/changed, {len(seen) - len(todo):,} already indexed")

    cols = ["rel", "name", "ext", "kind", "size", "mtime", "date_taken", "date_source", "date_conf",
            "duration", "width", "height", "codec", "fp", "insta_key", "error", "indexed_at"]
    sql = f"INSERT OR REPLACE INTO files({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    done, bytes_done = 0, 0
    t1 = time.time()
    stopped = False
    set_progress(phase="indexing", done=0, total=len(todo), bytes=0, rate=0.0, eta=None, started=t1)
    try:
        with ThreadPoolExecutor(max_workers=args.workers or cfg["workers"]) as pool:
            for i in range(0, len(todo), 64):
                if STOP.is_set():
                    stopped = True
                    break
                for rec in pool.map(lambda x: analyze_file(root, *x), todo[i:i + 64]):
                    con.execute(sql, [rec[c] for c in cols])
                    done += 1
                    bytes_done += rec["size"] or 0
                    PROGRESS["current"] = rec["rel"]
                con.commit()
                rate = done / max(time.time() - t1, 0.001)
                eta = (len(todo) - done) / max(rate, 0.001)
                set_progress(done=done, bytes=bytes_done, rate=rate, eta=eta)
                if done % 256 == 0:
                    print(f"  indexed {done:,}/{len(todo):,}  ({rate:.0f} files/s, ~{eta / 60:.0f} min left)", flush=True)
    except KeyboardInterrupt:
        stopped = True
    finally:
        con.commit()
        if EXIFTOOL:
            EXIFTOOL.close()

    if stopped:
        set_progress(phase="stopped")
        print(f"  Stopped after {done:,} files. Progress is saved - scan again to continue where it left off.")
        return "stopped"
    gone = [r for r in known if r not in seen]
    if gone and done == len(todo):
        con.executemany("DELETE FROM files WHERE rel=?", [(g,) for g in gone])
        print(f"  removed {len(gone):,} entries for files no longer on the drive")
    meta_set(con, "last_index", dt.datetime.now().isoformat(timespec="seconds"))
    meta_set(con, "last_index_seconds", round(time.time() - t0, 1))
    con.commit()
    set_progress(phase="indexed", done=done, total=len(todo))
    print(f"  done in {(time.time() - t0) / 60:.1f} min")
    return "ok"


# --------------------------------------------------------------------------- PLAN
def safe_name(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip().rstrip(".")
    return s or "_"


def is_generic(folder, cfg, _cache={}):
    key = folder.lower().strip()
    if key in _cache:
        return _cache[key]
    names = set(n.lower() for n in cfg["generic_folder_names"])
    pats = [re.compile(p, re.I) for p in cfg["generic_folder_patterns"]]
    res = key in names or any(p.match(key) for p in pats)
    _cache[key] = res
    return res


def cmd_plan(args, cfg):
    con = open_db(args.db)
    root = get_root(con, args.root)
    org_name = cfg["organized_folder_name"]
    insta_mode = args.insta360 or cfg["insta360_mode"]
    allow_low = args.allow_mtime or cfg["move_low_confidence_dates"]
    rows = [dict(r) for r in con.execute("SELECT * FROM files")]
    if not rows:
        sys.exit("Nothing indexed yet. Run index first.")

    settings = {"insta360_mode": insta_mode, "allow_mtime": allow_low, "organized": org_name}
    cur = con.execute("INSERT INTO plans(created, settings) VALUES(?,?)",
                      (dt.datetime.now().isoformat(timespec="seconds"), json.dumps(settings)))
    plan_id = cur.lastrowid
    ops, review = [], []
    planned_dst = set()
    dec = {r["rel"]: (r["action"], r["value"]) for r in con.execute("SELECT * FROM decisions")}

    def dec_date(rel):
        a = dec.get(rel)
        if a and a[0] == "date":
            try:
                return dt.datetime.fromisoformat(a[1])
            except (TypeError, ValueError):
                return None
        return None

    def free_dir(path):
        """For a 'rename on clash' decision: first 'name (2)', 'name (3)'... that is free."""
        n = 2
        while os.path.exists(os.path.join(root, f"{path} ({n})")) or f"{path} ({n})".lower() in planned_dst:
            n += 1
        return f"{path} ({n})"

    def add_review(rel, reason, detail=""):
        review.append((plan_id, rel, reason, detail))

    titles = {}                       # day -> confident AI event name from analyze.py
    if cfg.get("use_ai_titles", True):
        for r in con.execute("SELECT day, title, confidence FROM events"):
            if r["title"] and (r["confidence"] or 0) >= cfg.get("ai_title_min_confidence", 0.6):
                titles[r["day"]] = r["title"]

    def day_dir(d):
        """'2024-08-28', or '2024-08-28 Beach day' - unless the plain-date folder already exists (keep the day together)."""
        plain = f"{d:%Y-%m-%d}"
        t = titles.get(plain)
        if not t or os.path.exists(os.path.join(root, org_name, f"{d:%Y}", plain)):
            return plain
        return f"{plain} {safe_name(t)}"

    def dt_of(r):
        return dt.datetime.fromisoformat(r["date_taken"]) if r["date_taken"] else None

    units, loose = defaultdict(list), []
    for r in rows:
        parts = Path(r["rel"]).parts
        folders = parts[:-1]
        if folders and folders[0] == org_name:
            continue  # already organized
        anchor = None
        for i, f in enumerate(folders):
            if not is_generic(f, cfg):
                anchor = os.path.join(*folders[: i + 1])
                break
        if anchor:
            units[anchor].append(r)
        else:
            loose.append(r)

    # ---- named folders: move the whole folder as one unit (one instant rename)
    for anchor, files in sorted(units.items()):
        name = Path(anchor).name
        key = anchor + os.sep
        if any(f["kind"] == "project" for f in files) and dec.get(key, ("",))[0] != "move":
            add_review(anchor + os.sep, "editing project inside",
                       "Folder has an editing project file; moving it would break the project's media links. Left in place.")
            continue
        good = [dt_of(f) for f in files if f["kind"] in MEDIA_KINDS and f["date_conf"] in ("high", "medium")]
        low = [dt_of(f) for f in files if f["kind"] in MEDIA_KINDS and f["date_conf"] == "low"]
        dates = ([dec_date(key)] if dec_date(key) else None) or good or (low if allow_low else [])
        if not dates:
            if not any(f["kind"] in MEDIA_KINDS for f in files):
                add_review(anchor + os.sep, "no media in folder", "Left in place.")
            else:
                add_review(anchor + os.sep, "folder has no reliable dates",
                           "Only file-modified dates found. Rename the folder with a date (e.g. 2024-08-28 Name) or allow mtime.")
            continue
        start, end = min(dates), max(dates)
        folder_name = safe_name(name) if re.match(r"^(19|20)\d{2}", name) else f"{start:%Y-%m-%d}_{safe_name(name)}"
        dst = os.path.join(org_name, f"{start:%Y}", folder_name)
        note = f"{len(files)} files, {start:%Y-%m-%d} to {end:%Y-%m-%d}"
        if (end - start).days > cfg["wide_span_days"]:
            note += f"  (WIDE SPAN {(end - start).days} days - check)"
        if os.path.exists(os.path.join(root, dst)) or dst.lower() in planned_dst:
            if dec.get(key, ("",))[0] != "rename":
                add_review(anchor + os.sep, "destination folder already exists", dst)
                continue
            dst = free_dir(dst)
        planned_dst.add(dst.lower())
        ops.append(("dir", anchor, dst, "unit:" + anchor, note))

    # ---- loose files: group Insta360 sets and sidecars, then date-sort
    groups = defaultdict(list)
    for r in loose:
        if r["kind"] == "junk":
            continue
        folder = os.path.dirname(r["rel"])
        if r["insta_key"]:
            groups[("insta", folder, r["insta_key"])].append(r)
        else:
            stem = r["name"].rsplit(".", 1)[0].lower()
            if re.fullmatch(r"g[lxh]\d{6}", stem):  # GoPro: GL010123.LRV belongs to GX010123.MP4
                stem = "gx" + stem[2:]
            groups[("stem", folder, stem)].append(r)

    for key, files in sorted(groups.items(), key=lambda kv: kv[0]):
        kinds = {f["kind"] for f in files}
        media = [f for f in files if f["kind"] in MEDIA_KINDS]
        is360 = key[0] == "insta" or bool(kinds & {"360video", "360photo", "360proxy"})
        if not media and not (is360 and kinds & {"360proxy"}):
            for f in files:
                if f["kind"] in {"other", "project", "audio"}:
                    add_review(f["rel"], "non-media file left in place", f["kind"])
            continue
        lead = media or files
        if any(f["error"] for f in files):
            add_review(lead[0]["rel"], "read error", next(f["error"] for f in files if f["error"]))
            continue
        confs = [f for f in lead if f["date_conf"] in ("high", "medium")]
        pick = confs or ([f for f in lead if f["date_conf"] == "low"] if allow_low else [])
        if dec_date(lead[0]["rel"]):
            pick = [dict(lead[0], date_taken=dec_date(lead[0]["rel"]).isoformat())]
        if not pick:
            add_review(lead[0]["rel"], "only file-modified date",
                       "No date in metadata or filename" + (f" (+{len(files) - 1} linked files)" if len(files) > 1 else ""))
            continue
        d = min(dt_of(f) for f in pick)
        if is360:
            base = (os.path.join(org_name, f"{d:%Y}", day_dir(d), "360") if insta_mode == "together"
                    else os.path.join(org_name, "360", f"{d:%Y}", day_dir(d)))
        else:
            base = os.path.join(org_name, f"{d:%Y}", day_dir(d))
        moves = [(f["rel"], os.path.join(base, f["name"])) for f in files]
        clash = [dst for _, dst in moves if dst.lower() in planned_dst or os.path.exists(os.path.join(root, dst))]
        if clash and dec.get(lead[0]["rel"], ("",))[0] == "rename":
            base = free_dir(base)          # same file names, sibling folder "date (2)"
            moves = [(f["rel"], os.path.join(base, f["name"])) for f in files]
            clash = [dst for _, dst in moves if dst.lower() in planned_dst or os.path.exists(os.path.join(root, dst))]
        if clash:
            add_review(lead[0]["rel"], "name clash at destination", clash[0])
            continue
        grp = f"{key[0]}:{key[1]}:{key[2]}"
        for src, dst in moves:
            planned_dst.add(dst.lower())
            ops.append(("file", src, dst, grp, ""))

    con.executemany("INSERT INTO plan_ops VALUES(?,?,?,?,?,?,?)",
                    [(plan_id, i, *op) for i, op in enumerate(ops)])
    con.executemany("INSERT INTO review VALUES(?,?,?,?)", review)
    con.commit()

    REPORT_DIR.mkdir(exist_ok=True)
    with open(REPORT_DIR / "plan.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["type", "from", "to", "group", "note"])
        w.writerows(ops)
    with open(REPORT_DIR / "review.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["path", "reason", "detail"])
        w.writerows([r[1:] for r in review])
    n_dir = sum(1 for o in ops if o[0] == "dir")
    n_file = len(ops) - n_dir
    print(f"Plan #{plan_id}: {n_dir:,} named folders + {n_file:,} loose files to move; "
          f"{len(review):,} items set aside for review.")
    print(f"  Inspect: {REPORT_DIR / 'plan.csv'}  and  {REPORT_DIR / 'review.csv'}")
    print(f"  Nothing has moved. Run 'apply' when it looks right.")
    return plan_id


# --------------------------------------------------------------------------- APPLY / UNDO
def rebase_paths(con, src, dst, is_dir):
    if is_dir:
        p = src + os.sep
        con.execute("UPDATE files SET rel = ? || substr(rel, ?) WHERE substr(rel, 1, ?) = ?",
                    (dst + os.sep, len(p) + 1, len(p), p))
    else:
        con.execute("UPDATE files SET rel=?, name=? WHERE rel=?", (dst, os.path.basename(dst), src))


def prune_empty(root, start_rel, stop_rels):
    """Remove folders left completely empty after a move (never deletes any file)."""
    d = os.path.dirname(os.path.join(root, start_rel))
    root_n = os.path.normcase(os.path.abspath(root))
    while os.path.normcase(os.path.abspath(d)) != root_n:
        try:
            if os.listdir(d):
                return
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def cmd_apply(args, cfg):
    con = open_db(args.db)
    root = get_root(con, args.root)
    plan = con.execute("SELECT * FROM plans ORDER BY plan_id DESC LIMIT 1").fetchone()
    if not plan:
        sys.exit("No plan yet. Run plan first.")
    if plan["applied"]:
        sys.exit(f"Plan #{plan['plan_id']} was already applied. Run plan again for a fresh one.")
    ops = con.execute("SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan["plan_id"],)).fetchall()
    if not ops:
        sys.exit("Plan has nothing to move.")
    if not args.yes:
        a = input(f"Move {len(ops):,} items on {root} (plan #{plan['plan_id']})? Type YES: ")
        if a.strip() != "YES":
            sys.exit("Cancelled.")

    batch = (con.execute("SELECT COALESCE(MAX(batch),0) FROM moves").fetchone()[0] or 0) + 1
    log_dir = os.path.join(root, cfg["organized_folder_name"], "_mediaorg_logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"moves_batch{batch}_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
    logf = open(log_path, "w", newline="", encoding="utf-8-sig")
    log = csv.writer(logf)
    log.writerow(["type", "from", "to"])

    by_grp = defaultdict(list)
    order = []
    for o in ops:
        if o["grp"] not in by_grp:
            order.append(o["grp"])
        by_grp[o["grp"]].append(o)

    moved = failed = 0
    seq = 0
    t0 = time.time()
    set_progress(phase="applying", done=0, total=len(order), bytes=0, rate=0.0, eta=None, current="", started=t0)
    for gi, g in enumerate(order):
        if STOP.is_set():
            print("  Stopped. Moves done so far are logged and can be undone.")
            break
        set_progress(done=gi, current=by_grp[g][0]["src"])
        done_here = []
        try:
            for o in by_grp[g]:
                s, d = os.path.join(root, o["src"]), os.path.join(root, o["dst"])
                if not os.path.exists(s):
                    raise FileNotFoundError(f"source missing: {o['src']}")
                if os.path.exists(d):
                    raise FileExistsError(f"destination exists: {o['dst']}")
                os.makedirs(os.path.dirname(d), exist_ok=True)
                os.rename(s, d)  # same drive -> instant, no data copied
                done_here.append(o)
        except Exception as e:
            for o in reversed(done_here):  # keep groups together: roll back partial group
                try:
                    os.rename(os.path.join(root, o["dst"]), os.path.join(root, o["src"]))
                except OSError:
                    pass
            con.execute("INSERT INTO review VALUES(?,?,?,?)",
                        (plan["plan_id"], by_grp[g][0]["src"], "move failed - skipped", f"{type(e).__name__}: {e}"))
            failed += 1
            continue
        for o in done_here:
            seq += 1
            con.execute("INSERT INTO moves(batch,seq,op,src,dst,ts) VALUES(?,?,?,?,?,?)",
                        (batch, seq, o["op"], o["src"], o["dst"], dt.datetime.now().isoformat(timespec="seconds")))
            log.writerow([o["op"], o["src"], o["dst"]])
            rebase_paths(con, o["src"], o["dst"], o["op"] == "dir")
            moved += 1
            if args.prune_empty:
                prune_empty(root, o["src"], [])
        logf.flush()
        if seq % 200 == 0:
            con.commit()
    con.execute("UPDATE plans SET applied=1 WHERE plan_id=?", (plan["plan_id"],))
    con.commit()
    logf.close()
    set_progress(phase="applied", done=len(order))
    print(f"Batch {batch}: moved {moved:,} items in {time.time() - t0:.0f}s, {failed:,} groups skipped (see review).")
    print(f"  Undo log on the drive: {log_path}")
    print(f"  To reverse:  mediaorg.py undo")


def cmd_undo(args, cfg):
    con = open_db(args.db)
    root = get_root(con, args.root)
    b = con.execute("SELECT MAX(batch) FROM moves WHERE undone=0").fetchone()[0]
    if not b:
        sys.exit("Nothing to undo.")
    rows = con.execute("SELECT * FROM moves WHERE batch=? AND undone=0 ORDER BY seq DESC", (b,)).fetchall()
    if not args.yes and input(f"Undo batch {b} ({len(rows):,} moves)? Type YES: ").strip() != "YES":
        sys.exit("Cancelled.")
    ok = bad = 0
    for r in rows:
        s, d = os.path.join(root, r["dst"]), os.path.join(root, r["src"])
        try:
            if os.path.exists(d):
                raise FileExistsError(r["src"])
            os.makedirs(os.path.dirname(d), exist_ok=True)
            os.rename(s, d)
            rebase_paths(con, r["dst"], r["src"], r["op"] == "dir")
            con.execute("UPDATE moves SET undone=1 WHERE batch=? AND seq=?", (b, r["seq"]))
            prune_empty(root, r["dst"], [])  # tidy date folders the undo emptied (empty folders only)
            ok += 1
        except Exception as e:
            bad += 1
            print(f"  ! could not restore {r['src']}: {e}")
    con.commit()
    print(f"Undo batch {b}: restored {ok:,}, failed {bad:,}.")


# --------------------------------------------------------------------------- REPORT
def human(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def cmd_report(args, cfg):
    con = open_db(args.db)
    root = get_root(con, args.root)
    REPORT_DIR.mkdir(exist_ok=True)
    rows = [dict(r) for r in con.execute("SELECT * FROM files")]
    plan = con.execute("SELECT * FROM plans ORDER BY plan_id DESC LIMIT 1").fetchone()
    pid = plan["plan_id"] if plan else -1
    review = [dict(r) for r in con.execute("SELECT * FROM review WHERE plan_id=?", (pid,))]
    ops = con.execute("SELECT op, COUNT(*) FROM plan_ops WHERE plan_id=? GROUP BY op", (pid,)).fetchall()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")

    kinds = Counter(r["kind"] for r in rows)
    size_by_kind = defaultdict(int)
    for r in rows:
        size_by_kind[r["kind"]] += r["size"] or 0
    srcs = Counter(r["date_source"] or "none" for r in rows if r["kind"] in MEDIA_KINDS)
    years = Counter(r["date_taken"][:4] for r in rows if r["date_taken"] and r["kind"] in MEDIA_KINDS)
    errors = [r for r in rows if r["error"]]
    insta_sets = defaultdict(list)
    for r in rows:
        if r["insta_key"]:
            insta_sets[(os.path.dirname(r["rel"]), r["insta_key"])].append(r)
    lonely = [k for k, v in insta_sets.items()
              if any(re.search(r"_00_\d{3}\.insv$", f["name"], re.I) for f in v)
              and not any(re.search(r"_10_\d{3}\.insv$", f["name"], re.I) for f in v)]

    # duplicates (fast fingerprint)
    by_fp = defaultdict(list)
    for r in rows:
        if r["fp"] and r["kind"] in MEDIA_KINDS:
            by_fp[r["fp"]].append(r)
    dups = [v for v in by_fp.values() if len(v) > 1]
    dup_waste = sum((len(v) - 1) * (v[0]["size"] or 0) for v in dups)
    with open(REPORT_DIR / "duplicates.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["set", "size", "path"])
        for i, v in enumerate(sorted(dups, key=lambda v: -(v[0]["size"] or 0)), 1):
            for r in v:
                w.writerow([i, r["size"], os.path.join(root, r["rel"])])

    # edit queue: every video sorted by capture time, split into shoot sessions
    vids = sorted((r for r in rows if r["kind"] in {"video", "360video"} and r["date_taken"]),
                  key=lambda r: r["date_taken"])
    gap = dt.timedelta(hours=cfg["session_gap_hours"])
    sessions, last, sid = [], None, 0
    with open(REPORT_DIR / "edit_queue.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["session", "date", "time", "kind", "duration_s", "resolution", "date_source", "path"])
        for r in vids:
            d = dt.datetime.fromisoformat(r["date_taken"])
            if last is None or d - last > gap:
                sid += 1
                sessions.append([sid, d, d, 0, 0.0, Counter()])
            s = sessions[-1]
            s[2] = d
            s[3] += 1
            s[4] += r["duration"] or 0
            s[5][os.path.dirname(r["rel"])] += 1
            last = d
            res = f"{r['width']}x{r['height']}" if r["width"] else ""
            w.writerow([sid, f"{d:%Y-%m-%d}", f"{d:%H:%M:%S}", r["kind"],
                        round(r["duration"] or 0, 1), res, r["date_source"], os.path.join(root, r["rel"])])

    reasons = Counter(r["reason"] for r in review)
    unknown_ext = Counter(r["ext"] or "(none)" for r in rows if r["kind"] == "other")
    mtime_only = [r for r in rows if r["date_source"] == "file_modified"]
    shape = lambda n: re.sub(r"\d", "#", n.rsplit(".", 1)[0])[:30]
    mtime_patterns = Counter(shape(r["name"]) for r in mtime_only)
    anchors = Counter()
    for r in rows:
        folders = Path(r["rel"]).parts[:-1]
        for f in folders:
            if f == cfg["organized_folder_name"]:
                break
            if not is_generic(f, cfg):
                anchors[f] += 1
                break
    suspicious_anchors = [a for a in anchors if re.fullmatch(r"[\w\- ]{1,12}", a) and re.search(r"\d", a)]

    L = []
    L.append(f"# MediaOrg report - {dt.datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"Drive: `{root}`  |  indexed: {meta_get(con, 'last_index', '?')} "
             f"({float(meta_get(con, 'last_index_seconds', 0)) / 60:.1f} min)  |  MediaOrg {VERSION}\n")
    L.append("## What's on the drive")
    L.append("| Type | Files | Size |\n|---|---:|---:|")
    for k, n in kinds.most_common():
        L.append(f"| {k} | {n:,} | {human(size_by_kind[k])} |")
    L.append(f"\n**Total:** {len(rows):,} files, {human(sum(size_by_kind.values()))}\n")
    L.append("**Media by year:** " + ", ".join(f"{y}: {n:,}" for y, n in sorted(years.items())))
    L.append("\n**Where dates came from:** " + ", ".join(f"{k}: {n:,}" for k, n in srcs.most_common()))
    L.append(f"\n**Insta360:** {sum(1 for r in rows if r['kind'] == '360video'):,} .insv, "
             f"{sum(1 for r in rows if r['kind'] == '360photo'):,} .insp, "
             f"{sum(1 for r in rows if r['kind'] == '360proxy'):,} LRV proxies, in {len(insta_sets):,} capture sets. "
             f"Sets missing their second-lens file: {len(lonely):,} (fine for X3/X4 single-file captures).")
    L.append(f"\n**Likely duplicates:** {len(dups):,} sets, ~{human(dup_waste)} reclaimable "
             f"(fast fingerprint - verify before deleting; see duplicates.csv). Nothing is ever deleted by MediaOrg.")

    L.append("\n## Plan")
    if plan:
        c = dict((o[0], o[1]) for o in ops)
        L.append(f"Plan #{pid} ({'APPLIED' if plan['applied'] else 'not applied yet'}): "
                 f"{c.get('dir', 0):,} named folders moved whole, {c.get('file', 0):,} loose files date-sorted. "
                 f"Settings: `{plan['settings']}`")
    else:
        L.append("No plan yet.")
    L.append("\n## Set aside for review (skipped, nothing waits on these)")
    if reasons:
        for k, n in reasons.most_common():
            L.append(f"- **{k}**: {n:,}")
            for r in [x for x in review if x["reason"] == k][:5]:
                L.append(f"  - `{r['rel']}` {r['detail'][:120]}")
        L.append("\nFull list: review.csv")
    else:
        L.append("Nothing.")

    L.append("\n## Edit queue - shoot sessions")
    L.append(f"{len(vids):,} videos in {len(sessions):,} sessions (new session after a {cfg['session_gap_hours']}h gap). "
             f"Full list: edit_queue.csv\n")
    L.append("| # | Start | End | Clips | Footage | Main folder |\n|---:|---|---|---:|---:|---|")
    for s in sessions[-40:]:
        L.append(f"| {s[0]} | {s[1]:%Y-%m-%d %H:%M} | {s[2]:%m-%d %H:%M} | {s[3]} | {s[4] / 60:.0f} min | "
                 f"{s[5].most_common(1)[0][0] or '(root)'} |")
    if len(sessions) > 40:
        L.append(f"\n(showing latest 40 of {len(sessions)})")

    # ---------------- self-improvement section
    L.append("\n---\n## Prompt for Claude (paste everything below into the chat)\n")
    L.append("```text")
    L.append(f"MediaOrg {VERSION} run summary for improvement. Please suggest code/config changes.")
    L.append(f"- files: {len(rows):,}; media date sources: {dict(srcs)}")
    L.append(f"- index time: {meta_get(con, 'last_index_seconds', '?')} s")
    L.append(f"- read errors: {len(errors):,}" + (f"; examples: {[e['error'][:80] for e in errors[:3]]}" if errors else ""))
    L.append(f"- review reasons: {dict(reasons)}")
    if mtime_only:
        L.append(f"- {len(mtime_only):,} media files only had file-modified dates. Most common name shapes "
                 f"(# = digit), may need a filename date pattern or metadata reader:")
        for p, n in mtime_patterns.most_common(12):
            L.append(f"    {n:>6,}  {p}  e.g. {next(r['name'] for r in mtime_only if shape(r['name']) == p)}")
    if unknown_ext:
        L.append(f"- unrecognized extensions (count): {dict(unknown_ext.most_common(15))}")
    if suspicious_anchors:
        L.append(f"- folders treated as NAMED that might be generic camera folders: {suspicious_anchors[:20]}")
    L.append(f"- top named folders (files): {dict(anchors.most_common(15))}")
    L.append(f"- Insta360 sets missing _10_ lens file: {len(lonely)}; examples: {[k[1] for k in lonely[:5]]}")
    L.append(f"- wide-span named folders: {sum(1 for r in con.execute('SELECT note FROM plan_ops WHERE plan_id=?', (pid,)) if 'WIDE SPAN' in (r[0] or ''))}")
    L.append("- My notes / corrections: <write anything that looked wrong here>")
    L.append("```")
    out = REPORT_DIR / f"report_{stamp}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    # plain notepad copy too
    (REPORT_DIR / "LATEST_REPORT.txt").write_text("\n".join(L), encoding="utf-8")
    print(f"Report: {out}\n  also: {REPORT_DIR / 'LATEST_REPORT.txt'}, edit_queue.csv, duplicates.csv, review.csv")


# --------------------------------------------------------------------------- MENU (double-click)
def menu(cfg):
    drive_file = DATA_DIR / "drive.txt"
    db = str(DATA_DIR / "mediaorg.db")
    while True:
        drive = drive_file.read_text(encoding="utf-8").strip() if drive_file.exists() else ""
        print("\n" + "=" * 58)
        print(f"  MediaOrg {VERSION}  -  organize a media drive in place")
        print(f"  Drive: {drive or '(not set)'}      Data: {APP_DIR}")
        print("=" * 58)
        print("  1. Set drive / folder to organize")
        print("  2. FULL SCAN  (index + plan + report, moves nothing)")
        print("  3. Make a new plan only (after editing settings)")
        print("  4. Open latest report in Notepad")
        print("  5. APPLY plan  (moves files on the drive)")
        print("  6. UNDO last apply")
        print("  7. Edit settings")
        print("  8. Open reports folder")
        print("  0. Exit")
        c = input("\nChoose: ").strip()
        ns = argparse.Namespace(db=db, root=None, workers=None, insta360=None, allow_mtime=False,
                                yes=False, prune_empty=True)
        try:
            if c == "1":
                d = input("Drive or folder (e.g. E:\\ ): ").strip().strip('"')
                if d:
                    drive_file.write_text(d, encoding="utf-8")
            elif c == "2":
                if not drive:
                    print("Set the drive first (option 1).")
                    continue
                ns.root = drive
                if cmd_index(ns, cfg) == "stopped":
                    continue
                ns.root = None
                cmd_plan(ns, cfg)
                cmd_report(ns, cfg)
            elif c == "3":
                cmd_plan(ns, cfg)
                cmd_report(ns, cfg)
            elif c == "4":
                open_path(REPORT_DIR / "LATEST_REPORT.txt")
            elif c == "5":
                cmd_apply(ns, cfg)
            elif c == "6":
                cmd_undo(ns, cfg)
            elif c == "7":
                open_path(CONFIG_PATH)
                print("Save the file, then use option 3 to rebuild the plan. (Restart MediaOrg to reload settings.)")
            elif c == "8":
                REPORT_DIR.mkdir(parents=True, exist_ok=True)
                open_path(REPORT_DIR)
            elif c == "0":
                return
        except SystemExit as e:          # commands exit with a message; keep the menu alive
            if e.code not in (None, 0):
                print(e.code)
        except KeyboardInterrupt:
            print("\nStopped.")


def open_path(p):
    p = Path(p)
    if not p.exists():
        print(f"Not there yet: {p}")
        return
    if os.name == "nt":
        if p.is_file() and p.suffix.lower() in (".txt", ".json", ".md"):
            subprocess.Popen(["notepad.exe", str(p)])
        else:
            os.startfile(str(p))
    else:
        print(p)


# --------------------------------------------------------------------------- CLI
def main():
    cfg = load_config()
    if len(sys.argv) == 1:
        menu(cfg)
        return
    ap = argparse.ArgumentParser(description="MediaOrg phase 1 - organize a media drive in place")
    ap.add_argument("--db", default=str(DATA_DIR / "mediaorg.db"), help="database file (keep it on the NVMe)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["index", "plan", "apply", "undo", "report", "run"]:
        p = sub.add_parser(name)
        p.add_argument("root", nargs="?", help="drive or folder, e.g. E:\\  (remembered after first use)")
        if name in ("index", "run"):
            p.add_argument("--workers", type=int, help="parallel readers (default 4; HDD likes 2-4)")
        if name in ("plan", "run"):
            p.add_argument("--insta360", choices=["together", "separate"], help="where 360 files go")
            p.add_argument("--allow-mtime", action="store_true", help="also move files dated only by modified time")
        if name in ("apply", "undo"):
            p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
        if name == "apply":
            p.add_argument("--prune-empty", action="store_true", help="remove source folders left empty")
    args = ap.parse_args()
    if args.cmd == "run":
        if cmd_index(args, cfg) == "stopped":
            return
        args.root = None
        cmd_plan(args, cfg)
        cmd_report(args, cfg)
    else:
        {"index": cmd_index, "plan": cmd_plan, "apply": cmd_apply, "undo": cmd_undo, "report": cmd_report}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
