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
STATE = {"running": False, "step": "", "pct": 0, "message": "", "error": None, "finished": None}


def _set(**kw):
    STATE.update(kw)


def _log(msg):
    getattr(mo, "print", print)(msg)


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
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=mo.NO_WINDOW | 0x00000008)     # DETACHED_PROCESS: survives closing MediaOrg
    return True


def wait_for_ollama(cfg, seconds=90):
    for i in range(seconds):
        if ollama_up(cfg):
            return True
        if i == 15 and not ollama_up(cfg):
            start_ollama(cfg)               # the installer normally starts it; if not, start it ourselves
        time.sleep(1)
    return False


def install_ollama(cfg):
    _set(step="ollama", pct=0, message="Downloading Ollama (one time, about 1.7 GB)…")
    dest = str(mo.APP_DIR / "downloads" / "OllamaSetup.exe")
    download(OLLAMA_INSTALLER_URL, dest, "Ollama installer", 0, 90)
    _set(pct=92, message="Checking the installer's signature…")
    verify_signed(dest)
    _set(pct=94, message="Installing Ollama…")
    r = subprocess.run([dest, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"The Ollama installer stopped with code {r.returncode}.")
    try:
        os.remove(dest)
    except OSError:
        pass
    _set(pct=98, message="Starting Ollama…")
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


def ensure(cfg):
    """Install/start whatever is missing. Blocking; safe to call again at any time. Returns True when ready."""
    if STATE["running"]:
        return False
    _set(running=True, step="check", pct=0, message="Checking what's installed…", error=None, finished=None)
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
        _set(step="done", pct=100, message="Ready for overnight runs.")
        return True
    except Exception as e:
        _set(error=f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e))
        _log("Setup problem: " + STATE["error"])
        return False
    finally:
        _set(running=False, finished=time.time())
