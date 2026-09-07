#!/usr/bin/env python3
"""NEON MUSIC 2 / MONO: floating local Spotify controller and read-only library."""
import json
import re
import os
import queue
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from PyQt6.QtCore import QByteArray, QObject, QRectF, QSize, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (QApplication, QAbstractItemView, QDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import backend
from library import APIError, APP, LibraryClient, image_url, read_json, atomic_json, PAGE_SIZE, REDIRECT, SCOPES

CONFIG_FILE = APP / 'settings.json'
SETTINGS = read_json(CONFIG_FILE, {}) or {}
BG = QColor(27, 29, 32, 220)
TEXT = '#f2f2f2'
MUTED = '#a5a6a8'
FONT = 'Inter'
MONO = 'JetBrains Mono'
HEAD = 'Orbitron'

STYLE = '''
* { font-family: Inter, sans-serif; color: #ededed; font-size: 12px; }
QWidget#root { background: transparent; }
QFrame#panel { background: rgba(49,51,54,155); border: 1px solid rgba(255,255,255,19); border-radius: 12px; }
QFrame#footer { background: rgba(42,44,47,185); border: 1px solid rgba(255,255,255,22); border-radius: 13px; }
QLabel#muted { color: #a5a6a8; }
QLabel#eyebrow { color: #a5a6a8; font-family: 'JetBrains Mono'; font-size: 10px; letter-spacing: 1px; }
QLabel#heading { font-size: 18px; font-weight: 600; }
QLabel#brand { font-family: Orbitron; font-size: 12px; font-weight: 600; letter-spacing: 2px; }
QLabel#track { font-size: 15px; font-weight: 600; }
QPushButton { background: transparent; color: #d1d1d1; border: 1px solid transparent; border-radius: 7px; padding: 7px 10px; }
QPushButton:hover { background: rgba(255,255,255,18); color: white; }
QPushButton:pressed { background: rgba(255,255,255,28); }
QPushButton:disabled { color: #77787a; }
QPushButton#primary { background: #e5e5e5; color: #202124; border: 1px solid #eeeeee; font-weight: 600; }
QPushButton#primary:hover { background: white; }
QPushButton#primary:disabled { background: #66676a; color: #a5a5a5; border-color: #66676a; }
QPushButton#secondary { background: rgba(255,255,255,9); border: 1px solid rgba(255,255,255,22); }
QPushButton#icon { font-size: 17px; padding: 3px; min-width: 28px; min-height: 28px; }
QLineEdit { background: rgba(12,13,15,55); border: 1px solid rgba(255,255,255,24); border-radius: 7px; padding: 8px 10px; selection-background-color: #707174; }
QLineEdit:focus { border: 1px solid rgba(255,255,255,75); }
QListWidget, QTableWidget { background: transparent; border: none; outline: none; }
QListWidget::item { padding: 7px 8px; border-radius: 7px; margin: 1px 3px; }
QListWidget::item:hover, QTableWidget::item:hover { background: rgba(255,255,255,12); }
QListWidget::item:selected, QTableWidget::item:selected { background: rgba(255,255,255,25); color: white; }
QTableWidget { gridline-color: transparent; alternate-background-color: rgba(255,255,255,3); }
QHeaderView::section { background: transparent; border: none; border-bottom: 1px solid rgba(255,255,255,23); color: #999b9d; font-size: 10px; padding: 7px; text-align: left; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 2px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,35); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0; background: transparent; }
QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,27); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #d8d8d8; border-radius: 2px; }
QSlider::handle:horizontal { background: #f1f1f1; width: 10px; margin: -4px 0; border-radius: 5px; }
QDialog { background: #252628; }
'''


def label(text='', kind='', size=None):
    w = QLabel(text)
    if kind:
        w.setObjectName(kind)
    if size:
        w.setFont(QFont(FONT, size))
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def button(text, callback=None, kind='secondary', tooltip=''):
    b = QPushButton(text)
    b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if callback:
        b.clicked.connect(callback)
    if tooltip:
        b.setToolTip(tooltip)
    return b


def row(*widgets, spacing=8):
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        if w is None:
            layout.addStretch(1)
        elif isinstance(w, QHBoxLayout) or isinstance(w, QVBoxLayout):
            layout.addLayout(w)
        else:
            layout.addWidget(w)
    return layout


def fmt_time(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 60}:{seconds % 60:02}' if seconds < 3600 else f'{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}'


class Jobs(QObject):
    done = pyqtSignal(str, object, object)
    browse = pyqtSignal(str)
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.stopping = threading.Event()
        self.latest = {}
        self.latest_lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name='neon-library').start()

    def submit(self, tag, func):
        kind = tag.split(':', 1)[0]
        with self.latest_lock:
            self.latest[kind] = tag
        self.q.put((tag, func))

    def _run(self):
        while not self.stopping.is_set():
            try:
                tag, func = self.q.get(timeout=.25)
            except queue.Empty:
                continue
            if self.stopping.is_set():
                break
            with self.latest_lock:
                if self.latest.get(tag.split(':', 1)[0]) != tag:
                    continue
            try:
                value, error = func(), None
            except Exception as exc:
                value, error = None, exc
            self.done.emit(tag, value, error)

    def close(self):
        self.stopping.set()


