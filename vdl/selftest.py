"""Headless end-to-end test: local web server with a fixture MP4, an HLS stream, a page with <video> tags,
a slow file (for Cancel) and error pages; download through the real engine, verify with ffprobe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, core, engine

NOWIN = 0x08000000 if os.name == "nt" else 0


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NOWIN, **kw)


def make_fixtures(root: Path):
    ff = engine.tool("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found")
    root.mkdir(parents=True, exist_ok=True)
    src = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    encs = _run([ff, "-hide_banner", "-encoders"]).stdout
    h264 = "libx264" in encs and not os.environ.get("VDL_FIXTURE_NOH264")
    enc = (["-c:v", "libx264", "-preset", "veryfast"] if h264 else ["-c:v", "mpeg4", "-q:v", "4"]) + \
        ["-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p"]
    # MPEG-TS segments for H.264 (the common case); fMP4 segments when the bundled LGPL ffmpeg has no H.264 encoder
    # MPEG-TS segments like real sites; the LGPL ffmpeg has no H.264 encoder, so its fixture uses MPEG-2 video in TS
    seg = ["-hls_segment_type", "mpegts"]
    hls_enc = enc if h264 else ["-c:v", "mpeg2video", "-q:v", "4", "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p"]
    if not (root / "clip.mp4").exists():
        r = _run([ff, "-y", "-hide_banner", "-loglevel", "error", *src, "-t", "5", *enc, "-movflags", "+faststart", str(root / "clip.mp4")])
        if r.returncode:
            raise RuntimeError(r.stderr)
    (root / "hls").mkdir(exist_ok=True)
    if not (root / "hls" / "index.m3u8").exists():
        r = _run([ff, "-y", "-hide_banner", "-loglevel", "error", *src, "-t", "6", *hls_enc, "-f", "hls", "-hls_time", "2",
                  "-hls_playlist_type", "vod", *seg, "-hls_segment_filename", str(root / "hls" / "seg%03d.ts"), str(root / "hls" / "index.m3u8")])
        if r.returncode:
            raise RuntimeError(r.stderr)
    if not (root / "second.webm").exists():  # VP8 + Vorbis, built into every ffmpeg
        r = _run([ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi",
                  "-i", "sine=frequency=660", "-t", "3", "-c:v", "libvpx", "-b:v", "500k", "-c:a", "libvorbis", str(root / "second.webm")])
        if r.returncode:  # some builds lack libvpx; fall back to a second mp4
            shutil.copy(root / "clip.mp4", root / "second.webm.mp4")
    second = "second.webm" if (root / "second.webm").exists() else "second.webm.mp4"
    (root / "page.html").write_text(f"""<!doctype html><html><head><title>Fixture videos</title>
