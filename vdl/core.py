"""Fetch info, scan pages for <video> sources, download with yt-dlp + ffmpeg, friendly error messages.
No Qt in here: the GUI and the selftest both use it."""
from __future__ import annotations

import html as htmlmod
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import engine

QUALITIES = ["Best", "1080p", "720p", "480p", "MP3 (audio)", "M4A (audio)"]
COOKIE_CHOICES = ["Off", "Edge", "Chrome", "Firefox"]
MEDIA_EXT = (".mp4", ".webm", ".m3u8", ".mov", ".mkv", ".m4v", ".mpd", ".ogv")


class UserError(Exception):
    """An error whose message is already written for the user."""


class Cancelled(Exception):
    pass


@dataclass
class Item:
    url: str
    title: str = ""
    duration: float | None = None
    thumbnail: str | None = None
    site: str = ""
    heights: list = field(default_factory=list)
    source: str = "link"          # link | playlist | page-scan
    quality: str = "Best"
    status: str = "ready"         # ready | downloading | done | error | cancelled
    message: str = ""
    progress: float = 0.0
    path: str = ""
    filesize: int = 0

    def label_duration(self) -> str:
        return fmt_duration(self.duration)


def fmt_duration(s) -> str:
    if not s:
        return "--:--"
    s = int(round(float(s)))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_bytes(n) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


# ---------------------------------------------------------------------------------------------- errors

def friendly(msg: str) -> str:
    """Turn a yt-dlp / network error into one clear sentence for the user."""
    m = re.sub(r"\x1b\[[0-9;]*m", "", str(msg or ""))
    low = m.lower()
    rules = [
        (("drm",), "This video is DRM-protected (copy-protected). Video Downloader does not bypass DRM, so it can't be saved."),
        (("private video", "this video is private", "is private", "video is set to private"),
         "This video is private. Only people the owner shared it with can watch it. If that's you, turn on "
         "'Cookies from browser' (Edge/Chrome) and try again."),
        (("not available in your country", "geo restrict", "geo-restrict", "not available from your location",
          "blocked it in your country", "region", "available in your country"),
         "This video is region-locked: the site doesn't allow it in your country. Video Downloader doesn't get around that."),
        (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
         "This video is age-restricted and needs you to be signed in. Turn on 'Cookies from browser' and pick the browser "
         "where you're signed in, then try again."),
        (("members-only", "join this channel", "members only", "premium", "subscriber", "requires payment", "purchase"),
         "This video is for paying members/subscribers only. If your account has access, turn on 'Cookies from browser' and try again."),
        (("could not copy", "cookie database", "failed to decrypt", "dpapi", "app-bound", "could not find", "cookies from browser"),
         "Couldn't read the browser's cookies. Close that browser completely (also from the system tray) and try again. "
         "Newer Chrome versions lock their cookies; Edge or Firefox usually works."),
        (("sign in to confirm you", "not a bot"),
         "The site wants to check you're not a bot. Turn on 'Cookies from browser' (a browser where you're signed in) and try again."),
        (("login", "log in", "logged-in", "logged in", "sign in", "authentication", "account credentials", "--cookies", "--username"),
         "This video needs you to be signed in. Turn on 'Cookies from browser' and pick the browser where you're signed in, then try again."),
        (("is live", "live event", "premieres in", "will begin in", "this live"),
         "This is a live stream or hasn't started yet. Live recording isn't supported; try again after it has finished."),
        (("video unavailable", "has been removed", "no longer available", "deleted", "account associated"),
         "This video is unavailable. It was removed or deleted."),
        (("http error 404", "404: not found", "404 not found"), "Nothing was found at that address (error 404). Check the link."),
        (("http error 403", "403: forbidden"), "The site refused the download (error 403). It may need you to sign in. Try 'Cookies from browser', or update the downloader engine."),
        (("http error 429", "too many requests"), "The site is limiting requests right now (error 429). Wait a few minutes and try again."),
        (("unsupported url",), "No video was found at that link. Tick 'Scan page for videos' to look for video players in the page."),
        (("no video formats", "no formats", "requested format is not available"),
         "No downloadable video was found for that link. Try another quality, or update the downloader engine."),
        (("ffmpeg",), "The built-in ffmpeg couldn't process this file: " + m.strip()[:160]),
        (("getaddrinfo", "name or service not known", "nodename nor servname", "timed out", "connection refused",
          "unable to download webpage", "network is unreachable", "connection reset", "remote end closed"),
         "Couldn't reach the site. Check your internet connection and the link, then try again."),
        (("no space left", "disk full", "errno 28"), "The save folder's drive is full."),
        (("permission denied", "access is denied", "errno 13"), "Can't write to the save folder. Pick another folder with Browse."),
        (("unable to extract", "extractor", "please report this issue", "signature", "nsig", "challenge"),
         "This site changed and the downloader engine can't read it yet. Press 'Update downloader engine' and try again."),
    ]
    for keys, text in rules:
        if any(k in low for k in keys):
            return text
    m = re.sub(r"^error:\s*", "", m.strip(), flags=re.I)
    m = re.sub(r"\[[\w:]+\]\s*[\w-]+:\s*", "", m)
    return (m[:220] + "...") if len(m) > 220 else (m or "Unknown error.")