class PlayerThread(QObject):
    changed = pyqtSignal(dict)
    failed = pyqtSignal(str)
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.stopping = threading.Event()
        threading.Thread(target=self._run, daemon=True, name='neon-mpris').start()

    def command(self, *args):
        self.q.put(args)

    def _run(self):
        next_poll = 0
        while not self.stopping.is_set():
            try:
                args = self.q.get(timeout=max(.05, next_poll - time.monotonic()))
                if self.stopping.is_set():
                    break
                try:
                    backend.control(*args)
                except (RuntimeError, ValueError) as exc:
                    self.failed.emit(str(exc))
                next_poll = 0
                continue
            except queue.Empty:
                pass
            if self.stopping.is_set():
                break
            self.changed.emit(backend.snapshot())
            next_poll = time.monotonic() + 1

    def close(self):
        self.stopping.set()


class ImageLoader(QObject):
    ready = pyqtSignal(str, QPixmap)
    def __init__(self):
        super().__init__()
        self.net = QNetworkAccessManager(self)
        self.cache = {}
        self.pending = set()
        self.q = []
        self.active = 0
        self.disk = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'neon-music/art'
        self.disk.mkdir(parents=True, exist_ok=True)

    def request(self, url):
        if not url or url in self.cache or url in self.pending:
            return
        parsed = urlsplit(url)
        if parsed.scheme == 'file':
            pix = QPixmap(QUrl(url).toLocalFile())
            if not pix.isNull():
                self.cache[url] = pix
                self.ready.emit(url, pix)
            return
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or not (host == 'scdn.co' or host.endswith('.scdn.co') or host == 'spotifycdn.com' or host.endswith('.spotifycdn.com')):
            return
        import hashlib
        path = self.disk / hashlib.sha256(url.encode()).hexdigest()
        if path.exists():
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.cache[url] = pix
                self.ready.emit(url, pix)
                return
        self.pending.add(url)
        self.q.append((url, path))
        self._pump()

    def _pump(self):
        while self.q and self.active < 3:
            url, path = self.q.pop(0)
            self.active += 1
            req = QNetworkRequest(QUrl(url))
            req.setTransferTimeout(12000)
            req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
            reply = self.net.get(req)
            reply.finished.connect(lambda r=reply, u=url, p=path: self._finish(r, u, p))

    def _finish(self, reply, url, path):
        self.active -= 1
        self.pending.discard(url)
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = bytes(reply.readAll())
            if len(data) <= 3_000_000:
                pix = QPixmap()
                if pix.loadFromData(data):
                    self.cache[url] = pix
                    try:
                        path.write_bytes(data)
                    except OSError:
                        pass
                    self.ready.emit(url, pix)
        reply.deleteLater()
        self._pump()


class Glass(QWidget):
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 31), 1))
        p.setBrush(BG)
        rounded = QPainterPath()
        rounded.addRoundedRect(QRectF(self.rect().adjusted(1, 1, -1, -1)), 18, 18)
        p.drawPath(rounded)
        p.setClipPath(rounded)
        # A restrained, deterministic starfield: no animated or network assets.
        import random
        rng = random.Random(1907)
        p.setPen(Qt.PenStyle.NoPen)
        for _ in range(68):
            x = rng.randrange(max(1, self.width()))
            y = rng.randrange(max(1, self.height()))
            p.setBrush(QColor(255, 255, 255, rng.randrange(8, 35)))
            radius = rng.choice((0.5, 0.6, 0.8, 1.0))
            p.drawEllipse(QRectF(x, y, radius, radius))
        p.end()


