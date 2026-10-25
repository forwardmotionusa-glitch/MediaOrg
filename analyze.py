#!/usr/bin/env python3
"""
Phase 2: local analysis of your videos. Nothing leaves this PC.

For each clip: a few keyframes (decoded on the GPU with NVDEC when ffmpeg supports it), an optional Whisper
transcript, and a description from a vision model served by Ollama. Clips from the same day are then given
one short event name, which the plan uses for the day's folder when it is confident enough.

Resumable: finished clips are stored in the `analysis` table and skipped next time. Never prompts; anything
it cannot do is recorded per clip and surfaced as a review item.
"""
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import mediaorg as mo

MAX_TRIES = 3
_hw_ok = [True]          # flips to False the first time GPU decoding fails, so we stop paying for the retry
_whisper = {}


class Unavailable(Exception):
    """Analysis can't run at all (no ffmpeg, Ollama not running, model missing)."""


def _log(*a):
    getattr(mo, "print", print)(*a)


# --------------------------------------------------------------------------- Ollama
class OllamaError(Exception):
    """Ollama answered but refused the request (HTTP 4xx/5xx). Not an outage: it's about this one request."""

    def __init__(self, code, message):
        super().__init__(f"Ollama said {code}: {message}")
        self.code = code
        self.message = str(message)


def _ollama(cfg, path, body=None, timeout=600):
    url = cfg["ollama_url"].rstrip("/") + path
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:           # (an HTTPError is also a URLError, so it must be told apart from "Ollama is down")
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except Exception:
            msg = e.reason
        raise OllamaError(e.code, msg) from None


def _options(cfg):
    # One fixed context size for every call: images + prompt need well over Ollama's 4096 default, and changing it
    # between calls makes Ollama reload the model.
    return {"temperature": 0.2, "num_ctx": int(cfg.get("vision_ctx", 16384))}


def _model_present(cfg, tags):
    want = cfg["vision_model"]
    names = {m.get("name", "") for m in tags.get("models", [])}
    return want in names or (":" not in want and want + ":latest" in names)


def find_whisper_cli(cfg=None):
    """(whisper-cli.exe, model) installed by components.py, or dropped by hand into the tools folder."""
    try:
        j = json.loads((mo.APP_DIR / "components" / "whisper" / "info.json").read_text(encoding="utf-8"))
        if os.path.isfile(j["cli"]) and os.path.isfile(j["model"]):
            return j["cli"], j["model"]
    except Exception:
        pass
    exe = mo.find_tool("whisper-cli")
    model = mo.TOOLS_DIR / "ggml-small.bin"
    return (exe, str(model)) if exe and model.is_file() else None


def have_faster_whisper():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def setup_status(cfg):
    """What's ready for an unattended run. Each item: key, label, ok, required, detail."""
    items = []
    ff = mo.find_tool("ffmpeg")
    items.append({"key": "ffmpeg", "label": "ffmpeg (frame extraction)", "ok": bool(ff), "required": True,
                  "detail": ff or "Not found. Put ffmpeg.exe in the tools folder next to MediaOrg.exe."})
    nv = False
    if ff:
        try:
            out = subprocess.run([ff, "-hide_banner", "-hwaccels"], capture_output=True, timeout=20,
                                 creationflags=mo.NO_WINDOW).stdout.decode(errors="ignore")
            nv = "cuda" in out
        except Exception:
            pass
    items.append({"key": "nvdec", "label": "GPU video decoding (NVDEC)", "ok": nv, "required": False,
                  "detail": "On" if nv else "Not available; frames will be decoded on the CPU (slower, still works)."})
    tags, err = None, ""
    try:
        tags = _ollama(cfg, "/api/tags", timeout=3)
    except Exception as e:
        err = str(getattr(e, "reason", e))
    items.append({"key": "ollama", "label": "Ollama (runs the vision model)", "ok": tags is not None, "required": True,
                  "detail": cfg["ollama_url"] if tags is not None else
                  f"Not reachable ({err}). Install it from ollama.com and start it, then check again."})
    has_model = tags is not None and _model_present(cfg, tags)
    items.append({"key": "model", "label": f"Vision model {cfg['vision_model']}", "ok": has_model, "required": True,
                  "detail": "Installed" if has_model else f"Not installed. Run once, before bed:  ollama pull {cfg['vision_model']}"})
    wh = have_faster_whisper() or bool(find_whisper_cli(cfg))
    items.append({"key": "whisper", "label": "Whisper (speech transcripts)", "ok": wh or not cfg.get("transcribe"),
                  "required": False,
                  "detail": ("Ready" if wh else "Turned off in Settings") if wh or not cfg.get("transcribe")
                  else "Not set up yet (downloaded automatically); until then clips are described from pictures only."})
    return {"items": items, "ready": all(i["ok"] for i in items if i["required"])}


