"""Locates the bundled tools (ffmpeg, ffprobe, QuickJS) and the yt-dlp engine.

yt-dlp is shipped as plain source next to the exe (not inside the PyInstaller archive) so that
"Update downloader engine" can drop a newer yt-dlp into %LOCALAPPDATA%\\VideoDownloader\\engine and
have it take precedence on the next start.  If an updated engine fails to import we fall back to the
built-in one automatically.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
EXE = ".exe" if os.name == "nt" else ""
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
PACKAGES = [("yt-dlp", "yt_dlp"), ("yt-dlp-ejs", "yt_dlp_ejs")]


def data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(os.environ.get("VDL_DATA_DIR") or Path(root) / "VideoDownloader")
    p.mkdir(parents=True, exist_ok=True)
    return p


def engine_dir() -> Path:
    return data_dir() / "engine"


def tools_dir() -> Path | None:
    for c in (os.environ.get("VDL_TOOLS"), BASE / "tools"):
        if c and Path(c, "ffmpeg" + EXE).exists():
            return Path(c)
    return None


def tool(name: str) -> str | None:
    td = tools_dir()
    if td and (td / (name + EXE)).exists():
        return str(td / (name + EXE))
    return shutil.which(name)


def ffmpeg_location() -> str | None:
    p = tool("ffmpeg")
    return str(Path(p).parent) if p else None


def js_runtimes() -> dict:
    """QuickJS is bundled (about 1.5 MB) so YouTube's signature challenges can be solved offline-install free."""
    for n in ("qjs", "quickjs"):
        td = tools_dir()
        if td and (td / (n + EXE)).exists():
            return {"quickjs": {"path": str(td / (n + EXE))}}
    for name, exe in (("deno", "deno"), ("node", "node"), ("bun", "bun")):
        if shutil.which(exe):
            return {name: {}}
    q = shutil.which("qjs")
    return {"quickjs": {"path": q}} if q else {}


ENGINE_SOURCE = "built-in"
ENGINE_ERROR = ""


def _active_override() -> Path | None:
    f = engine_dir() / "active.txt"
    if not f.exists():
        return None
    p = engine_dir() / f.read_text("utf-8").strip()
    return p if (p / "yt_dlp" / "__init__.py").exists() else None


def load():
    """Import yt_dlp (updated copy first, then the built-in one). Returns the module."""
    global ENGINE_SOURCE, ENGINE_ERROR
    bundled = BASE / "engine"
    if (bundled / "yt_dlp").exists() and str(bundled) not in sys.path:
        sys.path.insert(0, str(bundled))
    ov = None if os.environ.get("VDL_NO_ENGINE_UPDATE") else _active_override()
    if ov is not None:
        sys.path.insert(0, str(ov))
        try:
            import yt_dlp  # noqa
            import yt_dlp.version  # noqa
            ENGINE_SOURCE = "updated"
            return yt_dlp
        except Exception as e:  # broken update -> fall back
            ENGINE_ERROR = f"updated engine failed to load ({e}); using the built-in one"
            sys.path.remove(str(ov))
            for k in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.") or k.startswith("yt_dlp_ejs")]:
                del sys.modules[k]
    import yt_dlp  # noqa
    import yt_dlp.version  # noqa
    ENGINE_SOURCE = "built-in"
    return yt_dlp


def version() -> str:
    import yt_dlp.version
    return yt_dlp.version.__version__


def _vkey(v: str):
    out = []
    for p in v.replace("-", ".").split("."):
        out.append(int(p) if p.isdigit() else 0)
    return out


def _pypi_wheel(pkg: str):
    with urllib.request.urlopen(urllib.request.Request(f"https://pypi.org/pypi/{pkg}/json", headers={"User-Agent": UA}), timeout=30) as r:
        meta = json.load(r)
    ver = meta["info"]["version"]
    for f in meta["urls"]:
        if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("py3-none-any.whl"):
            return ver, f["url"], f["digests"]["sha256"]
    raise RuntimeError(f"no universal wheel for {pkg} {ver}")


def check_update() -> tuple[str, str]:
    """(installed, latest) yt-dlp versions."""
    return version(), _pypi_wheel("yt-dlp")[0]


def update(progress=None, force=False) -> str:
    """Download the newest yt-dlp (+ yt-dlp-ejs) wheels from PyPI, verify SHA-256, unpack, activate.
    Returns a human message.  Takes effect on the next start of the app."""
    cur = version()
    latest, _, _ = _pypi_wheel("yt-dlp")
    if not force and _vkey(latest) <= _vkey(cur):
        return f"The downloader engine is already up to date (yt-dlp {cur})."
    ed = engine_dir()
    ed.mkdir(parents=True, exist_ok=True)
    target = ed / f"yt-dlp-{latest}"
    tmp = ed / f".tmp-{latest}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    for pkg, mod in PACKAGES:
        if progress:
            progress(f"Downloading {pkg} ...")
        ver, url, sha = _pypi_wheel(pkg)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r:
            blob = r.read()
        if hashlib.sha256(blob).hexdigest() != sha:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"{pkg} download was corrupted (checksum mismatch); nothing was changed")
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if n.startswith(mod + "/"):
                    z.extract(n, tmp)
    if not (tmp / "yt_dlp" / "__init__.py").exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("downloaded engine looks incomplete; nothing was changed")
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    (ed / "active.txt").write_text(target.name, "utf-8")
    for old in ed.iterdir():  # keep only the active one
        if old.is_dir() and old != target:
            shutil.rmtree(old, ignore_errors=True)
    return f"Updated yt-dlp {cur} -> {latest}. Restart Video Downloader to use it."


def reset() -> None:
    shutil.rmtree(engine_dir(), ignore_errors=True)
