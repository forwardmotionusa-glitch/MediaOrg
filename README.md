# MediaOrg

Windows app that organizes a media drive **in place**: it reads the real capture date of every photo and video, keeps named project folders intact, date-sorts loose camera files, keeps Insta360 sets together, and builds an edit queue of shoot sessions. Nothing is copied or deleted, and every move can be undone.

> Status: phase 1 with the desktop dashboard (indexing, planning, moving, undo, reports). Phase 2 adds the overnight GPU analysis run (below).

## Install (Windows 10/11)

1. Download the latest `MediaOrg-…-windows.zip` from **[Releases](../../releases)**.
2. Unzip it anywhere, ideally on an internal SSD, and double-click **MediaOrg.exe**.
3. If Windows shows "Windows protected your PC", click **More info → Run anyway**. The app isn't code-signed yet.

ffprobe is bundled. [ExifTool](https://exiftool.org) is optional: put `exiftool.exe` in the `tools` folder next to MediaOrg.exe for better dates on RAW and some phone files. The window uses Microsoft Edge WebView2, which Windows 10/11 already include.

Your scan database, settings and reports are kept in `%LOCALAPPDATA%\MediaOrg`, never on the media drive.

## Use

- **Dashboard:** choose your drive and press **Start scan**. You'll see live progress, CPU / GPU / disk activity, and anything that needs you. A scan never moves anything. **Stop & save** any time; the next scan picks up where it left off.
- **Action items:** everything set aside so the scan never waits: folders with editing projects (keep in place or move anyway), files with no real date (pick one), name clashes, duplicates and incomplete Insta360 sets. Decisions go into the next plan.
- **Plan & apply:** before/after preview, then **Apply plan**. Moves are same-drive renames, so they're instant. Every move is logged (also on the drive in `_Organized\_mediaorg_logs\`) and **Undo** puts everything back.
- **Edit queue:** every video by capture time, grouped into shoot sessions, newest first. Click a clip to show it in Explorer, or copy a session's file list.
- **Reports:** the full report, plus **Copy prompt for Claude** to paste into a chat for improvements tuned to your drive.
- **Settings:** generic folder names, where Insta360 files go, session gap, and more.

## Run from source

```
pip install -r requirements.txt
python app.py                 # the desktop app
python mediaorg.py            # text menu
python mediaorg.py run E:\    # or scripted: index | plan | apply | undo | report | run
python devserver.py           # preview the UI in a browser at http://127.0.0.1:8765/index.html?dev=1
```
Needs Python 3.12+ and `ffprobe` on PATH (or in a `tools` folder next to the script).

## What it does with your files

| Situation | Result |
|---|---|
| Named folder (e.g. `Chariot Dump #1\arcade evan\front.mp4`) | Whole folder moves as one unit to `_Organized\2026\2026-07-09_Chariot Dump #1\…`, so the inside structure is untouched |
| Camera/generic folders (`DCIM\100GOPRO`, `Insta360`, `New folder`, `Camera`…) | Files are date-sorted into `_Organized\YYYY\YYYY-MM-DD\` |
| Insta360 `.insv` / `.insp` | `_00_` + `_10_` lens files and their `LRV_` proxy always move together, into `…\YYYY-MM-DD\360\` (or a separate `_Organized\360\` tree — choose it in Settings) |
| GoPro `.LRV` / `.THM`, `.xmp`, `.srt` sidecars | Travel with their clip |
| Folder containing a Premiere/Resolve/AE/Vegas/Insta360 Studio project | **Skipped → review.** Moving it would break the project's media links |
| No real date (only "modified" time) | **Skipped → review.** Rename the folder with a date, or pick a date in Action items |
| Name clash, locked file, read error | **Skipped → review.** The run keeps going |
| Duplicates | Listed in `duplicates.csv` only. Never deleted |

Change these in **Settings**, then **Rebuild plan** on the Plan & apply screen.

## Report files

- `LATEST_REPORT.txt` is the summary, ready to open in Notepad. The bottom section, **"Prompt for Claude"**, is meant to be pasted back to Claude, along with your own notes, so the code gets improved for your actual drive.
- `edit_queue.csv` lists every video sorted by capture time and grouped into shoot sessions.
- `plan.csv`, `review.csv` and `duplicates.csv` cover the details.

## Phase 2: overnight analysis (GPU)
Everything runs on this PC; nothing is uploaded.
- **Overnight run** (dashboard): scan, describe every clip, name each day's event, build the plan, write the report. It never stops to ask. It stops before Apply unless you tick "Also apply the plan when finished". Windows is kept awake while it runs, and an interrupted run can be resumed.
- Keyframes are decoded on the GPU (NVDEC) with the bundled `ffmpeg`, and described by a vision model served by [Ollama](https://ollama.com). Insta360 `.insv` clips are described from their `.lrv` proxy.
- Day folders get names like `2025-08-28 Beach day` when the model is confident (default 60%); unsure names keep just the date and appear under **Action items > AI review**, along with clips that failed. Descriptions show in the Edit queue.
- **Before the first night:** install Ollama, run `ollama pull qwen2.5vl:7b` once, and press **Check setup** on the dashboard. Whisper transcripts are optional: `pip install faster-whisper` (running from source), or put `whisper-cli.exe` and `ggml-small.bin` in the `tools` folder.
- Tests: `python tests/test_analyze.py` and `python tests/test_overnight.py` (fake Ollama, throwaway folder).