# --------------------------------------------------------------------------- frames and audio
def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=mo.NO_WINDOW)


def want_gpu_decode(r):
    """GPU decode has a ~1 s start-up cost, so it only pays for big or HEVC/AV1 video; small H.264 is faster on the CPU."""
    return (r.get("width") or 0) >= 2560 or (r.get("codec") or "") in ("hevc", "av1", "vp9") and (r.get("size") or 0) > 100e6


def extract_frames(ffmpeg, src, duration, n, outdir, hw=False):
    """n evenly spaced JPEGs (768 px wide) from ONE ffmpeg run (one start-up, one open per frame). With hw, tries GPU
    decoding first and falls back to the CPU (and then stops trying the GPU for this session)."""
    times = [(duration or 0) * (i + 0.5) / n for i in range(n)] if duration else [0.0]
    outs = [os.path.join(outdir, f"f{i}.jpg") for i in range(len(times))]
    for use_hw in ((True, False) if hw and _hw_ok[0] else (False,)):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for t in times:
            cmd += (["-hwaccel", "cuda"] if use_hw else []) + ["-ss", f"{t:.2f}", "-i", src]
        for i, o in enumerate(outs):
            cmd += ["-map", f"{i}:v:0", "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "4", o]
        try:
            _run(cmd, 300)
        except Exception:
            pass
        got = [o for o in outs if os.path.isfile(o) and os.path.getsize(o) > 0]
        if got:
            return got
        if use_hw:
            _hw_ok[0] = False
    return []


def transcribe(cfg, ffmpeg, src, tmp):
    """First ~3 minutes of speech, or '' when no engine is installed / no speech."""
    if not cfg.get("transcribe"):
        return ""
    cli = None if have_faster_whisper() else find_whisper_cli(cfg)
    if not have_faster_whisper() and not cli:
        return ""
    wav = os.path.join(tmp, "a.wav")
    try:
        if _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", "180", "-vn", "-ac", "1",
                 "-ar", "16000", wav], 180).returncode != 0 or not os.path.isfile(wav):
            return ""
        if cli:
            r = subprocess.run([cli[0], "-m", cli[1], "-f", wav, "-nt", "-np", "-l", "auto"], capture_output=True,
                               timeout=600, creationflags=mo.NO_WINDOW, cwd=os.path.dirname(cli[0]))
            text = r.stdout.decode(errors="ignore")
            text = re.sub(r"\[BLANK_AUDIO\]|\[.*?\]|\(.*?\)", " ", text)       # non-speech markers
        else:
            from faster_whisper import WhisperModel
            m = _whisper.get("m")
            if m is None:
                try:
                    m = WhisperModel(cfg["whisper_model"], device="cuda", compute_type="float16")
                except Exception:
                    m = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8")
                _whisper["m"] = m
            segs, _ = m.transcribe(wav, vad_filter=True)
            text = " ".join(s.text.strip() for s in segs)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:600]


# --------------------------------------------------------------------------- vision model
PROMPT = ("These frames come from one video clip, in time order. In one or two plain sentences say what is "
          "happening (who or what, where, doing what). Then give 3 to 6 short lowercase tags. "
          'Reply only as JSON: {"description": "...", "tags": ["..."]}')


