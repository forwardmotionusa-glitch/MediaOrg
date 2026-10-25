"""
File classification and report wording, from the first real-drive report: DJI .lrf proxies stay with their video,
DAW projects protect their folder, and the "only modified dates" count covers media, not audio.
Run:  python tests/test_classify.py
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mediaorg as mo  # noqa: E402


def main():
    assert mo.kind_of("DJI_0001.LRF", "lrf") == "sidecar"
    assert mo.kind_of("GL010123.LRV", "lrv") == "sidecar"
    for ext in ("song", "als", "flp", "cpr", "ptx", "rpp", "prproj"):
        assert mo.kind_of("x." + ext, ext) == "project", ext

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        mo.REPORT_DIR = td / "reports"
        mo.REPORT_DIR.mkdir()
        db = str(td / "t.db")
        con = mo.open_db(db)
        mo.meta_set(con, "root", str(td / "drive"))
        (td / "drive").mkdir()

        def add(rel, kind, ext, date=None, src="exif", conf="high", dur=None):
            con.execute("INSERT INTO files(rel,name,ext,kind,size,mtime,date_taken,date_source,date_conf,duration) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (rel, rel, ext, kind, 1000, 0.0, date, src, conf, dur))

        add("DJI_0001.MP4", "video", "mp4", "2025-05-01T10:00:00", "video_metadata", dur=20.0)
        add("DJI_0001.LRF", "sidecar", "lrf")
        add("song1.wav", "audio", "wav", "2024-01-01T00:00:00", "file_modified", "low")
        add("photo.jpg", "photo", "jpg", "2024-02-02T00:00:00", "file_modified", "low")
        con.execute("INSERT INTO analysis(rel,status,description,tags,error,tries,model,ts) VALUES('DJI_0001.MP4','error',NULL,NULL,"
                    "'OllamaError: Ollama said 400: nope',1,'m','t')")
        con.commit()
        con.close()

        # An older scan stored the .lrf as "other"; the next scan must fix that without re-reading the file
        drive2 = td / "drive2"
        drive2.mkdir()
        db2 = str(td / "t2.db")
        con = mo.open_db(db2)
        for n in ("DJI_0001.LRF", "DJI_0001.MP4"):
            (drive2 / n).write_bytes(b"x" * 100)
            st = (drive2 / n).stat()
            con.execute("INSERT INTO files(rel,name,ext,kind,size,mtime) VALUES(?,?,?,?,?,?)",
                        (n, n, n.rsplit(".", 1)[1].lower(), "other", st.st_size, st.st_mtime))
        con.commit()
        con.close()
        mo.cmd_index(argparse.Namespace(db=db2, root=str(drive2), workers=2), dict(mo.DEFAULT_CONFIG))
        con = mo.open_db(db2)
        assert con.execute("SELECT kind FROM files WHERE rel='DJI_0001.LRF'").fetchone()[0] == "sidecar"
        assert con.execute("SELECT kind FROM files WHERE rel='DJI_0001.MP4'").fetchone()[0] == "video"
        con.close()

        cfg = dict(mo.DEFAULT_CONFIG)
        mo.cmd_plan(argparse.Namespace(db=db, root=None, insta360=None, allow_mtime=False), cfg)
        con = mo.open_db(db)
        dst = {r["src"]: r["dst"] for r in con.execute("SELECT src, dst FROM plan_ops")}
        assert Path(dst["DJI_0001.MP4"]).parent == Path(dst["DJI_0001.LRF"]).parent, dst      # proxy travels with its video
        con.close()

        mo.cmd_report(argparse.Namespace(db=db, root=None), cfg)
        text = (mo.REPORT_DIR / "LATEST_REPORT.txt").read_text(encoding="utf-8")
        assert "1 media files only had file-modified dates" in text, text[-1500:]      # the photo, not the audio
        assert "1 audio files also only have modified dates" in text
        assert "## AI analysis (phase 2)" in text and "1 failed" in text and "Ollama said #" in text, text[-1800:]
    print("classify test ok")


if __name__ == "__main__":
    main()
