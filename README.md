# MediaOrg

Windows app that organizes a media drive **in place**: it reads the real capture date of every photo and video, keeps named project folders intact, date-sorts loose camera files, keeps Insta360 sets together, and builds an edit queue of shoot sessions. Nothing is copied or deleted, and every move can be undone.

> Status: phase 1 (indexing, planning, moving, reports). GPU content analysis and the dashboard UI are in progress.

## Install (Windows)

1. Download the latest `MediaOrg-…-windows.zip` from **[Releases](../../releases)**.
2. Unzip it anywhere, ideally on an internal SSD, and double-click **MediaOrg.exe**.
3. If Windows shows "Windows protected your PC", click **More info → Run anyway**. The app isn't code-signed yet.

ffprobe is bundled. [ExifTool](https://exiftool.org) is optional: put `exiftool.exe` in the `tools` folder next to MediaOrg.exe for better dates on RAW and some phone files.

Your scan database, settings and reports are kept in `%LOCALAPPDATA%\MediaOrg`, never on the media drive.

## Use

The app opens a menu:

1. **Set drive.** Enter the drive, e.g. `E:\`. Plug an external HDD into a rear motherboard USB port for best speed.
2. **Full scan.** Indexes, plans and reports. Nothing moves. Stop any time with Ctrl+C; the next scan resumes where it left off.
3. **Open the report.** Look over `plan.csv` (what would move where) and `review.csv` (what was set aside) in the reports folder (option 8).
4. **Apply.** Moves are same-drive renames, so they're instant. A move log is also written to `_Organized\_mediaorg_logs\` on the drive.
5. **Undo.** Reverses the last apply exactly.

## Run from source

```
pip install -r requirements.txt
python mediaorg.py            # menu
python mediaorg.py run E:\    # or scripted: index | plan | apply | undo | report | run
```
Needs Python 3.12+ and `ffprobe` on PATH (or in a `tools` folder next to the script).

## What it does with your files

| Situation | Result |
|---|---|
| Named folder (e.g. `Chariot Dump #1\arcade evan\front.mp4`) | Whole folder moves as one unit to `_Organized\2026\2026-07-09_Chariot Dump #1\…`, so the inside structure is untouched |
| Camera/generic folders (`DCIM\100GOPRO`, `Insta360`, `New folder`, `Camera`…) | Files are date-sorted into `_Organized\YYYY\YYYY-MM-DD\` |
| Insta360 `.insv` / `.insp` | `_00_` + `_10_` lens files and their `LRV_` proxy always move together, into `…\YYYY-MM-DD\360\` (or a separate `_Organized\360\` tree — set `insta360_mode` to `separate`) |
| GoPro `.LRV` / `.THM`, `.xmp`, `.srt` sidecars | Travel with their clip |
| Folder containing a Premiere/Resolve/AE/Vegas/Insta360 Studio project | **Skipped → review.** Moving it would break the project's media links |
| No real date (only "modified" time) | **Skipped → review.** Rename the folder with a date, or set `move_low_confidence_dates` to `true` |
| Name clash, locked file, read error | **Skipped → review.** The run keeps going |
| Duplicates | Listed in `duplicates.csv` only. Never deleted |

Edit settings (menu option 7) to add folder names that should count as generic, change the session gap, and so on. Then choose **Make a new plan** from the menu.

## Reports (menu option 8)

- `LATEST_REPORT.txt` is the summary, ready to open in Notepad. The bottom section, **"Prompt for Claude"**, is meant to be pasted back to Claude, along with your own notes, so the code gets improved for your actual drive.
- `edit_queue.csv` lists every video sorted by capture time and grouped into shoot sessions.
- `plan.csv`, `review.csv` and `duplicates.csv` cover the details.

## Coming in Phase 2 (GPU)
- Keyframe and thumbnail proxies cached on the NVMe, decoded on the 3090.
- A local vision model (via Ollama) describes each clip and photo, and Whisper transcribes audio.
- Date folders get event names, e.g. `2025-08-28_BurningMan_Helios`.
