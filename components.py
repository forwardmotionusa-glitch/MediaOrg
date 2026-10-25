#!/usr/bin/env python3
"""
First-run setup for phase 2: makes sure Ollama is installed and running and the vision model is downloaded,
so nobody has to fetch anything by hand. Progress is published in STATE for the dashboard.

- Ollama: the official installer (ollama.com), run silently for the current user (no admin prompt). Its Authenticode
  signature is checked before it runs.
- Vision model: pulled through Ollama's own API, which resumes after an interruption.
Everything is skipped when it is already there, so this is cheap to call on every start.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import analyze
import mediaorg as mo

OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
NEED_FREE_GB = 12                      # installer + model + headroom
STATE = {"running": False, "step": "", "pct": 0, "message": "", "error": None, "warning": None, "finished": None}


def _set(**kw):
    STATE.update(kw)


def _log(msg):
    getattr(mo, "print", print)(msg)


def clean_env():
    """Environment for programs MediaOrg launches (Ollama and its installer). The packaged app's bootloader puts its own
    folder on PATH; without this, Ollama's model runner would load MediaOrg's DLLs and keep them locked (so the app
    folder couldn't be updated or deleted while Ollama runs)."""
    env = dict(os.environ)
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        keep = [p for p in env.get("PATH", "").split(os.pathsep) if p and not os.path.normcase(p).startswith(os.path.normcase(meipass))]
        env["PATH"] = os.pathsep.join(keep)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def ollama_exe():
    cands = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
             os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe")]
    return next((c for c in cands if c and os.path.isfile(c)), None) or shutil.which("ollama")


def ollama_up(cfg, timeout=3):
    try:
        analyze._ollama(cfg, "/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def download(url, dest, label, pct_from=0, pct_span=100, headers=None):
    """Resumable download to dest (via dest.part), updating STATE as it goes."""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers=dict(headers or {}, **({"Range": f"bytes={have}-"} if have else {})))
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code != 416 or not have:        # 416: the .part file already holds the whole thing
            raise
        os.replace(part, dest)
        return dest
    with r:
        if have and r.status != 206:         # server ignored the range: start over
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as f:
            done, last = have, 0.0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 0.5 and total:
                    last = time.time()
                    _set(pct=round(pct_from + pct_span * done / total, 1),
                         message=f"{label}: {done / 1e9:.2f} of {total / 1e9:.2f} GB")
    if total and os.path.getsize(part) != total:
        raise IOError(f"{label}: download was cut short, it will resume next time")
    os.replace(part, dest)
    return dest


def verify_signed(path, publisher="Ollama"):
    """Only run the installer if Windows says its signature is valid and it was signed for Ollama."""
    ps = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
          "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60,
                         creationflags=mo.NO_WINDOW).stdout.splitlines()
    if not out or out[0].strip() != "Valid" or publisher.lower() not in " ".join(out[1:]).lower():
        raise RuntimeError(f"The Ollama installer's signature didn't check out ({' | '.join(out)[:120]}), so it was not run.")


def start_ollama(cfg):
    exe = ollama_exe()
    if not exe:
        return False
    # Have Explorer start it (as if you'd clicked it in the Start menu). A process we start ourselves inherits things
    # from MediaOrg that make Ollama's model runner load MediaOrg's DLLs and lock its folder, even with a clean PATH.
    app = os.path.join(os.path.dirname(exe), "ollama app.exe")
    subprocess.Popen(["explorer.exe", app if os.path.isfile(app) else exe], creationflags=mo.NO_WINDOW)
    return True


def stop_ollama():
    for image in ("ollama app.exe", "ollama.exe", "llama-server.exe"):
        subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True, creationflags=mo.NO_WINDOW)
    time.sleep(2)


def wait_for_ollama(cfg, seconds=90):
    for i in range(seconds):
        if ollama_up(cfg):
            return True
        if i == 30 and not ollama_up(cfg):
            start_ollama(cfg)               # still not answering: try starting it again
        time.sleep(1)
    return False


