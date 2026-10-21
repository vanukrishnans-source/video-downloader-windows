"""Single fixed screen (no scroll bars): links -> Fetch -> preview + queue (paged, 5 rows) -> Browse -> Download."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontMetrics, QGuiApplication, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from . import APP_NAME, __version__, core, engine

ACCENT = "#4c8dff"
BG = "#14171f"
PANEL = "#1c212c"
CARD = "#222837"
TEXT = "#e8ecf4"
MUTED = "#98a2b8"
OK = "#45c486"
BAD = "#ff6b7a"
ROWS = 5

STYLE = f"""
* {{ font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 11pt; color: {TEXT}; }}
QWidget#root {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLineEdit, QComboBox, QPlainTextEdit {{ background: {PANEL}; border: 1px solid #333b4f; border-radius: 8px; padding: 4px 10px; selection-background-color: {ACCENT}; }}
QLineEdit, QComboBox {{ min-height: 36px; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid #333b4f; selection-background-color: {ACCENT}; }}
QComboBox QAbstractItemView::item {{ min-height: 40px; }}
QPushButton, QToolButton {{ background: #2b3347; border: 1px solid #3a4560; border-radius: 8px; padding: 4px 14px; min-height: 36px; }}
QPushButton:hover, QToolButton:hover {{ background: #364060; }}
QPushButton:pressed, QToolButton:pressed {{ background: #44507a; }}
QPushButton:disabled, QToolButton:disabled {{ color: #6b7388; background: #222839; border-color: #2c3347; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #6aa0ff; }}
QPushButton#primary:disabled {{ background: #2c3a5c; border-color: #2c3a5c; color: #8a97b6; }}
QPushButton#danger {{ background: #5b2a33; border-color: #7a3a46; }}
QPushButton#danger:disabled {{ color: #6b7388; background: #222839; border-color: #2c3347; }}
QCheckBox {{ spacing: 8px; min-height: 36px; }}
QCheckBox::indicator {{ width: 24px; height: 24px; border-radius: 5px; border: 2px solid #4a5572; background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QProgressBar {{ background: {PANEL}; border: 1px solid #333b4f; border-radius: 7px; text-align: center; color: {TEXT}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}
QFrame#card {{ background: {CARD}; border-radius: 12px; }}
QFrame#row {{ background: {PANEL}; border: 1px solid #2a3144; border-radius: 10px; }}
QFrame#rowsel {{ background: #23304d; border: 2px solid {ACCENT}; border-radius: 10px; }}
QMessageBox {{ background: {BG}; }}
"""


def placeholder(w, h, text="No preview") -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(QColor("#10131a"))
    p = QPainter(pm)
    p.setPen(QColor(MUTED))
    f = QFont()
    f.setPointSize(10 if w < 200 else 13)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, text)
    p.end()
    return pm


def fit(pm: QPixmap, w, h) -> QPixmap:
    if pm.isNull():
        return placeholder(w, h)
    s = pm.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x, y = max(0, (s.width() - w) // 2), max(0, (s.height() - h) // 2)
    return s.copy(x, y, w, h)


def elide(label: QLabel, text: str, width: int):
    label.setText(QFontMetrics(label.font()).elidedText(text, Qt.ElideRight, width))
    label.setToolTip(text)


# ============================================================================ workers

class FetchWorker(QThread):
    item = Signal(object)
    failed = Signal(str, str)
    note = Signal(str)

    def __init__(self, urls, playlist, scan, cookies, quality):
        super().__init__()
        self.urls, self.playlist, self.scan, self.cookies, self.quality = urls, playlist, scan, cookies, quality
        self.stop = False

    def run(self):
        for n, u in enumerate(self.urls, 1):
            if self.stop:
                break
            self.note.emit(f"Reading link {n} of {len(self.urls)} ...")
            try:
                for it in core.probe(u, playlist=self.playlist, scan=self.scan, cookies=self.cookies):
                    it.quality = self.quality
                    self.item.emit(it)
            except core.UserError as e:
                self.failed.emit(u, str(e))
            except Exception as e:
                self.failed.emit(u, core.friendly(str(e)))


class DownloadWorker(QThread):
    item_changed = Signal(int)
    progress = Signal(int, object, object, object, str)

    def __init__(self, items, folder, cookies):
        super().__init__()
        self.items, self.folder, self.cookies = items, folder, cookies
        self.cancel = threading.Event()

    def run(self):
        core.run_queue(self.items, self.folder, cookies=self.cookies, cancel=self.cancel,
                       on_item=lambda i, it: self.item_changed.emit(i),
                       on_progress=lambda i, it, f, s, e, t: self.progress.emit(i, f, s, e, t))


class Thumbs(QObject):
    got = Signal(str, bytes)

    def __init__(self):
        super().__init__()
        self.cache: dict[str, QPixmap] = {}
        self.pending = set()
        self.got.connect(self._got)
        self.listeners = []

    def get(self, url):
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        if url not in self.pending:
            self.pending.add(url)
            threading.Thread(target=self._load, args=(url,), daemon=True).start()
        return None

    def _load(self, url):
        try:
            if os.path.exists(url):
                data = Path(url).read_bytes()
            else:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": engine.UA}), timeout=20) as r:
                    data = r.read(6 * 1024 * 1024)
        except Exception:
            data = b""
        self.got.emit(url, data)

    def _got(self, url, data):
        pm = QPixmap()
        pm.loadFromData(data)
        self.cache[url] = pm
        for f in self.listeners:
            f()


# ============================================================================ queue row

class Row(QFrame):
    clicked = Signal(int)
    removed = Signal(int)
    quality_changed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.setObjectName("row")
        self.idx = -1
        self.setFixedHeight(58)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(5, 3, 6, 3)
        lay.setSpacing(10)
        self.thumb = QLabel()
        self.thumb.setFixedSize(88, 50)
        lay.addWidget(self.thumb)
        mid = QVBoxLayout()
        mid.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight:600;")
        self.sub = QLabel()
        self.sub.setStyleSheet(f"color:{MUTED}; font-size:10pt;")
        mid.addWidget(self.title)
        mid.addWidget(self.sub)
        lay.addLayout(mid, 1)
        right = QVBoxLayout()
        right.setSpacing(3)
        self.status = QLabel()
        self.status.setFixedWidth(210)
        self.status.setStyleSheet("font-size:10pt;")
        self.bar = QProgressBar()
        self.bar.setFixedSize(210, 12)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        right.addWidget(self.status)
        right.addWidget(self.bar)
        lay.addLayout(right)
        self.q = QComboBox()
        self.q.addItems(core.QUALITIES)
        self.q.setFixedWidth(150)
        self.q.currentTextChanged.connect(lambda t: self.idx >= 0 and self.quality_changed.emit(self.idx, t))
        lay.addWidget(self.q)
        self.rm = QToolButton()
        self.rm.setText("\u2715")
        self.rm.setFixedSize(44, 44)
        self.rm.setToolTip("Remove from the queue")
        self.rm.clicked.connect(lambda: self.removed.emit(self.idx))
        lay.addWidget(self.rm)

    def mousePressEvent(self, e):
        self.clicked.emit(self.idx)
        super().mousePressEvent(e)

    def show_item(self, idx, it: core.Item, pm, selected, busy):
        self.idx = idx
        self.setObjectName("rowsel" if selected else "row")
        self.style().unpolish(self)
        self.style().polish(self)
        self.thumb.setPixmap(fit(pm, 88, 50) if pm is not None else placeholder(88, 50, "..."))
        elide(self.title, f"{idx + 1}. {it.title or it.url}", max(120, self.title.width() - 4))
        hs = f"up to {it.heights[0]}p" if it.heights else ""
        elide(self.sub, " \u00b7 ".join(x for x in (it.label_duration(), it.site, hs) if x), max(120, self.sub.width() - 4))
        color = {"done": OK, "error": BAD, "cancelled": "#f0b45a", "downloading": ACCENT}.get(it.status, MUTED)
        txt = {"ready": "Ready", "done": it.message or "Saved", "cancelled": "Cancelled"}.get(it.status, it.message or it.status)
        self.status.setStyleSheet(f"font-size:10pt; color:{color};")
        elide(self.status, txt, 206)
        if it.status == "error":
            self.status.setToolTip(it.message)
        self.bar.setValue(int((it.progress or 0) * 1000))
        self.bar.setVisible(it.status in ("downloading", "done"))
        self.q.blockSignals(True)
        self.q.setCurrentText(it.quality)
        self.q.blockSignals(False)
        self.q.setEnabled(not busy and it.status != "downloading")
        self.rm.setEnabled(not busy)


# ============================================================================ main window

class MainWindow(QWidget):
    def __init__(self, auto=None):
        super().__init__()
        self.auto = auto or {}
        self.setObjectName("root")
        self.setStyleSheet(STYLE)
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.settings = QSettings("VideoDownloader", "VideoDownloader")
        self.items: list[core.Item] = []
        self.sel = -1
        self.page = 0
        self.fetcher = None
        self.worker = None
        self.errors: list[tuple[str, str]] = []
        self.thumbs = Thumbs()
        self.thumbs.listeners.append(self.refresh)
        engine.load()
        self._build()
        self.setMinimumSize(1100, 640)
        self.resize(1280, 720)
        self.refresh()

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 12)
        root.setSpacing(7)

        # header
        h = QHBoxLayout()
        t = QLabel(APP_NAME)
        t.setStyleSheet("font-size:17pt; font-weight:700;")
        h.addWidget(t)
        self.engine_lbl = QLabel()
        self.engine_lbl.setStyleSheet(f"color:{MUTED}; font-size:10pt;")
        h.addSpacing(10)
        h.addWidget(self.engine_lbl)
        h.addStretch(1)
        self.btn_update = QPushButton("Update downloader engine")
        self.btn_update.setToolTip("Sites change often. This downloads the newest yt-dlp from PyPI (checksum verified).")
        self.btn_update.clicked.connect(self.update_engine)
        h.addWidget(self.btn_update)
        root.addLayout(h)
        self._engine_label()

        # links
        lk = QHBoxLayout()
        lk.setSpacing(8)
        self.links = QPlainTextEdit()
        self.links.setPlaceholderText("Paste a video link here (or several, one per line): YouTube, Vimeo, Wikimedia Commons, "
                                      "a direct .mp4 / .webm / .m3u8 link, or any web page with a video player")
        self.links.setFixedHeight(76)
        self.links.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.links.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lk.addWidget(self.links, 1)
        col = QVBoxLayout()
        col.setSpacing(8)
        self.btn_paste = QPushButton("Paste")
        self.btn_paste.setFixedWidth(130)
        self.btn_paste.setFixedHeight(34)
        self.btn_paste.clicked.connect(self.paste)
        self.btn_fetch = QPushButton("Fetch")
        self.btn_fetch.setObjectName("primary")
        self.btn_fetch.setFixedWidth(130)
        self.btn_fetch.setFixedHeight(34)
        self.btn_fetch.clicked.connect(self.fetch)
        col.addWidget(self.btn_paste)
        col.addWidget(self.btn_fetch)
        lk.addLayout(col)
        root.addLayout(lk)

        # options
        op = QHBoxLayout()
        op.setSpacing(12)
        op.addWidget(QLabel("Quality"))
        self.quality = QComboBox()
        self.quality.addItems(core.QUALITIES)
        self.quality.setFixedWidth(160)
        self.quality.setCurrentText(self.settings.value("quality", "Best"))
        self.quality.currentTextChanged.connect(self.quality_top)
        op.addWidget(self.quality)
        self.chk_playlist = QCheckBox("Whole playlist")
        self.chk_playlist.setChecked(self.settings.value("playlist", "false") == "true")
        self.chk_scan = QCheckBox("Scan page for videos")
        self.chk_scan.setToolTip("Look inside a normal web page for <video> players and .mp4 / .m3u8 links")
        op.addWidget(self.chk_playlist)
        op.addWidget(self.chk_scan)
        op.addStretch(1)
        op.addWidget(QLabel("Cookies from browser"))
        self.cookies = QComboBox()
        self.cookies.addItems(core.COOKIE_CHOICES)
        self.cookies.setFixedWidth(130)
        self.cookies.setToolTip("Only for videos you can already watch while signed in (private-to-you, age-restricted, members).\n"
                                "Reads that browser's cookies on this PC; close the browser first. Nothing is sent anywhere else.")
        self.cookies.setCurrentText(self.settings.value("cookies", "Off"))
        op.addWidget(self.cookies)
        root.addLayout(op)

        # middle: preview card | queue
        mid = QHBoxLayout()
        mid.setSpacing(12)
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(344)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.setSpacing(6)
        self.pv_img = QLabel()
        self.pv_img.setFixedSize(320, 180)
        cl.addWidget(self.pv_img)
        self.pv_title = QLabel()
        self.pv_title.setWordWrap(True)
        self.pv_title.setStyleSheet("font-size:12pt; font-weight:600;")
        self.pv_title.setFixedHeight(46)
        self.pv_title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        cl.addWidget(self.pv_title)
        self.pv_info = QLabel()
        self.pv_info.setWordWrap(True)
        self.pv_info.setStyleSheet(f"color:{MUTED};")
        self.pv_info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        cl.addWidget(self.pv_info, 1)
        mid.addWidget(card)

        q = QVBoxLayout()
        q.setSpacing(6)
        qh = QHBoxLayout()
        self.q_lbl = QLabel("Queue")
        self.q_lbl.setStyleSheet("font-size:12pt; font-weight:600;")
        qh.addWidget(self.q_lbl)
        qh.addStretch(1)
        self.btn_clear_done = QPushButton("Clear finished")
        self.btn_clear_done.clicked.connect(self.clear_done)
        self.btn_clear = QPushButton("Clear all")
        self.btn_clear.clicked.connect(self.clear_all)
        self.btn_prev = QPushButton("\u25c0 Prev")
        self.btn_prev.clicked.connect(lambda: self.goto_page(self.page - 1))
        self.page_lbl = QLabel()
        self.page_lbl.setMinimumWidth(80)
        self.page_lbl.setAlignment(Qt.AlignCenter)
        self.btn_next = QPushButton("Next \u25b6")
        self.btn_next.clicked.connect(lambda: self.goto_page(self.page + 1))
        for w in (self.btn_clear_done, self.btn_clear, self.btn_prev, self.page_lbl, self.btn_next):
            qh.addWidget(w)
        q.addLayout(qh)
        self.rows = []
        for _ in range(ROWS):
            r = Row()
            r.clicked.connect(self.select)
            r.removed.connect(self.remove)
            r.quality_changed.connect(self.row_quality)
            q.addWidget(r)
            self.rows.append(r)
        self.empty = QLabel("Paste a link above and press Fetch. Each video appears here with its thumbnail; "
                            "pick a quality per video, then Download.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(f"color:{MUTED}; font-size:12pt;")
        q.addWidget(self.empty, 1)
        q.addStretch(0)
        mid.addLayout(q, 1)
        root.addLayout(mid, 1)

        # folder
        fl = QHBoxLayout()
        fl.setSpacing(8)
        fl.addWidget(QLabel("Save to"))
        self.folder = QLineEdit()
        self.folder.setReadOnly(True)
        default = str(Path.home() / "Videos") if (Path.home() / "Videos").exists() else str(Path.home() / "Downloads")
        self.folder.setText(self.settings.value("folder", default))
        fl.addWidget(self.folder, 1)
        self.btn_browse = QPushButton("Browse ...")
        self.btn_browse.clicked.connect(self.browse)
        self.btn_open = QPushButton("Open folder")
        self.btn_open.clicked.connect(self.open_folder)
        fl.addWidget(self.btn_browse)
        fl.addWidget(self.btn_open)
        root.addLayout(fl)

        # progress + actions
        bt = QHBoxLayout()
        bt.setSpacing(10)
        pc = QVBoxLayout()
        pc.setSpacing(2)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(18)
        self.bar.setTextVisible(False)
        self.status = QLabel("Ready.")
        self.status.setStyleSheet(f"color:{MUTED};")
        pc.addWidget(self.bar)
        pc.addWidget(self.status)
        bt.addLayout(pc, 1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setFixedSize(130, 46)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_dl = QPushButton("Download")
        self.btn_dl.setObjectName("primary")
        self.btn_dl.setFixedSize(200, 46)
        self.btn_dl.clicked.connect(self.download)
        bt.addWidget(self.btn_cancel)
        bt.addWidget(self.btn_dl)
        root.addLayout(bt)

    def _engine_label(self):
        extra = f" \u00b7 {engine.ENGINE_ERROR}" if engine.ENGINE_ERROR else ""
        self.engine_lbl.setText(f"v{__version__} \u00b7 engine yt-dlp {engine.version()} ({engine.ENGINE_SOURCE}){extra}")

    # ------------------------------------------------------------------ state
    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def fetching(self):
        return bool(self.fetcher and self.fetcher.isRunning())

    def refresh(self):
        n = len(self.items)
        pages = max(1, (n + ROWS - 1) // ROWS)
        self.page = min(self.page, pages - 1)
        busy = self.busy()
        for k, r in enumerate(self.rows):
            i = self.page * ROWS + k
            if i < n:
                it = self.items[i]
                r.show_item(i, it, self.thumbs.get(it.thumbnail) if it.thumbnail else QPixmap(), i == self.sel, busy)
                r.setVisible(True)
            else:
                r.setVisible(False)
        self.empty.setVisible(n == 0)
        self.page_lbl.setText(f"{self.page + 1} / {pages}")
        self.btn_prev.setEnabled(self.page > 0)
        self.btn_next.setEnabled(self.page < pages - 1)
        todo = [it for it in self.items if it.status in ("ready", "error", "cancelled")]
        self.q_lbl.setText(f"Queue \u00b7 {n} video{'s' if n != 1 else ''}" + (f" \u00b7 {sum(i.status == 'done' for i in self.items)} done" if n else ""))
        self.btn_dl.setEnabled(bool(todo) and not busy and not self.fetching())
        self.btn_dl.setText("Download" if not any(i.status in ("error", "cancelled") for i in todo) or any(i.status == "ready" for i in todo) else "Retry")
        self.btn_cancel.setEnabled(busy or self.fetching())
        self.btn_fetch.setEnabled(not self.fetching())
        self.btn_clear.setEnabled(not busy and n > 0)
        self.btn_clear_done.setEnabled(not busy and any(i.status == "done" for i in self.items))
        self.btn_browse.setEnabled(not busy)
        self.btn_update.setEnabled(not busy)
        self.show_preview()

    def show_preview(self):
        if 0 <= self.sel < len(self.items):
            it = self.items[self.sel]
            pm = self.thumbs.get(it.thumbnail) if it.thumbnail else QPixmap()
            self.pv_img.setPixmap(fit(pm, 320, 180) if pm is not None else placeholder(320, 180, "Loading thumbnail ..."))
            self.pv_title.setText(QFontMetrics(self.pv_title.font()).elidedText(it.title or it.url, Qt.ElideRight, 320 * 2 - 40))
            self.pv_title.setToolTip(it.title)
            lines = [f"Duration: {it.label_duration()}", f"Site: {it.site or '-'}"]
            if it.heights:
                lines.append("Available: " + ", ".join(f"{h}p" for h in it.heights[:6]))
            lines.append(f"Quality: {it.quality}")
            if it.status == "error":
                lines.append(f"<span style='color:{BAD}'>{it.message}</span>")
            elif it.status == "done":
                lines.append(f"<span style='color:{OK}'>Saved: {Path(it.path).name}</span>")
            self.pv_info.setText("<br>".join(lines))
        else:
            self.pv_img.setPixmap(placeholder(320, 180, "Video preview"))
            self.pv_title.setText("")
            self.pv_info.setText("Thumbnail, title and length of the selected video show here.")

    # ------------------------------------------------------------------ actions
    def paste(self):
        t = QGuiApplication.clipboard().text().strip()
        if t:
            cur = self.links.toPlainText().strip()
            self.links.setPlainText((cur + "\n" + t).strip() if cur else t)

    def fetch(self):
        urls = [u.strip() for u in self.links.toPlainText().replace(",", "\n").split() if u.strip()]
        if not urls:
            self.status.setText("Paste at least one link first.")
            return
        self.settings.setValue("playlist", "true" if self.chk_playlist.isChecked() else "false")
        self.settings.setValue("cookies", self.cookies.currentText())
        self.errors = []
        self.fetcher = FetchWorker(urls, self.chk_playlist.isChecked(), self.chk_scan.isChecked(),
                                   self.cookies.currentText(), self.quality.currentText())
        self.fetcher.item.connect(self.add_item)
        self.fetcher.failed.connect(self.fetch_failed)
        self.fetcher.note.connect(self.status.setText)
        self.fetcher.finished.connect(self.fetch_done)
        self.bar.setRange(0, 0)
        self.fetcher.start()
        self.refresh()

    def add_item(self, it):
        if any(x.url == it.url and x.quality == it.quality and x.status != "done" for x in self.items):
            return
        self.items.append(it)
        if self.sel < 0:
            self.sel = len(self.items) - 1
        self.page = (len(self.items) - 1) // ROWS if len(self.items) <= ROWS or self.sel < 0 else self.page
        self.refresh()

    def fetch_failed(self, url, msg):
        self.errors.append((url, msg))

    def fetch_done(self):
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        if self.errors:
            u, m = self.errors[-1]
            self.status.setText(f"{len(self.errors)} link(s) could not be added. {m}")
            self.status.setStyleSheet(f"color:{BAD};")
            if not self.auto:
                QMessageBox.warning(self, APP_NAME, "\n\n".join(f"{u}\n\u2192 {m}" for u, m in self.errors[:6]))
        else:
            self.status.setStyleSheet(f"color:{MUTED};")
            self.status.setText(f"{len(self.items)} video(s) in the queue. Pick the quality, then press Download.")
        # links that worked are cleared from the box; failed ones stay for editing
        self.links.setPlainText("\n".join(u for u, _ in self.errors))
        self.refresh()

    def select(self, idx):
        self.sel = idx
        self.refresh()

    def remove(self, idx):
        if self.busy() or not (0 <= idx < len(self.items)):
            return
        del self.items[idx]
        self.sel = min(self.sel, len(self.items) - 1)
        self.refresh()

    def row_quality(self, idx, q):
        if 0 <= idx < len(self.items):
            self.items[idx].quality = q
            if self.items[idx].status in ("error", "cancelled", "done"):
                self.items[idx].status, self.items[idx].message, self.items[idx].progress = "ready", "", 0
            self.refresh()

    def quality_top(self, q):
        self.settings.setValue("quality", q)
        if not self.busy():
            for it in self.items:  # applies to everything not yet downloaded
                if it.status in ("ready", "error", "cancelled"):
                    it.quality = q
            self.refresh()

    def goto_page(self, p):
        self.page = max(0, p)
        self.refresh()

    def clear_done(self):
        self.items = [i for i in self.items if i.status != "done"]
        self.sel = min(self.sel, len(self.items) - 1)
        self.refresh()

    def clear_all(self):
        self.items, self.sel, self.page = [], -1, 0
        self.refresh()

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the folder to save videos in", self.folder.text())
        if d:
            self.folder.setText(d)
            self.settings.setValue("folder", d)

    def open_folder(self):
        p = Path(self.folder.text())
        p.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def download(self):
        folder = self.folder.text().strip()
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            t = Path(folder) / ".vdl-write-test"
            t.write_text("x")
            t.unlink()
        except Exception:
            self.status.setText("Can't write to that folder. Press Browse and choose another one.")
            self.status.setStyleSheet(f"color:{BAD};")
            return
        self.settings.setValue("folder", folder)
        self.settings.setValue("cookies", self.cookies.currentText())
        self.status.setStyleSheet(f"color:{MUTED};")
        self.t0 = time.time()
        self.worker = DownloadWorker(self.items, folder, self.cookies.currentText())
        self.worker.item_changed.connect(self.on_item)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.dl_done)
        self.worker.start()
        self.refresh()

    def _overall(self):
        todo = [i for i in self.items if i.status in ("downloading", "done", "ready") or i in getattr(self, "_run", [])]
        return todo

    def on_item(self, idx):
        if 0 <= idx < len(self.items) and self.items[idx].status == "downloading":
            self.sel = idx
            self.page = idx // ROWS
        self.refresh()

    def on_progress(self, idx, frac, speed, eta, text):
        if not (0 <= idx < len(self.items)):
            return
        it = self.items[idx]
        if it.status != "downloading":
            return
        if frac is not None:
            it.progress = frac
        it.message = text
        bits = [text]
        if speed:
            bits.append(f"{core.fmt_bytes(speed)}/s")
        if eta is not None:
            bits.append(f"{core.fmt_duration(eta)} left")
        n = len(self.items)
        self.status.setText(f"Video {idx + 1} of {n}: " + " \u00b7 ".join(bits))
        self.bar.setValue(int(((it.progress or 0)) * 1000))
        for r in self.rows:
            if r.idx == idx and r.isVisible():
                r.bar.setValue(int((it.progress or 0) * 1000))
                elide(r.status, " \u00b7 ".join(bits[1:]) or text, 206)

    def dl_done(self):
        done = sum(i.status == "done" for i in self.items)
        bad = [i for i in self.items if i.status == "error"]
        canc = sum(i.status == "cancelled" for i in self.items)
        msg = f"Finished: {done} saved"
        if bad:
            msg += f", {len(bad)} failed ({bad[0].message})"
        if canc:
            msg += f", {canc} cancelled"
        self.status.setText(msg + ". Press Open folder to see them.")
        self.status.setStyleSheet(f"color:{BAD if bad else OK};")
        self.bar.setValue(1000 if done and not bad and not canc else self.bar.value())
        self.refresh()

    def cancel(self):
        if self.fetching():
            self.fetcher.stop = True
            self.status.setText("Stopping after the current link ...")
        if self.busy():
            self.worker.cancel.set()
            self.status.setText("Cancelling ...")

    def update_engine(self):
        self.btn_update.setEnabled(False)
        self.status.setText("Checking for a newer downloader engine ...")
        res = {}

        def work():
            try:
                res["msg"] = engine.update()
            except Exception as e:
                res["err"] = core.friendly(str(e)) if "urlopen" in str(e) or "timed out" in str(e) else str(e)

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                QTimer.singleShot(200, poll)
                return
            self.btn_update.setEnabled(not self.busy())
            if "err" in res:
                self.status.setText("Engine update failed: " + res["err"])
                QMessageBox.warning(self, APP_NAME, "Engine update failed:\n" + res["err"])
                return
            self.status.setText(res["msg"])
            if "Restart" in res["msg"]:
                if QMessageBox.question(self, APP_NAME, res["msg"] + "\n\nRestart now?") == QMessageBox.Yes:
                    import subprocess
                    subprocess.Popen([sys.executable] + ([] if getattr(sys, "frozen", False) else ["-m", "vdl"]))
                    QApplication.quit()
            else:
                QMessageBox.information(self, APP_NAME, res["msg"])
        QTimer.singleShot(200, poll)

    def closeEvent(self, e):
        if self.busy():
            if QMessageBox.question(self, APP_NAME, "A download is running. Cancel it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.worker.cancel.set()
            self.worker.wait(5000)
        super().closeEvent(e)

    # ------------------------------------------------------------------ automation (CI + my screenshots)
    def shot(self, name):
        d = self.auto.get("shots_dir")
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
            QApplication.processEvents()
            self.grab().save(str(Path(d) / f"{self.auto.get('shot_prefix', '')}{name}.png"))

    def run_auto(self):
        a = self.auto
        if a.get("size"):
            self.resize(*a["size"])
        if a.get("quality"):
            self.quality.setCurrentText(a["quality"])
        self.chk_playlist.setChecked(bool(a.get("playlist")))
        self.chk_scan.setChecked(bool(a.get("scan")))
        if a.get("folder"):
            self.folder.setText(a["folder"])
        self.links.setPlainText("\n".join(a["urls"]))
        self.shot("1_links")
        self.btn_fetch.click()
        self.deadline = time.time() + a.get("timeout", 600)
        self.stage = "fetch"
        self.mid_shot = False
        QTimer.singleShot(300, self._auto_tick)

    def _auto_tick(self):
        a = self.auto
        if time.time() > self.deadline:
            self._auto_finish("timeout")
            return
        if self.stage == "fetch":
            if self.fetching() or any(it.thumbnail and it.thumbnail not in self.thumbs.cache for it in self.items[:ROWS]):
                QTimer.singleShot(300, self._auto_tick)
                return
            self.refresh()
            self.shot("2_fetched")
            if not a.get("download") or not self.items:
                self._auto_finish("fetched")
                return
            self.btn_dl.click()
            self.stage = "dl"
            self.t_dl = time.time()
        elif self.stage == "dl":
            if self.busy():
                cur = [i for i in self.items if i.status == "downloading"]
                if not self.mid_shot and cur and cur[0].progress and cur[0].progress > 0.15:
                    self.mid_shot = True
                    self.shot("3_downloading")
                if a.get("cancel_after") and time.time() - self.t_dl > a["cancel_after"] and not self.worker.cancel.is_set():
                    if not self.mid_shot:
                        self.mid_shot = True
                        self.shot("3_downloading")
                    self.btn_cancel.click()
                QTimer.singleShot(250, self._auto_tick)
                return
            QApplication.processEvents()
            self.refresh()
            self.shot("4_done")
            self._auto_finish("done")
            return
        QTimer.singleShot(300, self._auto_tick)

    def _auto_finish(self, why):
        rep = {"why": why, "errors": self.errors, "folder": self.folder.text(),
               "window": [self.width(), self.height()], "dpr": self.devicePixelRatioF(),
               "items": [{"url": i.url, "title": i.title, "duration": i.duration, "heights": i.heights, "quality": i.quality,
                          "status": i.status, "message": i.message, "path": i.path, "size": i.filesize} for i in self.items]}
        if self.auto.get("report"):
            Path(self.auto["report"]).write_text(json.dumps(rep, indent=2), "utf-8")
        want_dl = self.auto.get("download")
        ok = why in ("done", "fetched") and self.items and (not want_dl or self.auto.get("cancel_after") or
                                                            all(i.status == "done" for i in self.items))
        QApplication.exit(0 if ok else 3)