def _json_from(text):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.S)
        return json.loads(m.group(0)) if m else {}


PLAIN_PROMPT = "These frames come from one video clip, in time order. In one or two plain sentences, say what is happening."


def _chat_images(cfg, prompt, imgs, as_json):
    """One vision request. Too many pictures for the context window -> retry with half of them."""
    while True:
        body = {"model": cfg["vision_model"], "stream": False, "keep_alive": "30m", "options": _options(cfg),
                "messages": [{"role": "user", "content": prompt, "images": imgs}]}
        if as_json:
            body["format"] = "json"
        try:
            return _ollama(cfg, "/api/chat", body).get("message", {}).get("content", "") or ""
        except OllamaError as e:
            if e.code == 400 and "context" in e.message.lower() and len(imgs) > 1:
                imgs = imgs[:len(imgs) // 2]
                continue
            raise


def _description_from(raw):
    """(description, tags) from the model's JSON, tolerating other key names; ('', []) if there's nothing usable."""
    try:
        j = _json_from(raw)
    except Exception:
        j = {}
    if not isinstance(j, dict):
        j = {}
    desc = next((j[k].strip() for k in ("description", "caption", "summary", "scene", "text") if isinstance(j.get(k), str)
                 and j[k].strip()), "")
    if not desc:                                      # any other text field: take the longest
        strings = [v.strip() for k, v in j.items() if k != "tags" and isinstance(v, str) and v.strip()]
        desc = max(strings, key=len) if strings else ""
    tags = j.get("tags", [])
    tags = [str(t).strip().lower() for t in (tags if isinstance(tags, list) else str(tags).split(",")) if str(t).strip()][:6]
    return desc, tags


def describe(cfg, frames, transcript):
    prompt = PROMPT + (f"\nWhat people say in it (may be partial): {transcript}" if transcript else "")
    imgs = [base64.b64encode(open(f, "rb").read()).decode() for f in frames]
    raw = _chat_images(cfg, prompt, imgs, True)
    desc, tags = _description_from(raw)
    if not desc:                                      # JSON mode sometimes comes back empty: ask for plain text instead
        raw2 = _chat_images(cfg, PLAIN_PROMPT, imgs, False).strip()
        if raw2 and not raw2.startswith("{"):
            desc, tags = raw2, []
        else:
            raise ValueError(f"model returned no description (said: {(raw or raw2)[:80]!r})")
    return desc[:400], tags


def name_event(cfg, day, descs):
    prompt = (f"These are descriptions of video clips all filmed on {day}:\n- " + "\n- ".join(d[:200] for d in descs) +
              '\n\nGive ONE short name for this day, 2 to 5 words, suitable for a folder (no dates, no punctuation), '
              'and how confident you are that one name fits all the clips, from 0 to 1. If the clips are unrelated '
              'or unclear, use a low confidence. Reply only as JSON: {"title": "...", "summary": "...", "confidence": 0.0}')
    r = _ollama(cfg, "/api/chat", {"model": cfg["vision_model"], "stream": False, "format": "json", "keep_alive": "30m",
                                   "options": _options(cfg), "messages": [{"role": "user", "content": prompt}]})
    j = _json_from(r.get("message", {}).get("content", ""))
    title = re.sub(r"[^\w &'-]", "", str(j.get("title", ""))).strip()[:40]
    try:
        conf = max(0.0, min(1.0, float(j.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    return title, str(j.get("summary", ""))[:300], conf if title else 0.0


# --------------------------------------------------------------------------- the run
def _targets(con, cfg):
    kinds = ["video", "360video"] + (["photo"] if cfg.get("analyze_photos") else [])
    q = ",".join("?" * len(kinds))
    rows = con.execute(f"""SELECT f.rel, f.name, f.ext, f.kind, f.duration, f.insta_key, f.date_taken, f.width, f.height, f.codec, f.size
                           FROM files f LEFT JOIN analysis a ON a.rel = f.rel
                           WHERE f.kind IN ({q}) AND (a.rel IS NULL OR (a.status IN ('error', 'skipped') AND a.tries < ?))
                           ORDER BY f.date_taken""", (*kinds, MAX_TRIES)).fetchall()
    return [dict(r) for r in rows]


def _source_for(con, root, r):
    """The file to decode. Insta360: the small LRV proxy if there is one, otherwise the .insv itself."""
    if r["kind"] != "360video":
        return os.path.join(root, r["rel"])
    folder = os.path.dirname(r["rel"])
    for p in con.execute("SELECT rel FROM files WHERE insta_key=? AND ext='lrv'", (r["insta_key"],)):
        if os.path.dirname(p[0]) == folder:
            return os.path.join(root, p[0])
    return os.path.join(root, r["rel"])           # no proxy: try the .insv itself (X3/X4 files are ordinary MP4-style video)


def run(cfg, db_path, stop):
    """Analyze everything not yet analyzed. Returns counts. Raises Unavailable if the setup isn't ready."""
    st = setup_status(cfg)
    if not st["ready"]:
        bad = [i for i in st["items"] if i["required"] and not i["ok"]]
        raise Unavailable("; ".join(f"{i['label']}: {i['detail']}" for i in bad))
    ffmpeg = mo.find_tool("ffmpeg")
    con = mo.open_db(db_path)
    root = mo.meta_get(con, "root")
    todo = _targets(con, cfg)
    counts = {"analyzed": 0, "errors": 0, "skipped": 0, "titled": 0, "low_confidence": 0, "todo": len(todo)}
    _log(f"Analyze: {len(todo):,} clips to describe with {cfg['vision_model']}")
    t0 = time.time()
    mo.set_progress(phase="analyzing", done=0, total=len(todo), bytes=0, rate=0.0, eta=None, current="", started=t0)
    fails_in_a_row = 0

    def prepare(r, src):
        """Worker thread: frames + transcript for one clip (disk and ffmpeg bound), while the GPU describes the previous ones."""
        tmp = tempfile.mkdtemp(prefix="mediaorg_")
        try:
            frames = extract_frames(ffmpeg, src, r["duration"], max(1, int(cfg["frames_per_clip"])), tmp, hw=want_gpu_decode(r))
            if not frames:
                raise ValueError("could not read any frames")
            return tmp, frames, transcribe(cfg, ffmpeg, src, tmp)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    workers = max(1, int(cfg.get("analysis_workers", 3)))
    pool = ThreadPoolExecutor(max_workers=workers)
    sources, futures = {}, {}

    def submit(i):
        if i < len(todo):
            sources[i] = _source_for(con, root, todo[i])
            if sources[i] and os.path.isfile(sources[i]):
                futures[i] = pool.submit(prepare, todo[i], sources[i])

    for i in range(min(workers * 2, len(todo))):        # keep a few clips prepared ahead
        submit(i)
    for i, r in enumerate(todo):
        if stop.is_set():
            break
        submit(i + workers * 2)
        mo.set_progress(current=r["rel"])
        now = dt.datetime.now().isoformat(timespec="seconds")
        src = sources.pop(i, None)
        prev = con.execute("SELECT tries FROM analysis WHERE rel=?", (r["rel"],)).fetchone()
        tries = (prev[0] if prev else 0) + 1
        if i not in futures:
            con.execute("INSERT OR REPLACE INTO analysis(rel,status,error,tries,model,ts) VALUES(?,?,?,?,?,?)",
                        (r["rel"], "skipped", "file not found", tries, cfg["vision_model"], now))
            counts["skipped"] += 1
        else:
            tmp = None
            try:
                tmp, frames, text = futures.pop(i).result()
                desc, tags = describe(cfg, frames, text)
                con.execute("INSERT OR REPLACE INTO analysis VALUES(?,?,?,?,?,?,?,?,?)",
                            (r["rel"], "ok", desc, ", ".join(tags), text, None, tries, cfg["vision_model"], now))
                counts["analyzed"] += 1
                fails_in_a_row = 0
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                # Ollama went away: leave this clip untried, and give up after a few in a row (the run carries on to the plan)
                fails_in_a_row += 1
                if fails_in_a_row >= 3:
                    con.commit()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise Unavailable(f"Ollama stopped responding ({getattr(e, 'reason', e)})")
                continue
            except Exception as e:
                con.execute("INSERT OR REPLACE INTO analysis(rel,status,error,tries,model,ts) VALUES(?,?,?,?,?,?)",
                            (r["rel"], "error", f"{type(e).__name__}: {e}"[:300], tries, cfg["vision_model"], now))
                counts["errors"] += 1
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
        con.commit()
        done = i + 1
        rate = done / max(time.time() - t0, 0.001)
        mo.set_progress(done=done, rate=rate, eta=(len(todo) - done) / max(rate, 0.001))
        if done % 20 == 0:
            _log(f"  described {done:,}/{len(todo):,} clips ({rate * 60:.1f}/min)")
    pool.shutdown(wait=False, cancel_futures=True)
    for f in futures.values():                          # clips prepared but not used (stopped early): clean their temp folders
        if f.done() and not f.cancelled() and not f.exception():
            shutil.rmtree(f.result()[0], ignore_errors=True)
    if not stop.is_set():
        counts.update(build_events(cfg, con, stop))
    con.commit()
    con.close()
    _log(f"Analyze finished: {counts['analyzed']:,} described, {counts['errors']:,} failed, {counts['skipped']:,} skipped, "
         f"{counts['titled']:,} days named, {counts['low_confidence']:,} names need a look")
    return counts


def build_events(cfg, con, stop):
    """One name per day that has described clips and no name yet (or new clips since)."""
    days = defaultdict(list)
    for r in con.execute("""SELECT substr(f.date_taken,1,10) day, a.description FROM analysis a JOIN files f ON f.rel = a.rel
                            WHERE a.status='ok' AND f.date_taken IS NOT NULL ORDER BY f.date_taken"""):
        days[r["day"]].append(r["description"])
    have = {r["day"]: r["clips"] for r in con.execute("SELECT day, clips FROM events")}
    titled = low = 0
    for day, descs in sorted(days.items()):
        if stop.is_set():
            break
        if have.get(day) == len(descs):
            continue
        sample = descs if len(descs) <= 12 else [descs[int(i * len(descs) / 12)] for i in range(12)]
        try:
            title, summary, conf = name_event(cfg, day, sample)
        except Exception as e:
            _log(f"  couldn't name {day}: {e}")
            continue
        con.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?)",
                    (day, title, summary, conf, len(descs), dt.datetime.now().isoformat(timespec="seconds")))
        if title and conf >= cfg["ai_title_min_confidence"]:
            titled += 1
        else:
            low += 1
        con.commit()
    return {"titled": titled, "low_confidence": low}


# --------------------------------------------------------------------------- for the UI
def flags(con, cfg):
    """Things to look at in the morning: names the model wasn't sure about, clips that failed or were skipped."""
    out = []
    for r in con.execute("SELECT day, title, summary, confidence FROM events ORDER BY day DESC"):
        if not r["title"] or (r["confidence"] or 0) < cfg["ai_title_min_confidence"]:
            out.append({"kind": "name", "rel": "", "title": f"{r['day']}  ·  {r['title'] or '(no name)'}",
                        "detail": f"Not sure ({round((r['confidence'] or 0) * 100)}% confident), so the folder keeps just the date. "
                                  + (r["summary"] or "")})
    for r in con.execute("SELECT rel, status, error FROM analysis WHERE status IN ('error','skipped') ORDER BY ts DESC LIMIT 300"):
        out.append({"kind": r["status"], "rel": r["rel"], "title": r["rel"], "detail": r["error"] or ""})
    return out