<meta property="og:image" content="/poster.jpg"></head><body><h1>Two players</h1>
<video controls poster="/poster.jpg"><source src="/clip.mp4" type="video/mp4"></video>
<video controls data-src="/{second}"></video>
<script>var cfg = {{"hls":"\\/hls\\/index.m3u8"}};</script></body></html>""", "utf-8")
    (root / "nothing.html").write_text("<!doctype html><title>No video</title><p>Just text.</p>", "utf-8")
    if not (root / "poster.jpg").exists():
        _run([ff, "-y", "-loglevel", "error", "-i", str(root / "clip.mp4"), "-frames:v", "1", str(root / "poster.jpg")])
    return second


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.path.startswith("/slow/"):  # ~40 KB/s, for the Cancel test
            data = (Path(self.directory) / "clip.mp4").read_bytes() * 4
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for i in range(0, len(data), 4096):
                    self.wfile.write(data[i:i + 4096])
                    time.sleep(0.1)
            except Exception:
                pass
            return
        if self.path.startswith("/forbidden"):
            self.send_error(403)
            return
        return super().do_GET()


def serve(root: Path, port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(_Handler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def serve_forever(port=0, root=None):
    root = Path(root or Path(os.environ.get("TEMP", "/tmp")) / "vdl-fixture")
    make_fixtures(root)
    srv, base = serve(root, port)
    print(base, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def ffprobe(path) -> dict:
    fp = engine.tool("ffprobe")
    r = _run([fp, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height",
              "-of", "json", str(path)])
    j = json.loads(r.stdout or "{}")
    if not j:
        print("ffprobe failed:", path, r.returncode, r.stderr[-800:], flush=True)
    st = j.get("streams", [])
    return {"duration": float(j.get("format", {}).get("duration", 0) or 0), "format": j.get("format", {}).get("format_name"),
            "video": [s for s in st if s.get("codec_type") == "video"], "audio": [s for s in st if s.get("codec_type") == "audio"]}


def run(out: Path) -> int:
    out = Path(out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    os.environ.setdefault("VDL_DATA_DIR", str(out / "appdata"))
    checks = []

    def check(name, fn):
        t = time.time()
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail, "secs": round(time.time() - t, 2)})
            print(f"PASS {name}: {detail}", flush=True)
        except Exception as e:
            checks.append({"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
            print(f"FAIL {name}: {e}", flush=True)

    engine.load()
    fx = out / "fixture"
    second = make_fixtures(fx)
    srv, base = serve(fx)
    dl = out / "downloads"

    def tools():
        ff, fp, js = engine.tool("ffmpeg"), engine.tool("ffprobe"), engine.js_runtimes()
        assert ff and fp, "ffmpeg/ffprobe missing"
        if getattr(sys, "frozen", False):
            assert "quickjs" in js and Path(js["quickjs"]["path"]).exists(), f"bundled QuickJS missing: {js}"
            assert engine.tools_dir() is not None, "bundled tools dir missing"
            v = _run([js["quickjs"]["path"], "-e", "console.log(6*7)"])
            assert "42" in v.stdout, f"qjs did not run: {v.stdout} {v.stderr}"
        return {"yt_dlp": engine.version(), "engine": engine.ENGINE_SOURCE, "ffmpeg": ff, "js": js}
    check("engine + bundled tools", tools)

    def direct_mp4():
        its = core.probe(base + "/clip.mp4")
        assert len(its) == 1 and abs((its[0].duration or 0) - 5) < 0.6, its
        p = core.download(its[0], str(dl / "direct"), quality="Best")
        pr = ffprobe(p)
        assert p.endswith(".mp4") and pr["video"] and pr["audio"] and abs(pr["duration"] - 5) < 0.6, pr
        return {"file": Path(p).name, "probe": pr["video"][0]["codec_name"] + "+" + pr["audio"][0]["codec_name"], "secs": pr["duration"]}
    check("direct .mp4 link -> MP4 (ffprobe)", direct_mp4)

    def hls():
        its = core.probe(base + "/hls/index.m3u8")
        assert len(its) == 1, its
        p = core.download(its[0], str(dl / "hls"), quality="720p")
        pr = ffprobe(p)
        assert p.endswith(".mp4") and pr["video"] and pr["audio"] and abs(pr["duration"] - 6) < 0.8, (p, pr)
        assert pr["video"][0]["height"] == 720
        return {"file": Path(p).name, "h": pr["video"][0]["height"], "secs": pr["duration"], "fmt": pr["format"]}
    check("HLS .m3u8 stream at 720p -> MP4 (ffprobe)", hls)

    def audio():
        it = core.probe(base + "/clip.mp4")[0]
        r = {}
        for q, codec in (("MP3 (audio)", "mp3"), ("M4A (audio)", "aac")):
            p = core.download(it, str(dl / "audio"), quality=q)
            pr = ffprobe(p)
            assert not pr["video"] or all(v.get("codec_name") in ("mjpeg", "png") for v in pr["video"]), pr
            assert pr["audio"] and pr["audio"][0]["codec_name"] == codec, pr
            r[q] = Path(p).name
        return r
    check("audio only MP3 + M4A", audio)

    def scan():
        its = core.probe(base + "/page.html", scan=True)
        urls = [i.url for i in its]
        assert base + "/clip.mp4" in urls and base + "/" + second in urls and base + "/hls/index.m3u8" in urls, urls
        assert all(i.thumbnail and i.thumbnail.endswith("poster.jpg") for i in its)
        return {"found": [u.replace(base, "") for u in urls]}
    check("scan web page for <video> sources", scan)

    def generic_page():
        try:
            core.probe(base + "/page.html")
            return "single"
        except core.UserError as e:
            assert "playlist" in str(e).lower() or "video" in str(e).lower(), e
        its = core.probe(base + "/page.html", playlist=True)
        assert len(its) >= 2, its
        core.run_queue(its, str(dl / "playlist"))
        assert all(i.status == "done" for i in its), [(i.url, i.status, i.message) for i in its]
        return {"playlist_entries": len(its), "files": sorted(Path(i.path).name for i in its)}
    check("page with several videos: playlist message, then whole-playlist", generic_page)

    def queue():
        its = core.probe(base + "/page.html", scan=True)
        its += [core.Item(url=base + "/missing.mp4", title="missing"), core.Item(url=base + "/hls/index.m3u8", title="hls", quality="480p")]
        seen = []
        core.run_queue(its, str(dl / "queue"), on_item=lambda i, it: seen.append((i, it.status)))
        st = [i.status for i in its]
        assert st.count("done") == len(its) - 1 and its[-2].status == "error", [(i.url, i.status, i.message) for i in its]
        assert "404" in its[-2].message, its[-2].message
        for i in its:
            if i.status == "done":
                assert ffprobe(i.path)["video"], i.path
        return {"statuses": st, "error_msg": its[-2].message, "files": sorted(p.name for p in (dl / "queue").iterdir())}
    check("queue: 5 items, one broken link does not stop the rest", queue)

    def cancel():
        it = core.Item(url=base + "/slow/clip.mp4", title="slow")
        ev = threading.Event()
        threading.Timer(1.5, ev.set).start()
        t = time.time()
        core.run_queue([it, core.Item(url=base + "/clip.mp4")], str(dl / "cancel"), cancel=ev)
        assert it.status == "cancelled", (it.status, it.message)
        left = [p.name for p in (dl / "cancel").iterdir()] if (dl / "cancel").exists() else []
        assert not left, f"leftover files after cancel: {left}"
        return {"stopped_after_s": round(time.time() - t, 1), "leftovers": left}
    check("cancel mid-download stops queue and cleans up partial files", cancel)

    def errors():
        r = {}
        for path, want in (("/nothing.html", "no video"), ("/forbidden", "403"), ("/missing-page", "404")):
            try:
                core.probe(base + path)
                raise AssertionError(f"{path} should fail")
            except core.UserError as e:
                assert want in str(e).lower(), (path, str(e))
                r[path] = str(e)
        try:
            core.probe("http://127.0.0.1:1/x.mp4")
            raise AssertionError("dead port should fail")
        except core.UserError as e:
            assert "reach" in str(e).lower(), str(e)
            r["dead"] = str(e)
        samples = {
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video": "private",
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.": "age-restricted",
            "ERROR: [vimeo] 1: The web client only works when logged-in. Use --cookies": "signed in",
            "ERROR: [youtube] abc: The uploader has not made this video available in your country": "region-locked",
            "ERROR: [generic] This video is DRM protected": "drm",
            "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271": "cookies",
            "ERROR: [youtube] abc: Join this channel to get access to members-only content": "members",
        }
        for s, want in samples.items():
            f = core.friendly(s)
            assert want in f.lower(), (s, f)
            r[want] = f
        return r
    check("friendly errors: no video, 403, 404, offline, private, age, login, region, DRM, cookies, members", errors)

    def eng_update():
        # same code path as the button: PyPI -> SHA-256 check -> unpack -> activate; then a fresh process must load it
        cur, latest = engine.check_update()
        msg = engine.update(force=True)
        assert "Updated" in msg, msg
        cmd = [sys.executable, "--version"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "vdl", "--version"]
        r = _run(cmd, env=dict(os.environ), timeout=120)
        assert "updated" in r.stdout, (r.stdout, r.stderr[-500:])
        engine.reset()
        r2 = _run(cmd, env=dict(os.environ), timeout=120)
        assert "built-in" in r2.stdout, r2.stdout
        return {"installed": cur, "latest_on_pypi": latest, "after_update": r.stdout.strip(), "after_reset": r2.stdout.strip()}
    if os.environ.get("VDL_SELFTEST_ONLINE", "1") == "1":
        check("Update downloader engine (PyPI wheel, SHA-256, override, fallback)", eng_update)

    srv.shutdown()
    ok = all(c["ok"] for c in checks)
    rep = {"app": __version__, "ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks}
    (out / "selftest_report.json").write_text(json.dumps(rep, indent=2, default=str), "utf-8")
    print(f"SELFTEST {'OK' if ok else 'FAILED'} {rep['passed']}/{rep['total']}")
    return 0 if ok else 1