class Art(QLabel):
    def __init__(self, size=92):
        super().__init__()
        self.size_px = size
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet('background: rgba(255,255,255,10); border: 1px solid rgba(255,255,255,20); border-radius: 9px; color: #777; font-size: 27px;')
        self.setText('♫')

    def set_image(self, pix):
        if pix.isNull():
            return
        target = self.size_px
        scaled = pix.scaled(target, target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        x, y = (scaled.width()-target)//2, (scaled.height()-target)//2
        clipped = scaled.copy(x, y, target, target)
        final = QPixmap(target, target)
        final.fill(Qt.GlobalColor.transparent)
        p = QPainter(final)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, target, target), 9, 9)
        p.setClipPath(clip)
        p.drawPixmap(0, 0, clipped)
        p.end()
        self.setText('')
        self.setPixmap(final)

    def clear_image(self):
        self.clear()
        self.setText('♫')


class SettingsDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle('NEON MUSIC / Library connection')
        self.setMinimumWidth(490)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(12)
        lay.addWidget(label('LIBRARY CONNECTION', 'eyebrow'))
        lay.addWidget(label('Connect your Spotify library', 'heading'))
        info = label('Playback works locally without this connection. To browse playlists and Liked Songs, use your own Spotify developer app with read-only permissions.')
        info.setWordWrap(True)
        lay.addWidget(info)
        self.client = QLineEdit(main.client.client_id)
        self.client.setPlaceholderText('Spotify Client ID (32 hex characters)')
        lay.addWidget(self.client)
        details = label('Add this exact redirect URI to your Spotify app:')
        details.setObjectName('muted')
        lay.addWidget(details)
        redirect = QLineEdit(REDIRECT)
        redirect.setReadOnly(True)
        lay.addWidget(redirect)
        note = label('Scopes: ' + SCOPES + '\nSpotify Premium and developer-app access are required. New app quotas and playlist restrictions still apply.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        lay.addWidget(note)
        self.state = label('', 'muted')
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.connect_button = button('Connect library', self.connect_library, 'primary')
        dashboard = button('Developer dashboard ↗', lambda: QDesktopServices.openUrl(QUrl('https://developer.spotify.com/dashboard')), 'secondary')
        lay.addLayout(row(self.connect_button, dashboard))
        lay.addLayout(row(button('Disconnect library', self.disconnect), None, button('Close', self.close)))

    def connect_library(self):
        client_id = self.client.text().strip()
        if not re.fullmatch(r'[0-9a-fA-F]{32}', client_id):
            self.state.setText('Enter the 32-character Client ID from your Spotify developer app.')
            return
        SETTINGS['client_id'] = client_id
        atomic_json(CONFIG_FILE, SETTINGS)
        self.main.reset_library('Connecting your library…')
        self.connect_button.setEnabled(False)
        self.state.setText('Waiting for authorization in your browser…')
        def connect():
            self.main.client.set_client_id(client_id)
            return self.main.client.authorize(self.main.jobs.browse.emit,
                                               cancelled=self.main.jobs.stopping)
        self.main.jobs.submit('auth', connect)

    def finished_auth(self, error):
        self.connect_button.setEnabled(True)
        self.state.setText(str(error) if error else 'Connected. Your library is ready.')
        if not error:
            self.close()

    def disconnect(self):
        self.main.client.disconnect()
        self.main.reset_library('Library disconnected. Local playback still works.')
        self.state.setText('Disconnected. Cached pages are retained for offline use.')


class DragHeader(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.drag_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window.windowHandle()
            if handle is not None and not handle.startSystemMove():
                self.drag_pos = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self.drag_pos)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('NEON MUSIC / MONO')
        self.setMinimumSize(760, 510)
        self.resize(930, 640)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.client = LibraryClient(SETTINGS.get('client_id', ''))
        self.jobs = Jobs()
        self.jobs.done.connect(self.on_job)
        self.jobs.browse.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        self.player = PlayerThread()
        self.player.changed.connect(self.on_player)
        self.player.failed.connect(lambda text: self.notice(text))
        self.images = ImageLoader()
        self.images.ready.connect(self.on_image)
        self.windows = backend.WindowManager()
        self.settings_dialog = None
        self.generation = 0
        self.playlist_generation = 0
        self.playlists = []
        self.playlist_total = 0
        self.playlist_offset = 0
        self.current_playlist = None
        self.track_offset = 0
        self.track_total = 0
        self.tracks = []
        self.state = {}
        self.position_time = time.monotonic()
        self.art_url = ''
        self._drag_pos = None
        self._volume_dragging = False
        self._build()
        self._restore_geometry()
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.tick)
        self.clock.start(250)
        self.window_timer = QTimer(self)
        self.window_timer.timeout.connect(self.windows.hide)
        self.window_timer.start(2500)
        self.windows.activate()
        if self.windows.last_error:
            self.notice(self.windows.last_error)
        try:
            self.windows.start_spotify()
        except RuntimeError as exc:
            self.notice(str(exc))
        if self.client.authenticated():
            self.jobs.submit('identity', self.client.identity)
        else:
            self.reset_library('Connect your library to browse playlists and Liked Songs.')

    def _build(self):
        root = Glass()
        root.setObjectName('root')
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)
        self.brand = label('N E O N  /  M O N O', 'brand')
        self.brand.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.connection = label('LOCAL · SPOTIFY', 'eyebrow')
        self.connection.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.connection.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        top = row(self.brand, None, self.connection,
                  button('▭', self.toggle_spotify, 'icon', 'Show or hide the official Spotify window'),
                  button('⚙', self.open_settings, 'icon', 'Library settings'),
                  button('−', self.showMinimized, 'icon', 'Minimize widget'),
                  button('×', self.close, 'icon', 'Close widget'))
        self.header = DragHeader(self)
        self.header.setLayout(top)
        outer.addWidget(self.header)
        body = QHBoxLayout()
        body.setSpacing(10)
        outer.addLayout(body, 1)
        sidebar = QFrame()
        sidebar.setObjectName('panel')
        sidebar.setFixedWidth(226)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(10, 14, 10, 10)
        side.setSpacing(9)
        side.addWidget(label('YOUR LIBRARY', 'eyebrow'))
        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText('Filter playlists…')
        self.library_search.textChanged.connect(self.filter_playlists)
        side.addWidget(self.library_search)
        self.liked = button('♡   Liked Songs', self.select_liked, 'secondary')
        self.liked.setStyleSheet('text-align: left; padding: 10px 8px;')
        side.addWidget(self.liked)
        side.addWidget(label('PLAYLISTS', 'eyebrow'))
        self.playlist_list = QListWidget()
        self.playlist_list.setIconSize(QSize(30,30))
        self.playlist_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.playlist_list.currentItemChanged.connect(self.select_playlist)
        side.addWidget(self.playlist_list, 1)
        self.more_playlists = button('Load more playlists', self.load_more_playlists, 'secondary')
        self.more_playlists.setEnabled(False)
        side.addWidget(self.more_playlists)
        self.library_state = label('LOCAL LIBRARY', 'eyebrow')
        self.library_state.setWordWrap(True)
        side.addWidget(self.library_state)
        body.addWidget(sidebar)

        main = QFrame()
        main.setObjectName('panel')
        content = QVBoxLayout(main)
        content.setContentsMargins(16, 14, 16, 12)
        content.setSpacing(10)
        self.eyebrow = label('LIBRARY / SELECT A COLLECTION', 'eyebrow')
        content.addWidget(self.eyebrow)
        self.heading = label('Your music', 'heading')
        self.count = label('', 'muted')
        content.addLayout(row(self.heading, None, self.count))
        self.message = label('Choose a playlist or Liked Songs.', 'muted')
        self.message.setWordWrap(True)
        self.message.hide()
        content.addWidget(self.message)
        self.play_collection = button('▶  Play collection', self.play_collection_uri, 'primary')
        self.refresh = button('↻  Refresh', self.refresh_tracks, 'secondary')
        content.addLayout(row(self.play_collection, self.refresh, None,
                              button('Open in Spotify ↗', self.open_collection, 'secondary')))
        self.track_search = QLineEdit()
        self.track_search.setPlaceholderText('Filter loaded tracks…')
        self.track_search.textChanged.connect(self.filter_tracks)
        content.addWidget(self.track_search)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['#', 'TITLE', 'ARTIST', 'TIME'])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 45)
        self.table.setColumnWidth(3, 58)
        self.table.verticalHeader().setDefaultSectionSize(37)
        self.table.cellDoubleClicked.connect(self.play_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.track_menu)
        content.addWidget(self.table, 1)
        self.page_info = label('', 'eyebrow')
        self.prev_page = button('← Previous', lambda: self.load_tracks(max(0, self.track_offset-PAGE_SIZE)), 'secondary')
        self.next_page = button('Next →', lambda: self.load_tracks(self.track_offset+PAGE_SIZE), 'secondary')
        self.prev_page.setEnabled(False)
        self.next_page.setEnabled(False)
        content.addLayout(row(self.page_info, None, self.prev_page, self.next_page))
        body.addWidget(main, 1)

        footer = QFrame()
        footer.setObjectName('footer')
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(13, 12, 16, 12)
        foot.setSpacing(15)
        self.art = Art(94)
        foot.addWidget(self.art)
        right = QVBoxLayout()
        right.setContentsMargins(0,0,0,0)
        right.setSpacing(5)
        self.now_title = label('Spotify is starting…', 'track')
        self.now_title.setMinimumWidth(60)
        self.now_artist = label('Official desktop player', 'muted')
        right.addLayout(row(self.now_title, None, self.now_artist))
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setEnabled(False)
        self.seek.sliderReleased.connect(self.seek_to)
        self.seek.sliderMoved.connect(self.preview_seek)
        self.elapsed = label('0:00', 'eyebrow')
        self.remaining = label('0:00', 'eyebrow')
        self.elapsed.setFixedWidth(42)
        self.remaining.setFixedWidth(42)
        right.addLayout(row(self.elapsed, self.seek, self.remaining))
        self.previous = button('⏮', lambda: self.player.command('previous'), 'icon', 'Previous track')
        self.play_button = button('▶', lambda: self.player.command('play-pause'), 'primary', 'Play / pause')
        self.next = button('⏭', lambda: self.player.command('next'), 'icon', 'Next track')
        self.shuffle = button('SHUFFLE', lambda: self.player.command('shuffle', 'Toggle'), 'secondary')
        self.repeat = button('REPEAT', self.toggle_repeat, 'secondary')
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0,100)
        self.volume.setFixedWidth(82)
        self.volume.setValue(70)
        self.volume.sliderPressed.connect(lambda: setattr(self, '_volume_dragging', True))
        self.volume.sliderReleased.connect(lambda: setattr(self, '_volume_dragging', False))
        self.volume.sliderReleased.connect(lambda: self.player.command('volume', str(self.volume.value()/100)))
        right.addLayout(row(self.shuffle, None, self.previous, self.play_button, self.next, None,
                            self.repeat, label('VOL', 'eyebrow'), self.volume))
        foot.addLayout(right, 1)
        outer.addWidget(footer)
        self.status = label('MPRIS · LOCAL PLAYBACK · NO API POLLING', 'eyebrow')
        outer.addWidget(self.status)
        self.setStyleSheet(STYLE)
        self.setAccessibleName('NEON MUSIC monochrome player')

    def _restore_geometry(self):
        raw = SETTINGS.get('geometry')
        if raw:
            try:
                if self.restoreGeometry(QByteArray.fromBase64(raw.encode())):
                    return
            except Exception:
                pass
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x()-self.width()//2, screen.center().y()-self.height()//2)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 52:
            if not self.windowHandle().startSystemMove():
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def notice(self, text):
        self.status.setText(text)
        self.status.setToolTip(text)

    def set_message(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def reset_library(self, message):
        self.generation += 1
        self.playlist_generation += 1
        self.playlists.clear()
        self.playlist_list.clear()
        self.tracks.clear()
        self.table.setRowCount(0)
        self.current_playlist = None
        self.heading.setText('Your music')
        self.count.clear()
        self.library_state.setText('NOT CONNECTED')
        self.more_playlists.setEnabled(False)
        self.play_collection.setEnabled(False)
        self.refresh.setEnabled(False)
        self.set_message(message)
        self.page_info.clear()

    def open_settings(self):
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self)
            self.settings_dialog.destroyed.connect(lambda: setattr(self, 'settings_dialog', None))
            self.settings_dialog.show()
        else:
            self.settings_dialog.raise_()
            self.settings_dialog.activateWindow()

    def on_job(self, tag, result, error):
        if tag == 'auth':
            if self.settings_dialog is not None:
                try:
                    self.settings_dialog.finished_auth(error)
                except RuntimeError:
                    pass
            if not error:
                self.on_connected(result)
            else:
                self.notice(str(error))
            return
        if tag == 'identity':
            if error:
                self.notice(str(error))
                self.reset_library('Library connection needs attention. Open settings to reconnect.')
            else:
                self.on_connected(result)
            return
        if tag.startswith('playlists:'):
            if int(tag.split(':')[1]) != self.playlist_generation:
                return
            self.more_playlists.setEnabled(True)
            if error:
                self.more_playlists.setEnabled(False)
                self.library_state.setText('LIBRARY PAUSED')
                self.notice(str(error))
                self.set_message(str(error))
                return
            self.playlists.extend(result['items'])
            self.playlist_total = result['total']
            self.playlist_offset = result['offset']
            self.populate_playlists()
            self.more_playlists.setEnabled(len(self.playlists) < self.playlist_total)
            self.library_state.setText(f'{len(self.playlists)} / {self.playlist_total} PLAYLISTS')
            self.source_notice(result['source'])
            return
        if tag.startswith('tracks:'):
            if int(tag.split(':')[1]) != self.generation:
                return
            self.refresh.setEnabled(True)
            if error:
                self.set_message(str(error))
                self.notice(str(error))
                self.table.setRowCount(0)
                self.page_info.setText('UNAVAILABLE')
                self.play_collection.setEnabled(self.client.authenticated())
                return
            self.tracks = result['items']
            self.track_offset = result['offset']
            self.track_total = result['total']
            self.populate_tracks()
            self.source_notice(result['source'])

    def source_notice(self, source):
        if source == 'stale':
            self.notice('CACHED / ' + self.client.last_warning)
        elif source == 'cache':
            self.notice('CACHED LIBRARY · LOCAL PLAYBACK')
        else:
            self.notice('LIBRARY SYNCED · LOCAL PLAYBACK')

    def on_connected(self, profile):
        self.connection.setText('LOCAL · ' + (profile.get('display_name') or 'SPOTIFY').upper()[:22])
        self.library_state.setText('LIBRARY CONNECTED')
        self.refresh.setEnabled(True)
        self.select_liked()
        self.load_more_playlists(reset=True)

    def load_more_playlists(self, reset=False):
        if not self.client.authenticated():
            self.open_settings()
            return
        if reset:
            self.playlist_generation += 1
            self.playlists.clear()
            self.playlist_list.clear()
            self.playlist_total = 0
            offset = 0
        else:
            offset = len(self.playlists)
        self.more_playlists.setEnabled(False)
        self.library_state.setText('LOADING PLAYLISTS…')
        gen = self.playlist_generation
        self.jobs.submit(f'playlists:{gen}', lambda: self.client.playlists(offset))

    def populate_playlists(self):
        current = self.current_playlist and self.current_playlist.get('id')
        self.playlist_list.blockSignals(True)
        self.playlist_list.clear()
        for p in self.playlists:
            item = QListWidgetItem(p['name'])
            item.setData(Qt.ItemDataRole.UserRole, p)
            item.setToolTip(p['name'] + '\n' + p.get('owner_name',''))
            item.setSizeHint(QSize(0, 43))
            if p.get('art') in self.images.cache:
                item.setIcon(self.icon_for(self.images.cache[p['art']], 30))
            elif p.get('art'):
                self.images.request(p['art'])
            self.playlist_list.addItem(item)
            if p.get('id') == current:
                self.playlist_list.setCurrentItem(item)
        self.playlist_list.blockSignals(False)
        self.filter_playlists(self.library_search.text())

    @staticmethod
    def icon_for(pix, size):
        return QIcon(pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation))

    def filter_playlists(self, text):
        query = text.casefold().strip()
        for i in range(self.playlist_list.count()):
            item = self.playlist_list.item(i)
            item.setHidden(query not in item.text().casefold())

    def select_liked(self):
        self.playlist_list.blockSignals(True)
        self.playlist_list.clearSelection()
        self.playlist_list.setCurrentItem(None)
        self.playlist_list.blockSignals(False)
        self.current_playlist = None
        self.heading.setText('Liked Songs')
        self.eyebrow.setText('LIBRARY / YOUR COLLECTION')
        self.load_tracks(0)

    def select_playlist(self, item, previous=None):
        if not item:
            return
        p = item.data(Qt.ItemDataRole.UserRole)
        self.current_playlist = p
        self.heading.setText(p['name'])
        self.eyebrow.setText('PLAYLIST / ' + (p.get('owner_name') or 'SPOTIFY').upper()[:45])
        self.load_tracks(0)

    def load_tracks(self, offset=0, force=False):
        self.generation += 1
        gen = self.generation
        self.track_offset = offset
        self.tracks = []
        self.table.setRowCount(0)
        self.set_message('Loading this page…')
        self.page_info.setText('LOADING…')
        self.prev_page.setEnabled(False)
        self.next_page.setEnabled(False)
        self.refresh.setEnabled(False)
        self.play_collection.setEnabled(self.client.authenticated())
        if not self.client.authenticated():
            self.set_message('Connect your library in settings. Local playback is available without an API connection.')
            self.refresh.setEnabled(False)
            return
        playlist = self.current_playlist
        self.jobs.submit(f'tracks:{gen}', lambda: self.client.tracks(playlist, offset, force))

    def refresh_tracks(self):
        self.load_tracks(self.track_offset, force=True)

    def populate_tracks(self):
        self.table.setRowCount(len(self.tracks))
        for i, t in enumerate(self.tracks):
            values = [str(self.track_offset+i+1), t['title'], t['artist'], fmt_time(t['duration'])]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, t)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif col == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(t['title'] + '\n' + t['artist'] + '\n' + t['album'])
                self.table.setItem(i, col, item)
        self.filter_tracks(self.track_search.text())
        self.set_message('' if self.tracks else 'No tracks on this page.')
        self.count.setText(f'{self.track_total:,} tracks')
        start = self.track_offset+1 if self.tracks else 0
        self.page_info.setText(f'{start}–{self.track_offset+len(self.tracks)} / {self.track_total:,}')
        self.prev_page.setEnabled(self.track_offset > 0)
        self.next_page.setEnabled(self.track_offset+len(self.tracks) < self.track_total)

    def filter_tracks(self, text):
        q = text.casefold().strip()
        for i, t in enumerate(self.tracks):
            self.table.setRowHidden(i, q not in (t['title']+' '+t['artist']+' '+t['album']).casefold())

    def play_row(self, row, column=0):
        if row < 0 or row >= len(self.tracks):
            return
        track = self.tracks[row]
        if not track['uri'] or track['local']:
            self.notice('This local or unavailable track cannot be opened through the widget.')
            return
        self.open_uri(track['uri'])

    def track_menu(self, position):
        from PyQt6.QtWidgets import QMenu
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        menu = QMenu(self)
        action = menu.addAction('Play this track')
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen == action:
            self.play_row(row)

    def open_uri(self, uri):
        try:
            self.player.command('open', backend.spotify_uri(uri))
            self.notice('Opening in the local Spotify player…')
        except ValueError as exc:
            self.notice(str(exc))

    def play_collection_uri(self):
        if self.current_playlist:
            self.open_uri(self.current_playlist.get('uri',''))
        else:
            self.open_uri('spotify:collection:tracks')

    def open_collection(self):
        self.windows.show_spotify()
        self.window_timer.stop()
        uri = self.current_playlist.get('uri','') if self.current_playlist else 'spotify:collection:tracks'
        self.open_uri(uri)
        self.notice('Official Spotify is visible. Use the window button to hide it again.')

    def toggle_spotify(self):
        if self.windows.enabled:
            self.windows.show_spotify()
            self.window_timer.stop()
            self.notice('Official Spotify is visible. Click the window button again to hide it.')
        else:
            self.windows.activate()
            self.window_timer.start(2500)
            self.notice('Spotify hidden on the dedicated backend workspace.' if self.windows.enabled else self.windows.last_error)

    def toggle_repeat(self):
        current = self.state.get('loop', 'None')
        next_mode = {'None': 'Playlist', 'Playlist': 'Track', 'Track': 'None'}.get(current, 'Playlist')
        self.player.command('loop', next_mode)

    def on_player(self, data):
        old = self.state.get('trackid')
        self.state = data
        self.position_time = time.monotonic()
        playing = data['status'] == 'Playing'
        available = data['status'] != 'Unavailable'
        self.play_button.setText('Ⅱ' if playing else '▶')
        for w in (self.previous, self.play_button, self.next, self.shuffle, self.repeat):
            w.setEnabled(available)
        self.now_title.setText(data['title'] or ('Nothing playing' if available else 'Connecting to Spotify…'))
        self.now_artist.setText(data['artist'] or ('Ready' if available else 'Official desktop player'))
        self.now_title.setToolTip(data['title'])
        self.now_artist.setToolTip(data['artist'] + '\n' + data['album'])
        if data['art'] != self.art_url:
            self.art_url = data['art']
            self.art.clear_image()
            self.images.request(self.art_url)
        self.shuffle.setText('SHUFFLE ●' if data.get('shuffle', '').lower() == 'on' else 'SHUFFLE')
        self.repeat.setText({'Track': 'REPEAT 1', 'Playlist': 'REPEAT ●'}.get(data.get('loop'), 'REPEAT'))
        if not self._volume_dragging and data.get('volume'):
            try:
                self.volume.setValue(max(0, min(100, round(float(data['volume'])*100))))
            except ValueError:
                pass
        self.seek.setEnabled(available and data['duration'] > 0)
        if not self.seek.isSliderDown():
            self.update_progress()
        if old != data.get('trackid') and data.get('trackid'):
            self.connection.setToolTip(data['title']+' — '+data['artist'])

    def tick(self):
        if self.state and not self.seek.isSliderDown():
            self.update_progress()

    def update_progress(self):
        duration = self.state.get('duration',0)
        position = self.state.get('position',0)
        if self.state.get('status') == 'Playing':
            position += time.monotonic()-self.position_time
        position = max(0, min(duration, position))
        self.elapsed.setText(fmt_time(position))
        self.remaining.setText(fmt_time(duration))
        self.seek.blockSignals(True)
        self.seek.setValue(int(position/duration*1000) if duration else 0)
        self.seek.blockSignals(False)

    def preview_seek(self, value):
        self.elapsed.setText(fmt_time(self.state.get('duration',0)*value/1000))

    def seek_to(self):
        duration = self.state.get('duration',0)
        if duration > 0:
            self.player.command('position', f'{duration*self.seek.value()/1000:.3f}')

    def on_image(self, url, pix):
        if url == self.art_url:
            self.art.set_image(pix)
        for i in range(self.playlist_list.count()):
            item = self.playlist_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if data.get('art') == url:
                item.setIcon(self.icon_for(pix,30))

    def closeEvent(self, event):
        self.clock.stop()
        self.window_timer.stop()
        SETTINGS['geometry'] = bytes(self.saveGeometry().toBase64()).decode()
        try:
            atomic_json(CONFIG_FILE, SETTINGS)
        except OSError:
            pass
        self.jobs.close()
        self.player.close()
        self.windows.restore()
        super().closeEvent(event)


def main():
    if '--restore' in sys.argv:
        backend.WindowManager().restore(focus=True)
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName('neon-music')
    app.setDesktopFileName('neon-music')
    app.setOrganizationName('NEON MUSIC')
    app.setStyle('Fusion')
    from PyQt6.QtCore import QLockFile
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', str(Path.home()/'.cache')))
    lock = QLockFile(str(runtime/'neon-music.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            try:
                backend.eval_lua('hl.dispatch(hl.dsp.focus({window="class:^(neon-music|neon_music)$"}))')
            except RuntimeError:
                pass
        return 0
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    result = app.exec()
    lock.unlock()
    return result


if __name__ == '__main__':
    sys.exit(main())
