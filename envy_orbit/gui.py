"""A native ENVY Orbit window: a real desktop app, not a web view.

Built with PyQt6 and styled in the ENVY palette with a live animated header.
Define recurring tracks with a cadence, log completions into an encrypted,
hash-chained journal, and watch the orbit board show what is overdue, due,
coming up soon or on track. Every save is a new revision; earlier revisions are
never overwritten.

Nothing is sent: no network, no accounts.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

from . import __version__
from . import core
from .core import OrbitError

BG = "#050713"
PANEL = "#0a0e22"
PANEL_HI = "#121735"
INK = "#f2f4ff"
MUTED = "#b9c3df"
DIM = "#6f7898"
PINK = "#ff3db4"
CYAN = "#35e8ff"
LAVENDER = "#ac8cff"
VIOLET = "#8547e8"
AMBER = "#ffb347"
GREEN = "#49e3a6"
LINE = "#1f2547"

STATE_COLOR = {"overdue": PINK, "due": AMBER, "pending": LAVENDER, "soon": CYAN, "on-track": GREEN}
STATE_LABEL = {"overdue": "overdue", "due": "due", "pending": "never logged",
               "soon": "soon", "on-track": "on track"}


def lerp(a, b, t):
    from PyQt6.QtGui import QColor
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * t),
                  int(ca.green() + (cb.green() - ca.green()) * t),
                  int(ca.blue() + (cb.blue() - ca.blue()) * t))


def _due_text(row):
    if row["state"] == "pending":
        return "never logged"
    days = row["days"]
    if days is None:
        return "—"
    if days < 0:
        return f"{-days}d overdue"
    if days == 0:
        return "due today"
    return f"in {days}d"


def _build():
    from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt6.QtGui import (QColor, QPainter, QLinearGradient, QRadialGradient, QFont,
                             QPen, QBrush)
    from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QPlainTextEdit,
                                 QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
                                 QAbstractItemView, QVBoxLayout, QHBoxLayout, QInputDialog,
                                 QFileDialog, QMessageBox, QLineEdit as QLE)

    class Header(QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedHeight(172)
            self.phase = 0.0
            self.stars = [(((i * 139 + 37) % 1000) / 1000.0,
                           ((i * 83 + 19) % 172) / 172.0,
                           0.6 + (i % 3) * 0.5, (i % 7) / 7.0) for i in range(60)]

        def tick(self, phase):
            self.phase = phase
            self.update()

        def paintEvent(self, _event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), QColor(BG))
            for cx, cy, rad, col, a in ((w - 120, 60, 250, CYAN, 46),
                                        (60, h, 280, VIOLET, 52), (w * 0.5, -40, 240, PINK, 38)):
                g = QRadialGradient(cx, cy, rad)
                c = QColor(col); c.setAlpha(a); g.setColorAt(0, c)
                c2 = QColor(col); c2.setAlpha(0); g.setColorAt(1, c2)
                p.setBrush(QBrush(g)); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(cx, cy), rad, rad)
            for sx, sy, sr, ph in self.stars:
                tw = 0.5 + 0.5 * math.sin(self.phase / 0.5 + ph * 6.28)
                c = QColor(205, 217, 255); c.setAlphaF(0.15 + 0.6 * tw)
                p.setBrush(c); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(sx * w, sy * h), sr, sr)
            # An orbiting body: a ring with a dot travelling around it, beside the mark.
            self._orbit(p, 44, 50, 17)
            pulse = 0.5 + 0.5 * math.sin(self.phase)
            p.setFont(QFont("Helvetica", 30, QFont.Weight.Bold))
            grad = QLinearGradient(78, 0, 430, 0)
            grad.setColorAt(0, QColor(CYAN))
            grad.setColorAt(0.5, QColor(lerp(CYAN, "#ffffff", 0.2 * pulse)))
            grad.setColorAt(1, QColor(PINK))
            glow = QColor(VIOLET); glow.setAlpha(int(55 + 55 * pulse))
            p.setPen(QPen(glow, 1))
            for dx, dy in ((1.3, 1.3), (-1.3, 1.3), (1.3, -1.3)):
                p.drawText(QRectF(78 + dx, 38 + dy, 520, 48), Qt.AlignmentFlag.AlignVCenter, "ENVY ORBIT")
            p.setPen(QPen(QBrush(grad), 1))
            p.drawText(QRectF(78, 38, 520, 48), Qt.AlignmentFlag.AlignVCenter, "ENVY ORBIT")
            p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
            p.setPen(QColor(LAVENDER))
            p.drawText(QRectF(80, 92, 700, 20), Qt.AlignmentFlag.AlignVCenter,
                       "ENCRYPTED  ·  RECURRING  ·  ON CADENCE")
            y = 126
            p.setPen(QPen(QColor(LINE), 2)); p.drawLine(80, y, w - 54, y)
            p.setPen(QPen(QColor(VIOLET), 3)); p.drawLine(80, y, 152, y)
            span = (w - 54) - 80
            x = 80 + span * ((self.phase * 0.12) % 1.0)
            halo = QRadialGradient(x, y, 15)
            hc = QColor(CYAN); hc.setAlpha(170); halo.setColorAt(0, hc)
            hc2 = QColor(CYAN); hc2.setAlpha(0); halo.setColorAt(1, hc2)
            p.setBrush(QBrush(halo)); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), 15, 15)
            p.setBrush(QColor("#ffffff")); p.drawEllipse(QPointF(x, y), 2.5, 2.5)
            p.end()

        def _orbit(self, p, cx, cy, r):
            # The central body.
            core_c = QColor(CYAN) if 0.5 + 0.5 * math.sin(self.phase) > 0.5 else QColor(PINK)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(core_c)
            p.drawEllipse(QPointF(cx, cy), 4.4, 4.4)
            # A tilted elliptical orbit path.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(LAVENDER), 1.4))
            p.drawEllipse(QRectF(cx - r, cy - r * 0.46, 2 * r, r * 0.92))
            # The travelling satellite.
            ang = self.phase * 1.3
            sx = cx + r * math.cos(ang)
            sy = cy + (r * 0.46) * math.sin(ang)
            halo = QRadialGradient(sx, sy, 6)
            hc = QColor(CYAN); hc.setAlpha(200); halo.setColorAt(0, hc)
            hc2 = QColor(CYAN); hc2.setAlpha(0); halo.setColorAt(1, hc2)
            p.setBrush(QBrush(halo)); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(sx, sy), 6, 6)
            p.setBrush(QColor("#ffffff")); p.drawEllipse(QPointF(sx, sy), 2.0, 2.0)

    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setDefaultSectionSize(28)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        return table

    ID_ROLE = int(Qt.ItemDataRole.UserRole)

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.orbit = None      # Path of the current encrypted orbit file
            self.secret = None     # passphrase bytes, kept in memory for this orbit only
            self.data = None       # decrypted, validated orbit dict
            self.report = None     # the latest status() result
            self.setWindowTitle("ENVY Orbit")
            self.resize(1180, 880)
            self.setMinimumSize(980, 740)
            self.setStyleSheet(STYLE)
            root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
            self.header = Header(); root.addWidget(self.header)

            body = QWidget(); body.setObjectName("body")
            bl = QVBoxLayout(body); bl.setContentsMargins(22, 14, 22, 16); bl.setSpacing(10)

            intro = QLabel("Define recurring tracks with a cadence, log each completion, and see what is "
                           "overdue, due, coming up soon or on track. Every save is a new revision. Nothing is sent.")
            intro.setObjectName("status"); intro.setWordWrap(True); bl.addWidget(intro)

            controls = QHBoxLayout()
            for caption, handler, name in (("New orbit", self.new_orbit, "go"),
                                           ("Open", self.open_orbit, "pick"),
                                           ("Lock", self.lock, "pick"),
                                           ("Add track", self.add_track, "go"),
                                           ("Log done", self.log_done, "go"),
                                           ("Remove track", self.remove_track, "pick"),
                                           ("Export report", self.export_report, "pick")):
                b = QPushButton(caption); b.setObjectName(name)
                b.setCursor(Qt.CursorShape.PointingHandCursor); b.clicked.connect(handler)
                controls.addWidget(b)
            controls.addStretch(1)
            bl.addLayout(controls)

            self.mon_header = QPlainTextEdit(); self.mon_header.setObjectName("mono")
            self.mon_header.setReadOnly(True); self.mon_header.setFixedHeight(84)
            self.mon_header.setPlainText("No orbit open. Create a new orbit or open an existing one.")
            bl.addWidget(self.mon_header)

            self.views = QTabWidget(); self.views.setObjectName("tabs")
            self.t_orbit = make_table(["Status", "Track", "Cadence", "Last done", "Next due", "Due in"])
            self.t_tracks = make_table(["Track", "Cadence", "Grace", "Completions", "Added"])
            self.t_history = make_table(["Logged UTC", "Track", "Completed (UTC)", "Note"])
            self.views.addTab(self.t_orbit, "Orbit")
            self.views.addTab(self.t_tracks, "Tracks")
            self.views.addTab(self.t_history, "History")
            bl.addWidget(self.views, 1)

            self.path_label = QLabel(""); self.path_label.setObjectName("foot")
            self.path_label.setWordWrap(True); bl.addWidget(self.path_label)
            foot = QLabel("Orbit records when you logged a completion, not that you did it. A cadence is a "
                          "reminder, not an enforcement — nothing acts for you. Times are assertions from your "
                          "clock; the hash chain orders entries, it does not prove when they happened.")
            foot.setObjectName("foot"); foot.setWordWrap(True); bl.addWidget(foot)
            root.addWidget(body, 1)

            self.phase = 0.0
            self.timer = QTimer(self); self.timer.timeout.connect(self._animate); self.timer.start(33)
            self._set_enabled(False)

        # ----- lifecycle ----------------------------------------------------
        def _ask_passphrase(self, confirm):
            text, ok = QInputDialog.getText(self, "Passphrase",
                                            "Orbit passphrase (at least 12 characters):", QLE.EchoMode.Password)
            if not ok:
                return None
            secret = text.encode("utf-8")
            if confirm:
                again, ok2 = QInputDialog.getText(self, "Confirm passphrase",
                                                  "Repeat the passphrase:", QLE.EchoMode.Password)
                if not ok2:
                    return None
                if again.encode("utf-8") != secret:
                    self._warn("Passphrases do not match."); return None
            try:
                return core.check_passphrase(secret)
            except OrbitError as exc:
                self._warn(str(exc)); return None

        def new_orbit(self):
            title, ok = QInputDialog.getText(self, "New orbit", "Orbit title:")
            if not ok or not title.strip():
                return
            dest, _ = QFileDialog.getSaveFileName(self, "Save the encrypted orbit", "cadence.envyorbit",
                                                  "Orbit (*.envyorbit);;All files (*)")
            if not dest:
                return
            secret = self._ask_passphrase(confirm=True)
            if secret is None:
                return
            try:
                if Path(dest).exists():
                    Path(dest).unlink()
                core.create(dest, title.strip(), secret)
                self._open_path(Path(dest), secret)
                self._warn("Orbit created. Add a track to start tracking a recurring obligation.",
                           icon=QMessageBox.Icon.Information, title="Orbit created")
            except (OrbitError, OSError) as exc:
                self._warn(str(exc))

        def open_orbit(self):
            path, _ = QFileDialog.getOpenFileName(self, "Open an encrypted orbit", "",
                                                  "Orbit (*.envyorbit);;All files (*)")
            if not path:
                return
            secret = self._ask_passphrase(confirm=False)
            if secret is None:
                return
            self._open_path(Path(path), secret)

        def _open_path(self, path, secret):
            try:
                data, _ = core._load(path, secret)
                report = core.status(path, secret)
            except OrbitError as exc:
                self._warn(str(exc)); return
            self.orbit, self.secret, self.data, self.report = path, secret, data, report
            self._set_enabled(True)
            self._refresh_all()

        def lock(self):
            self.orbit = self.secret = self.data = self.report = None
            for t in (self.t_orbit, self.t_tracks, self.t_history):
                t.setRowCount(0)
            self.mon_header.setPlainText("Locked. The passphrase was cleared from memory.")
            self.path_label.setText("")
            self._set_enabled(False)

        def _reopen(self, path):
            self._open_path(Path(path), self.secret)

        # ----- mutations ----------------------------------------------------
        def add_track(self):
            if not self.data:
                return
            title, ok = QInputDialog.getText(self, "Add track", "What recurs? (e.g. Rotate signing key)")
            if not ok or not title.strip():
                return
            cadence, ok = QInputDialog.getInt(self, "Cadence", "Repeat every how many days?",
                                              30, core.MIN_CADENCE, core.MAX_CADENCE)
            if not ok:
                return
            grace, ok = QInputDialog.getInt(self, "Grace", "Grace days after the due date (0 for none):",
                                            0, 0, core.MAX_GRACE)
            if not ok:
                return
            dest = self._next_revision_path()
            try:
                core.add_track(self.orbit, dest, self.secret, title.strip(), cadence, grace)
            except (OrbitError, OSError) as exc:
                self._warn(str(exc)); return
            self._reopen(dest)
            self.views.setCurrentIndex(0)

        def log_done(self):
            if not self.data:
                return
            track_id, label = self._selected_track()
            if not track_id:
                self._warn("Select a track in the Orbit or Tracks tab first."); return
            note, ok = QInputDialog.getText(self, "Log completion",
                                            f"Note for “{label}” (optional):")
            if not ok:
                return
            dest = self._next_revision_path()
            try:
                core.log(self.orbit, dest, self.secret, track_id, note=note.strip())
            except (OrbitError, OSError) as exc:
                self._warn(str(exc)); return
            self._reopen(dest)
            self.views.setCurrentIndex(0)

        def remove_track(self):
            if not self.data:
                return
            track_id, label = self._selected_track()
            if not track_id:
                self._warn("Select a track in the Orbit or Tracks tab first."); return
            confirm = QMessageBox.question(self, "Remove track",
                                           f"Remove “{label}” from this orbit?\n\n"
                                           "Its logged completions stay in the journal; only the recurring "
                                           "track is removed.")
            if confirm != QMessageBox.StandardButton.Yes:
                return
            dest = self._next_revision_path()
            try:
                core.remove_track(self.orbit, dest, self.secret, track_id)
            except (OrbitError, OSError) as exc:
                self._warn(str(exc)); return
            self._reopen(dest)

        def export_report(self):
            if not self.data:
                return
            choice, ok = QInputDialog.getItem(self, "Export report", "Format:",
                                              ["HTML report", "JSON report"], 0, False)
            if not ok:
                return
            fmt = "html" if choice.startswith("HTML") else "json"
            dest, _ = QFileDialog.getSaveFileName(self, "Export status report",
                                                  f"orbit-report.{fmt}", f"{fmt.upper()} (*.{fmt});;All files (*)")
            if not dest:
                return
            try:
                if Path(dest).exists():
                    Path(dest).unlink()
                result = core.export(self.orbit, dest, self.secret, fmt)
            except (OrbitError, OSError) as exc:
                self._warn(str(exc)); return
            self._warn(f"Exported a plaintext {fmt.upper()} report to {result['output']}.\n\n"
                       f"SHA-256: {result['sha256']}", icon=QMessageBox.Icon.Information, title="Report exported")

        # ----- selection helpers -------------------------------------------
        def _selected_track(self):
            table = self.views.currentWidget()
            if table not in (self.t_orbit, self.t_tracks):
                table = self.t_orbit
            row = table.currentRow()
            if row < 0:
                return None, None
            item = table.item(row, 0)
            if item is None:
                return None, None
            return item.data(ID_ROLE), item.data(ID_ROLE + 1)

        # ----- display ------------------------------------------------------
        def _refresh_all(self):
            self._refresh_header()
            self._fill_orbit(self.report)
            self._fill_tracks(self.data["tracks"], self.report)
            self._fill_history(self.data["journal"], self.data["tracks"])

        def _refresh_header(self):
            d, r = self.data, self.report
            if not d:
                return
            if r and r["all_clear"]:
                verdict = "all clear"
            elif r:
                c = r["counts"]
                verdict = f"{r['needs_attention']} need attention ({c['overdue']} overdue, " \
                          f"{c['due']} due, {c['pending']} pending)"
            else:
                verdict = "—"
            self.mon_header.setPlainText(
                f"{d['title']}  ·  revision {d['revision']}  ·  {len(d['tracks'])} tracks  ·  {verdict}\n"
                f"orbit: {d['orbit_id']}")
            if self.orbit:
                self.path_label.setText(str(self.orbit))

        def _cell(self, text, color=None):
            item = QTableWidgetItem("" if text is None else str(text))
            if color:
                item.setForeground(QColor(color))
            return item

        def _tag(self, item, track_id, label):
            item.setData(ID_ROLE, track_id)
            item.setData(ID_ROLE + 1, label)
            return item

        def _fill_orbit(self, report):
            t = self.t_orbit
            rows = report["tracks"] if report else []
            t.setRowCount(len(rows))
            for i, r in enumerate(rows):
                state = r["state"]
                status_item = self._cell(STATE_LABEL[state], STATE_COLOR[state])
                self._tag(status_item, r["id"], r["title"])
                t.setItem(i, 0, status_item)
                t.setItem(i, 1, self._cell(r["title"], INK))
                t.setItem(i, 2, self._cell(f"every {r['cadence_days']}d"))
                t.setItem(i, 3, self._cell(r["last_done_utc"] or "—"))
                t.setItem(i, 4, self._cell(r["next_due_utc"] or "—"))
                t.setItem(i, 5, self._cell(_due_text(r), STATE_COLOR[state]))
            t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)
            if not rows:
                t.setRowCount(1)
                t.setItem(0, 0, self._cell("empty", DIM))
                t.setItem(0, 1, self._cell("No tracks yet. Use “Add track”."))

        def _fill_tracks(self, tracks, report):
            counts = {row["id"]: row["completions"] for row in (report["tracks"] if report else [])}
            t = self.t_tracks; t.setRowCount(len(tracks))
            for i, tr in enumerate(tracks):
                name_item = self._cell(tr["title"], CYAN)
                self._tag(name_item, tr["id"], tr["title"])
                t.setItem(i, 0, name_item)
                t.setItem(i, 1, self._cell(f"every {tr['cadence_days']}d"))
                t.setItem(i, 2, self._cell(f"{tr['grace_days']}d"))
                t.setItem(i, 3, self._cell(counts.get(tr["id"], 0)))
                t.setItem(i, 4, self._cell(tr["created_utc"]))
            t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)

        def _fill_history(self, journal, tracks):
            names = {tr["id"]: tr["title"] for tr in tracks}
            t = self.t_history; t.setRowCount(len(journal))
            for i, r in enumerate(reversed(journal)):
                t.setItem(i, 0, self._cell(r["recorded_utc"]))
                t.setItem(i, 1, self._cell(names.get(r["track_id"], "(removed track)"),
                                           INK if r["track_id"] in names else DIM))
                t.setItem(i, 2, self._cell(r["done_utc"], GREEN))
                t.setItem(i, 3, self._cell(r["note"] or "—"))
            t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)

        def _next_revision_path(self):
            base = self.orbit
            stem = base.stem
            if ".r" in stem and stem.rsplit(".r", 1)[-1].isdigit():
                stem = stem.rsplit(".r", 1)[0]
            rev = (self.data["revision"] + 1) if self.data else 1
            candidate = base.with_name(f"{stem}.r{rev}{base.suffix}")
            n = rev
            while candidate.exists():
                n += 1
                candidate = base.with_name(f"{stem}.r{n}{base.suffix}")
            return candidate

        def _set_enabled(self, on):
            self.views.setEnabled(on)

        def _warn(self, message, icon=None, title="Orbit"):
            box = QMessageBox(self); box.setWindowTitle(title)
            box.setIcon(icon or QMessageBox.Icon.Warning); box.setText(message); box.exec()

        def _animate(self):
            self.phase += 0.05
            self.header.tick(self.phase)

    return QApplication, Window


STYLE = f"""
#body {{ background: {BG}; }}
QWidget {{ color: {INK}; font-family: Helvetica, Arial, sans-serif; font-size: 13px; }}
#status {{ color: {MUTED}; font-size: 12px; }}
#foot {{ color: {DIM}; font-size: 11px; }}
#mono {{ background: {PANEL_HI}; border: 1px solid {LINE}; border-radius: 9px; color: {INK};
         font-family: "Courier New", monospace; font-size: 13px; padding: 8px; }}
