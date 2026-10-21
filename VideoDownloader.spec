# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir build for Video Downloader (Windows x64; the same spec builds on Linux for testing).
# yt-dlp is kept OUT of the PYZ archive and shipped as plain source in engine/ so the in-app
# "Update downloader engine" button can override it with a newer copy.
import importlib.util
from pathlib import Path

root = Path(SPECPATH)
datas = [(str(root / "packaging" / "icon.ico"), "."), (str(root / "THIRD_PARTY.md"), "."),
         (str(root / "LICENSE"), "."), (str(root / "README.md"), ".")]
ENGINE_PKGS = ("yt_dlp", "yt_dlp_ejs")
for pkg in ENGINE_PKGS:
    pdir = Path(importlib.util.find_spec(pkg).origin).parent
    for f in pdir.rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts:
            datas.append((str(f), str(Path("engine") / pkg / f.relative_to(pdir).parent)))
tools = root / "tools"
if tools.exists():
    for f in tools.iterdir():
        if f.is_file():
            datas.append((str(f), "tools"))

hidden = ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "yt_dlp", "yt_dlp_ejs", "Cryptodome", "mutagen", "brotli",
          "websockets", "certifi", "requests", "urllib3", "sqlite3", "secretstorage", "xml.etree.ElementTree", "http.cookiejar",
          "ctypes.wintypes", "hmac", "unicodedata", "zipimport", "uuid", "netrc", "getpass", "xml.dom.minidom", "email.utils"]

a = Analysis([str(root / "VideoDownloader.py")], pathex=[str(root)], binaries=[], datas=datas, hiddenimports=hidden,
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["tkinter", "matplotlib", "IPython", "notebook", "pytest", "numpy", "scipy", "pandas", "cv2", "PIL",
                       "curl_cffi", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtPdf", "PySide6.QtDBus",
                       "PySide6.QtMultimedia", "PySide6.QtWebEngineCore", "PySide6.QtNetwork", "PySide6.Qt3DCore"],
             noarchive=False)
a.pure = [e for e in a.pure if not any(e[0] == p or e[0].startswith(p + ".") for p in ENGINE_PKGS)]
pyz = PYZ(a.pure, a.zipped_data)
icon = str(root / "packaging" / "icon.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VideoDownloader", debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False, disable_windowed_traceback=False, argv_emulation=False, icon=icon)
exe_console = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VideoDownloader_cli", debug=False,
                  bootloader_ignore_signals=False, strip=False, upx=False, console=True, icon=icon)
coll = COLLECT(exe, exe_console, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, upx_exclude=[], name="VideoDownloader")
