from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import APP_NAME, __version__


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="VideoDownloader", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--selftest-out", default="selftest-out")
    ap.add_argument("--serve-fixture", action="store_true")
    ap.add_argument("--fixture-port", type=int, default=0)
    ap.add_argument("--update-engine", action="store_true", help="update yt-dlp from PyPI (same as the button)")
    # GUI automation (CI + screenshots)
    ap.add_argument("--urls", help="links separated by |")
    ap.add_argument("--quality")
    ap.add_argument("--playlist", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--cancel-after", type=float)
    ap.add_argument("--folder")
    ap.add_argument("--shots-dir")
    ap.add_argument("--shot-prefix", default="")
    ap.add_argument("--size")
    ap.add_argument("--report")
    ap.add_argument("--timeout", type=int, default=600)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        from . import engine
        engine.load()
        print(f"{APP_NAME} {__version__} (yt-dlp {engine.version()}, {engine.ENGINE_SOURCE})")
        return 0
    if args.selftest:
        from . import selftest
        return selftest.run(Path(args.selftest_out))
    if args.serve_fixture:
        from . import selftest
        return selftest.serve_forever(args.fixture_port)
    if args.update_engine:
        from . import engine
        engine.load()
        print(engine.update(print))
        return 0

    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    QCoreApplication.setOrganizationName("VideoDownloader")
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for cand in (base / "icon.ico", base / "packaging" / "icon.ico"):
        if cand.exists():
            app.setWindowIcon(QIcon(str(cand)))
            break
    from .gui import MainWindow
    auto = {}
    if args.urls:
        auto = dict(urls=args.urls.split("|"), quality=args.quality, playlist=args.playlist, scan=args.scan,
                    download=args.download, cancel_after=args.cancel_after, folder=args.folder, shots_dir=args.shots_dir,
                    shot_prefix=args.shot_prefix, report=args.report, timeout=args.timeout,
                    size=tuple(int(x) for x in args.size.lower().split("x")) if args.size else None)
    w = MainWindow(auto)
    if auto:
        w.show()
        QTimer.singleShot(100, w.run_auto)
        return app.exec()
    w.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