QTableWidget {{ background: {PANEL_HI}; alternate-background-color: {PANEL}; border: 1px solid {LINE};
                border-radius: 10px; gridline-color: {LINE}; }}
QTableWidget::item {{ padding: 4px 6px; }}
QTableWidget::item:selected {{ background: {VIOLET}; color: {INK}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; padding: 6px 8px; border: none;
                        border-bottom: 1px solid {LINE}; font-weight: bold; }}
QTableCornerButton::section {{ background: {PANEL}; border: none; }}
QTabWidget#tabs::pane {{ border: 1px solid {LINE}; border-radius: 12px; background: {PANEL}; }}
QTabBar::tab {{ background: {PANEL_HI}; color: {MUTED}; padding: 8px 18px; margin-right: 6px;
                border-top-left-radius: 9px; border-top-right-radius: 9px; font-weight: bold; }}
QTabBar::tab:selected {{ color: {INK}; border-bottom: 2px solid {PINK}; }}
QPushButton#go {{ background: {PINK}; color: {BG}; border: none; border-radius: 10px; font-weight: bold; padding: 9px 16px; }}
QPushButton#go:hover {{ background: {CYAN}; }}
QPushButton#pick {{ background: {PANEL_HI}; border: 1px solid {LAVENDER}; border-radius: 9px; color: {INK};
                    font-weight: bold; padding: 9px 14px; }}
QPushButton#pick:hover {{ background: #1b2150; }}
"""


def run_app() -> int:
    try:
        from PyQt6.QtWidgets import QApplication  # noqa: F401
    except Exception:
        print("PyQt6 is required for the Orbit window. Use 'envy-orbit' subcommands instead.")
        return 2
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("No display found. Use 'envy-orbit init/add/log/status/export' in a terminal instead.")
        return 2
    try:
        QApplication, Window = _build()
        app = QApplication.instance() or QApplication(sys.argv)
        window = Window(); window.show()
        return app.exec()
    except Exception as exc:  # pragma: no cover
        print(f"Could not open the window: {exc}")
        return 2