class _Log:
    def __init__(self, sink=None):
        self.sink = sink
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        self.infos.append(msg)
        del self.infos[:-50]
        if self.sink:
            self.sink(msg)

    info = debug

    def warning(self, msg):
        self.warnings.append(msg)
        if self.sink:
            self.sink("WARNING: " + msg)

    def error(self, msg):
        self.errors.append(msg)
        if self.sink:
            self.sink(msg)


# ---------------------------------------------------------------------------------------------- options

def base_opts(cookies: str | None = None, log: _Log | None = None) -> dict:
    o = {
        "quiet": True, "no_warnings": False, "noprogress": True, "logger": log or _Log(),
        "socket_timeout": 30, "retries": 5, "fragment_retries": 10, "extractor_retries": 2,
        "windowsfilenames": True, "trim_file_name": 150, "noplaylist": True,
        "js_runtimes": engine.js_runtimes(), "color": {"stdout": "never", "stderr": "never"},
        "http_headers": {"User-Agent": engine.UA},
    }
    ff = engine.ffmpeg_location()
    if ff:
        o["ffmpeg_location"] = ff
    if cookies and cookies.lower() not in ("off", "none", ""):
        o["cookiesfrombrowser"] = (cookies.lower(),)
    return o


def format_opts(quality: str) -> dict:
    q = quality.lower()
    if "mp3" in q or "m4a" in q:
        codec = "mp3" if "mp3" in q else "m4a"
        return {"format": "ba[ext=m4a]/ba/b" if codec == "m4a" else "ba/b",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": "192"}],
                "final_ext": codec}
    h = {"1080p": 1080, "720p": 720, "480p": 480}.get(quality)
    o = {"merge_output_format": "mp4",
         "format_sort": (["res:%d" % h] if h else []) + ["fps", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]}
    if h:
        o["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/wv*+ba/w"
    else:
        o["format"] = "bv*+ba/b"
    # put HLS / TS / MOV results into an .mp4 that Windows plays everywhere (stream copy, no re-encode)
    o["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "ts>mp4/mov>mp4/flv>mp4/m4v>mp4"}]
    return o


# ---------------------------------------------------------------------------------------------- page scan

def _get(url: str, timeout=30) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": engine.UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ct = (r.headers.get("Content-Type") or "").lower()
        if ct and not any(k in ct for k in ("html", "xml", "text", "json", "javascript")):
            raise UserError("That link is a media file, not a web page.")
        raw = r.read(8 * 1024 * 1024)
        cs = r.headers.get_content_charset() or "utf-8"
        return raw.decode(cs, "replace"), r.geturl()


def scan_html(page: str, base: str) -> tuple[str, list[str], str | None]:
    """Return (page title, unique media URLs in page order, poster/og:image)."""
    title = ""
    mt = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if mt:
        title = htmlmod.unescape(re.sub(r"\s+", " ", mt.group(1))).strip()
    found: list[str] = []

    def add(u):
        if not u:
            return
        u = htmlmod.unescape(u.strip()).replace("\\/", "/")
        if u.startswith(("data:", "blob:", "javascript:")):
            return
        u = urllib.parse.urljoin(base, u)
        if u.startswith("http") and u not in found:
            found.append(u)

    attr = r"""(?:src|data-src|data-video-src|data-hls|data-mp4)\s*=\s*["']([^"']+)["']"""
    for vm in re.finditer(r"<video\b([^>]*)>(.*?)</video>", page, re.I | re.S):
        for a in re.finditer(attr, vm.group(1), re.I):
            add(a.group(1))
        for sm in re.finditer(r"<source\b([^>]*)>", vm.group(2), re.I):
            for a in re.finditer(attr, sm.group(1), re.I):
                add(a.group(1))
    for vm in re.finditer(r"<video\b([^>]*)/?>", page, re.I):
        for a in re.finditer(attr, vm.group(1), re.I):
            add(a.group(1))
    for mm in re.finditer(r"""<meta\b[^>]*(?:property|name)\s*=\s*["'](?:og:video(?::secure_url|:url)?|twitter:player:stream)["'][^>]*>""", page, re.I):
        c = re.search(r"""content\s*=\s*["']([^"']+)["']""", mm.group(0), re.I)
        if c:
            add(c.group(1))
    for um in re.finditer(r"""["'(]((?:https?:)?(?:\\?/)[^"'()\s<>]+?\.(?:mp4|webm|m3u8|mov|m4v|mpd|ogv)(?:\?[^"'()\s<>]*)?)["')]""", page, re.I):
        add(um.group(1))
    poster = None
    pm = re.search(r"""<video\b[^>]*poster\s*=\s*["']([^"']+)["']""", page, re.I)
    pu = htmlmod.unescape(pm.group(1)) if pm else og_image(page)
    if pu:
        poster = urllib.parse.urljoin(base, pu)
    # prefer a playlist (.m3u8) over its own segments; drop obvious segment files
    found = [u for u in found if not re.search(r"\.(ts|m4s)(\?|$)", u, re.I)]
    return title, found, poster


def og_image(page: str) -> str | None:
    for mm in re.finditer(r"<meta\b[^>]*>", page, re.I):
        tag = mm.group(0)
        if re.search(r"""(?:property|name)\s*=\s*["'](?:og:image(?::url|:secure_url)?|twitter:image)["']""", tag, re.I):
            c = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
            if c:
                return htmlmod.unescape(c.group(1))
    return None


def scan_page(url: str, cookies=None, log=None) -> list[Item]:
    try:
        page, final = _get(url)
    except Exception as e:
        raise UserError(friendly(str(e)))
    title, urls, poster = scan_html(page, final)
    if not urls:
        raise UserError("No video players were found in that page. If the video only appears after you sign in or press "
                        "play, it can't be found this way.")
    items = []
    for i, u in enumerate(urls, 1):
        name = Path(urllib.parse.urlparse(u).path).name or "video"
        it = Item(url=u, title=(f"{title} - {i}: {name}" if title else name), thumbnail=poster,
                  site=urllib.parse.urlparse(final).hostname or "", source="page-scan")
        try:  # fill in duration / heights quickly; failures are fine
            got = _info(u, cookies=cookies, playlist=False, log=log)
            if got.get("_type") != "playlist":
                _fill(it, got)
                it.title = f"{title} - {i}" if title and len(urls) > 1 else (title or it.title)
        except Exception:
            pass
        items.append(it)
    return items


# ---------------------------------------------------------------------------------------------- info

def _info(url, *, cookies=None, playlist=False, log=None) -> dict:
    yt_dlp = engine.load()
    log = log or _Log()
    o = base_opts(cookies, log)
    o.update({"skip_download": True, "noplaylist": not playlist, "extract_flat": "in_playlist" if playlist else False,
              "format": "bv*+ba/b/ba", "ignore_no_formats_error": False, "playlistend": 500})
    with yt_dlp.YoutubeDL(o) as y:
        return y.sanitize_info(y.extract_info(url, download=False))


def _fill(it: Item, info: dict):
    it.title = info.get("title") or it.title or info.get("id") or "video"
    it.duration = info.get("duration") or it.duration
    it.thumbnail = info.get("thumbnail") or it.thumbnail
    th = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    if not it.thumbnail and th:
        it.thumbnail = th[-1]["url"]
    it.site = info.get("extractor_key") or info.get("webpage_url_domain") or it.site
    if it.site in ("Generic", "HTML5MediaEmbed", "GenericEmbed") or not it.site:
        it.site = urllib.parse.urlparse(it.url).hostname or "web"
    hs = sorted({int(f["height"]) for f in (info.get("formats") or []) if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    if not hs and info.get("height"):
        hs = [int(info["height"])]
    it.heights = hs
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise UserError(friendly("is live"))


def media_probe(it: Item):
    """Direct media links: read duration / height with ffprobe and grab a frame for the thumbnail."""
    import json as _json
    import subprocess
    import tempfile
    nowin = 0x08000000 if os.name == "nt" else 0
    fp, ff = engine.tool("ffprobe"), engine.tool("ffmpeg")
    try:
        if fp:
            r = subprocess.run([fp, "-v", "error", "-user_agent", engine.UA, "-show_entries", "format=duration:stream=codec_type,height",
                                "-of", "json", it.url], capture_output=True, text=True, timeout=25, creationflags=nowin)
            j = _json.loads(r.stdout or "{}")
            d = float(j.get("format", {}).get("duration") or 0)
            if d > 0 and not it.duration:
                it.duration = d
            hs = sorted({int(s["height"]) for s in j.get("streams", []) if s.get("codec_type") == "video" and s.get("height")}, reverse=True)
            it.heights = it.heights or hs
        if ff and not it.thumbnail:
            out = Path(tempfile.gettempdir()) / f"vdl-thumb-{uuid.uuid4().hex[:10]}.jpg"
            ss = "1" if (it.duration or 0) > 2 else "0"
            subprocess.run([ff, "-y", "-v", "error", "-user_agent", engine.UA, "-ss", ss, "-i", it.url, "-frames:v", "1", "-vf", "scale=480:-2",
                            str(out)], capture_output=True, timeout=30, creationflags=nowin)
            if out.exists() and out.stat().st_size:
                it.thumbnail = str(out)
    except Exception:
        pass


def probe(url: str, *, playlist=False, scan=False, cookies=None, log=None) -> list[Item]:
    """One link -> one or more queue items.  Raises UserError with a friendly message."""
    url = url.strip()
    if not url:
        return []
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    if scan:
        return scan_page(url, cookies=cookies, log=log)
    log = log or _Log()
    try:
        info = _info(url, cookies=cookies, playlist=playlist, log=log)
    except UserError:
        raise
    except Exception as e:
        msg = str(e)
        if "unsupported url" in msg.lower():
            try:
                return scan_page(url, cookies=cookies, log=log)
            except UserError:
                raise UserError(friendly(msg))
        raise UserError(friendly(msg))
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise UserError("That playlist is empty, or its videos are private.")
        if not playlist and len(entries) > 1:
            raise UserError(f"This link is a playlist / page with {len(entries)} videos. Tick 'Whole playlist' to add them all.")
        out = []
        for e in entries:
            if e.get("_type") in ("url", "url_transparent") or not e.get("formats"):
                u = e.get("url") or e.get("webpage_url") or url
            elif len(e["formats"]) == 1 or (e.get("webpage_url") or url) == url:
                u = e["formats"][-1].get("url") or url   # several players on one page: download each player's own file
            else:
                u = e.get("webpage_url") or url
            if e.get("ie_key") == "Youtube" and not str(u).startswith("http"):
                u = "https://www.youtube.com/watch?v=" + u
            it = Item(url=u, source="playlist" if playlist else "link")
            try:
                _fill(it, e)
            except UserError as ue:
                it.status, it.message = "error", str(ue)
            if not it.thumbnail and e.get("id") and "youtube" in (e.get("ie_key") or "").lower():
                it.thumbnail = f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg"
            out.append(it)
        return out
    it = Item(url=info.get("webpage_url") or url)
    _fill(it, info)
    if not it.duration or not it.heights:
        media_probe(it)
    if not it.thumbnail and it.url.startswith("http"):
        try:
            page, final = _get(it.url, timeout=15)
            og = og_image(page)
            if og:
                it.thumbnail = urllib.parse.urljoin(final, og)
        except Exception:
            pass
    return [it]


# ---------------------------------------------------------------------------------------------- download

def download(it: Item, folder: str, *, quality: str | None = None, cookies=None, cancel: threading.Event | None = None,
             on_progress=None, log=None) -> str:
    """Download one item into folder. on_progress(fraction 0..1 or None, speed B/s, eta s, text). Returns the file path."""
    yt_dlp = engine.load()
    cancel = cancel or threading.Event()
    quality = quality or it.quality
    log = log or _Log()
    folder = str(Path(folder).resolve())
    Path(folder).mkdir(parents=True, exist_ok=True)
    tmp = Path(folder) / f".vdl-part-{uuid.uuid4().hex[:8]}"
    result = {"path": None}
    state = {"stage": 0, "last": 0.0}

    def hook(d):
        if cancel.is_set():
            raise Cancelled("cancelled")
        st = d.get("status")
        if st == "downloading":
            fn = d.get("filename") or ""
            if fn != state.get("fn"):
                state["fn"] = fn
                state["stage"] += 1
            done = d.get("downloaded_bytes") or 0
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            frac = None
            if tot:
                frac = min(1.0, done / tot)
            elif d.get("fragment_count"):
                frac = min(1.0, (d.get("fragment_index") or 0) / d["fragment_count"])
            now = time.monotonic()
            if on_progress and (now - state["last"] > 0.2 or (frac or 0) >= 1):
                state["last"] = now
                part = f"part {state['stage']} - " if state["stage"] > 1 else ""
                on_progress(frac, d.get("speed"), d.get("eta"), f"Downloading {part}{fmt_bytes(done)}" + (f" of {fmt_bytes(tot)}" if tot else ""))
        elif st == "finished":
            if on_progress:
                on_progress(1.0, None, None, "Downloaded, finishing ...")

    def pphook(d):
        if cancel.is_set():
            raise Cancelled("cancelled")
        if d.get("status") == "started" and on_progress:
            names = {"Merger": "Merging video and audio ...", "FFmpegExtractAudio": "Converting audio ...",
                     "FFmpegVideoRemuxer": "Making MP4 ...", "FixupM3u8": "Fixing up stream ..."}
            on_progress(1.0, None, None, names.get(d.get("postprocessor"), "Finishing ..."))
        if d.get("status") == "finished":
            fp = (d.get("info_dict") or {}).get("filepath")
            if fp:
                result["path"] = fp

    o = base_opts(cookies, log)
    o.update(format_opts(quality))
    o.update({"paths": {"home": folder, "temp": str(tmp)}, "outtmpl": {"default": "%(title).150B [%(id).40B].%(ext)s"},
              "progress_hooks": [hook], "postprocessor_hooks": [pphook], "noplaylist": True, "overwrites": False,
              "continuedl": False, "concurrent_fragment_downloads": 4, "keepvideo": False, "noprogress": True})
    if it.source == "page-scan":
        o["outtmpl"] = {"default": "%(title).150B.%(ext)s"}
    try:
        with yt_dlp.YoutubeDL(o) as y:
            info = y.extract_info(it.url, download=True)
            info = y.sanitize_info(info) if info else {}
            if info.get("_type") == "playlist":
                raise UserError("This link is a playlist; add it with 'Whole playlist' ticked.")
            for rd in info.get("requested_downloads") or []:
                if rd.get("filepath"):
                    result["path"] = rd["filepath"]
            result["path"] = result["path"] or info.get("filepath")
    except Cancelled:
        raise
    except UserError:
        raise
    except Exception as e:
        if cancel.is_set() or "cancelled" in str(e).lower() and isinstance(getattr(e, "exc_info", [None, None])[1], Cancelled):
            raise Cancelled("cancelled")
        if cancel.is_set():
            raise Cancelled("cancelled")
        raise UserError(friendly(str(e)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    p = result["path"]
    if p and Path(p).exists() and p.lower().endswith(".webm") and "audio" not in quality.lower():
        p = _try_mp4(p)
    if not p or not Path(p).exists():
        # yt-dlp skips files that already exist; find it
        if log.errors:
            raise UserError(friendly(log.errors[-1]))
        raise UserError("The download finished but the file couldn't be found in the folder.")
    return p


def _try_mp4(p: str) -> str:
    """WebM (VP9/AV1 + Opus) -> MP4 by stream copy, so it plays everywhere on Windows. Keeps the WebM if that can't be done."""
    import subprocess
    ff = engine.tool("ffmpeg")
    out = str(Path(p).with_suffix(".mp4"))
    if not ff or Path(out).exists():
        return p
    tmp = out + ".part.mp4"
    r = subprocess.run([ff, "-y", "-v", "error", "-i", p, "-map", "0", "-c", "copy", "-movflags", "+faststart", tmp],
                       capture_output=True, creationflags=0x08000000 if os.name == "nt" else 0)
    if r.returncode == 0 and Path(tmp).exists() and Path(tmp).stat().st_size > 0:
        os.replace(tmp, out)
        Path(p).unlink(missing_ok=True)
        return out
    Path(tmp).unlink(missing_ok=True)
    return p


def run_queue(items: list[Item], folder: str, *, cookies=None, cancel=None, on_item=None, on_progress=None, log=None):
    """Download every ready item in order; one failure doesn't stop the rest. Cancel stops the current one and the queue."""
    cancel = cancel or threading.Event()
    log = log or _Log()
    for idx, it in enumerate(items):
        if it.status not in ("ready", "error", "cancelled") or (it.status == "error" and not it.url):
            continue
        if cancel.is_set():
            break
        it.status, it.message, it.progress = "downloading", "Starting ...", 0.0
        if on_item:
            on_item(idx, it)
        try:
            it.path = download(it, folder, cookies=cookies, cancel=cancel, log=log,
                               on_progress=(lambda f, s, e, t, idx=idx, it=it: on_progress(idx, it, f, s, e, t)) if on_progress else None)
            it.filesize = Path(it.path).stat().st_size
            already = any("has already been downloaded" in m for m in (log.infos[-6:] if log else []))
            it.status, it.progress = "done", 1.0
            it.message = f"Already in folder ({fmt_bytes(it.filesize)})" if already else f"Saved {fmt_bytes(it.filesize)}"
        except Cancelled:
            it.status, it.message = "cancelled", "Cancelled"
        except UserError as e:
            it.status, it.message = "error", str(e)
        except Exception as e:  # pragma: no cover
            it.status, it.message = "error", friendly(str(e))
        if on_item:
            on_item(idx, it)
    return items