def install_ollama(cfg):
    _set(step="ollama", pct=0, message="Downloading Ollama (one time, about 1.7 GB)…")
    dest = str(mo.APP_DIR / "downloads" / "OllamaSetup.exe")
    download(OLLAMA_INSTALLER_URL, dest, "Ollama installer", 0, 90)
    _set(pct=92, message="Checking the installer's signature…")
    verify_signed(dest)
    _set(pct=94, message="Installing Ollama…")
    r = subprocess.run([dest, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=900, env=clean_env(),
                       cwd=os.path.dirname(dest))
    if r.returncode != 0:
        raise RuntimeError(f"The Ollama installer stopped with code {r.returncode}.")
    try:
        os.remove(dest)
    except OSError:
        pass
    _set(pct=98, message="Starting Ollama…")
    stop_ollama()                    # the installer started it as our child; restart it the clean way
    start_ollama(cfg)
    if not wait_for_ollama(cfg):
        raise RuntimeError("Ollama installed but didn't start. Open it from the Start menu, then check setup again.")


def pull_model(cfg):
    model = cfg["vision_model"]
    _set(step="model", pct=0, message=f"Downloading the vision model {model} (one time, several GB)…")
    req = urllib.request.Request(cfg["ollama_url"].rstrip("/") + "/api/pull", data=json.dumps({"name": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if not line.strip():
                continue
            j = json.loads(line)
            if j.get("error"):
                raise RuntimeError(f"Ollama couldn't download {model}: {j['error']}")
            if j.get("total"):
                _set(pct=round(100 * j.get("completed", 0) / j["total"], 1),
                     message=f"Vision model {model}: {j.get('completed', 0) / 1e9:.1f} of {j['total'] / 1e9:.1f} GB")
            else:
                _set(message=f"Vision model {model}: {j.get('status', '')}")


# --------------------------------------------------------------------------- Whisper (whisper.cpp)
WHISPER_REPO = "ggml-org/whisper.cpp"
WHISPER_MODEL_REPO = "ggerganov/whisper.cpp"
WHISPER_DIR = mo.APP_DIR / "components" / "whisper"


def _json_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "MediaOrg", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def pick_assets(releases):
    """Newest release that ships Windows binaries -> {'gpu': asset or None, 'cpu': asset}. Assets: {name, url, size, sha256}."""
    for rel in releases:
        by = {a["name"]: a for a in rel.get("assets", [])}
        cpu = by.get("whisper-bin-x64.zip")
        if not cpu:
            continue
        gpu = next((a for n, a in sorted(by.items(), reverse=True)
                    if n.startswith(("whisper-bin-win-cuda-12", "whisper-cublas-12")) and n.endswith("x64.zip")), None)
        fix = lambda a: a and {"name": a["name"], "url": a["browser_download_url"], "size": a["size"],
                               "sha256": (a.get("digest") or "").removeprefix("sha256:") or None}
        return {"gpu": fix(gpu), "cpu": fix(cpu), "tag": rel.get("tag_name")}
    return None


def find_whisper_cli_in(folder):
    for dirpath, _, files in os.walk(folder):
        if "whisper-cli.exe" in files:
            return os.path.join(dirpath, "whisper-cli.exe")
    return None


def whisper_selftest(cli, model):
    """Transcribe one second of silence. A GPU build on an unsupported card or driver fails here, not mid-run."""
    import tempfile
    import wave
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "s.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        r = subprocess.run([cli, "-m", model, "-f", wav, "-nt", "-np"], capture_output=True, timeout=180,
                           creationflags=mo.NO_WINDOW, cwd=os.path.dirname(cli))
        return r.returncode == 0


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_whisper_binary(assets, model, selftest=whisper_selftest):
    """Try the GPU build first, keep it only if it really runs; otherwise the small CPU build."""
    import zipfile
    order = [("GPU", assets["gpu"]), ("CPU", assets["cpu"])] if assets.get("gpu") and _has_nvidia() else [("CPU", assets["cpu"])]
    last = ""
    for variant, a in order:
        try:
            _set(step="whisper", pct=0, message=f"Downloading Whisper ({variant} build, {a['size'] / 1e6:.0f} MB)…")
            zpath = download(a["url"], str(mo.APP_DIR / "downloads" / a["name"]), f"Whisper {variant}", 0, 90)
            if a["sha256"] and _sha256(zpath).lower() != a["sha256"].lower():
                os.remove(zpath)
                raise RuntimeError("download didn't match its checksum")
            dest = WHISPER_DIR / variant.lower()
            shutil.rmtree(dest, ignore_errors=True)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(dest)
            os.remove(zpath)
            cli = find_whisper_cli_in(dest)
            if not cli:
                raise RuntimeError("whisper-cli.exe not in the download")
            _set(pct=95, message=f"Testing Whisper ({variant})…")
            if not selftest(cli, model):
                raise RuntimeError(f"the {variant} build didn't run on this PC")
            (WHISPER_DIR / "info.json").write_text(json.dumps({"cli": cli, "model": model, "variant": variant}), encoding="utf-8")
            return variant
        except Exception as e:
            last = f"{variant}: {e}"
            _log(f"Whisper {last}")
            shutil.rmtree(WHISPER_DIR / variant.lower(), ignore_errors=True)
    raise RuntimeError("Whisper couldn't be set up (" + last + ")")


def _has_nvidia():
    return bool(shutil.which("nvidia-smi")) or os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"))


def ensure_whisper(cfg):
    """Whisper binary + model for speech transcripts. Optional: a failure here never blocks overnight runs."""
    if not cfg.get("transcribe"):
        return
    if analyze.find_whisper_cli(cfg):
        return
    name = f"ggml-{cfg['whisper_model']}.bin"
    model = str(WHISPER_DIR / name)
    if not os.path.isfile(model):
        files = {f["path"]: f for f in _json_get(f"https://huggingface.co/api/models/{WHISPER_MODEL_REPO}/tree/main")}
        info = files.get(name)
        if not info:
            raise RuntimeError(f"Whisper model {name} isn't offered")
        _set(step="whisper", pct=0, message=f"Downloading the Whisper speech model {name} ({info['size'] / 1e6:.0f} MB)…")
        download(f"https://huggingface.co/{WHISPER_MODEL_REPO}/resolve/main/{name}", model, "Whisper model", 0, 90)
        want = (info.get("lfs") or {}).get("oid")
        if want and _sha256(model).lower() != want.lower():
            os.remove(model)
            raise RuntimeError("Whisper model download didn't match its checksum; it will be fetched again next time")
    assets = pick_assets(_json_get(f"https://api.github.com/repos/{WHISPER_REPO}/releases?per_page=20"))
    if not assets:
        raise RuntimeError("no Windows Whisper build found")
    install_whisper_binary(assets, model)


def ensure(cfg):
    """Install/start whatever is missing. Blocking; safe to call again at any time. Returns True when ready."""
    if STATE["running"]:
        return False
    _set(running=True, step="check", pct=0, message="Checking what's installed…", error=None, warning=None, finished=None)
    try:
        if os.name != "nt":
            raise RuntimeError("Automatic setup is for Windows. Install Ollama from ollama.com and run: ollama pull " + cfg["vision_model"])
        if not ollama_up(cfg):
            if ollama_exe():
                _set(message="Starting Ollama…")
                start_ollama(cfg)
                if not wait_for_ollama(cfg, 45):
                    raise RuntimeError("Ollama is installed but won't start. Open it from the Start menu, then check setup again.")
            else:
                free = shutil.disk_usage(mo.APP_DIR if mo.APP_DIR.exists() else mo.APP_DIR.parent).free / 1e9
                if free < NEED_FREE_GB:
                    raise RuntimeError(f"Only {free:.0f} GB free on this drive; setup needs about {NEED_FREE_GB} GB.")
                install_ollama(cfg)
        if not analyze._model_present(cfg, analyze._ollama(cfg, "/api/tags", timeout=10)):
            pull_model(cfg)
        try:
            ensure_whisper(cfg)
        except Exception as e:        # optional: clips are still described from pictures
            _log(f"Whisper setup problem: {e}")
            _set(warning=f"Speech transcripts aren't set up: {e}")
        _set(step="done", pct=100, message="Ready for overnight runs.")
        return True
    except Exception as e:
        _set(error=f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e))
        _log("Setup problem: " + STATE["error"])
        return False
    finally:
        _set(running=False, finished=time.time())
