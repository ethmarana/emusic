"""Read-only Spotify library client. No streaming, client secret, or API playback.
Only the explicitly requested page is fetched. Network operations run off the UI thread.
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

APP = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'neon-music'
CACHE = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'neon-music/library'
REDIRECT = 'http://127.0.0.1:8765/callback'
SCOPES = 'user-library-read playlist-read-private playlist-read-collaborative'
API_ROOT = 'https://api.spotify.com/v1'
PAGE_SIZE = 50


class APIError(Exception):
    def __init__(self, message, status=0, reason='', retry_after=0):
        super().__init__(message)
        self.status, self.reason, self.retry_after = status, reason, retry_after


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(5) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def image_url(images):
    return next((im.get('url', '') for im in images or [] if isinstance(im, dict) and im.get('url')), '')


def normalize_track(item):
    """Accept old .track and 2026 .item wrappers, without assuming optional fields exist."""
    item = item if isinstance(item, dict) else {}
    t = item.get('item') or item.get('track') or item
    if not isinstance(t, dict):
        t = {}
    album = t.get('album') or {}
    return {
        'uri': t.get('uri') or '', 'title': t.get('name') or 'Unavailable track',
        'artist': ', '.join(a.get('name', '') for a in t.get('artists') or [] if isinstance(a, dict)),
        'album': album.get('name') or '', 'art': image_url(album.get('images')),
        'duration': (t.get('duration_ms') or 0) / 1000,
        'type': t.get('type') or '', 'local': bool(item.get('is_local') or t.get('is_local')),
    }


def normalize_playlist(p):
    owner = p.get('owner') or {}
    contents = p.get('items') or p.get('tracks') or {}
    return {
        'id': p.get('id') or '', 'uri': p.get('uri') or '',
        'name': p.get('name') or 'Untitled playlist',
        'owner': owner.get('id') or '', 'owner_name': owner.get('display_name') or owner.get('id') or '',
        'collaborative': bool(p.get('collaborative')), 'art': image_url(p.get('images')),
        'total': contents.get('total'), 'snapshot': p.get('snapshot_id') or '',
    }


def request_json(url, method='GET', data=None, headers=None, timeout=20, opener=None):
    """Strict HTTPS request, with explicit 429 handling and no automatic retry loops."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('api.spotify.com', 'accounts.spotify.com'):
        raise APIError('Blocked an unexpected API destination.')
    body = None if data is None else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except (ValueError, UnicodeError):
            payload = {}
        error = payload.get('error') or {}
        if not isinstance(error, dict):
            error = {'message': str(error)}
        reason = error.get('reason') or ''
        retry = exc.headers.get('Retry-After', '0')
        try:
            retry = max(0, min(86400, int(float(retry))))
        except (ValueError, TypeError):
            retry = 0
        if exc.code == 429:
            message = ('Spotify developer quota exceeded. Library requests are paused.' if reason == 'QUOTA_EXCEEDED'
                       else 'Spotify rate-limited the library. Cached pages remain available.')
        elif exc.code == 403:
            message = 'Spotify denied this endpoint. Check app access, scopes, or playlist ownership.'
        elif exc.code == 401:
            message = 'Spotify authorization expired or was rejected. Reconnect your library.'
        else:
            message = str(error.get('message') or error.get('error_description') or f'Spotify HTTP {exc.code}')
        raise APIError(message, exc.code, reason, retry) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise APIError('Network connection failed. Check your connection and try again.') from exc


class LibraryClient:
    def __init__(self, client_id='', token_file=None, cache_dir=None, clock=None, transport=None):
        self.client_id = client_id.strip()
        self.token_file = Path(token_file or APP / 'token.json')
        self.cache_dir = Path(cache_dir or CACHE)
        self.clock = clock or time.time
        self.transport = transport or request_json
        self.lock = threading.RLock()
        self.token = read_json(self.token_file, {}) or {}
        self.user_id = self.token.get('user_id', '')
        self.cooldown_until = 0
        self.cooldown_reason = ''
        self.last_request = 0
        self.last_warning = ''
        self._load_cooldown()

    def _load_cooldown(self):
        data = read_json(APP / 'cooldown.json', {}) or {}
        self.cooldown_until = 0
        self.cooldown_reason = ''
        if data.get('client_id') == self.client_id:
            self.cooldown_until = data.get('until', 0)
            self.cooldown_reason = data.get('reason', '')

    def _save_cooldown(self):
        atomic_json(APP / 'cooldown.json', {'client_id': self.client_id, 'until': self.cooldown_until,
                                           'reason': self.cooldown_reason})

    def _save_token(self):
        atomic_json(self.token_file, self.token)

    def update_token(self, data):
        with self.lock:
            previous = self.token
            self.token = dict(data)
            self.token['expires_at'] = self.clock() + max(0, int(data.get('expires_in', 3600)))
            if not data.get('refresh_token'):
                self.token['refresh_token'] = previous.get('refresh_token', '')
            self.token['client_id'] = self.client_id
            self.token['user_id'] = self.user_id
            self._save_token()

    def set_client_id(self, value):
        value = value.strip()
        if not value or not all(c in '0123456789abcdefABCDEF' for c in value) or len(value) != 32:
            raise APIError('Enter the 32-character Client ID from your Spotify developer app.')
        self.client_id = value
        if self.token.get('client_id') != value:
            self.token, self.user_id = {}, ''
        self._load_cooldown()

    def authenticated(self):
        return bool(self.client_id and self.token.get('refresh_token'))

    def access_token(self, force=False):
        if not self.client_id:
            raise APIError('Connect your library with your Spotify developer Client ID.')
        if self.token.get('client_id') != self.client_id:
            raise APIError('Connect your library again for this Client ID.')
        if not force and self.token.get('access_token') and self.token.get('expires_at', 0) > self.clock() + 60:
            return self.token['access_token']
        refresh = self.token.get('refresh_token')
        if not refresh:
            raise APIError('Connect your library to Spotify first.')
        data = self.transport('https://accounts.spotify.com/api/token', method='POST', data={
            'grant_type': 'refresh_token', 'refresh_token': refresh, 'client_id': self.client_id})
        self.update_token(data)
        return self.token['access_token']

    def authorize(self, open_browser, timeout=180, cancelled=None):
        """Called in a worker thread; open_browser is a queued UI-thread signal."""
        if not self.client_id:
            raise APIError('Enter a Client ID first.')
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        state = secrets.token_urlsafe(32)
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path != '/callback' or query.get('state', [''])[0] != state:
                    self.send_error(400, 'Invalid callback')
                    return
                result['code'] = query.get('code', [''])[0]
                result['error'] = query.get('error', [''])[0]
                content = b'<html><body style="background:#202124;color:#fff;font:16px sans-serif;padding:40px">Authorization received. You can return to NEON MUSIC.</body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        try:
            server = HTTPServer(('127.0.0.1', 8765), Handler)
        except OSError as exc:
            raise APIError('Could not open localhost port 8765. Close another authorization attempt and try again.') from exc
        try:
            server.timeout = 1
            url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
                'client_id': self.client_id, 'response_type': 'code', 'redirect_uri': REDIRECT,
                'code_challenge_method': 'S256', 'code_challenge': challenge,
                'scope': SCOPES, 'state': state})
            open_browser(url)
            deadline = time.monotonic() + timeout
            while not result and time.monotonic() < deadline and not (cancelled and cancelled.is_set()):
                server.handle_request()
            if result.get('error'):
                raise APIError('Spotify authorization was cancelled or denied.')
            if not result.get('code'):
                raise APIError('Authorization timed out. Try connecting again.')
            self.user_id = ''
            data = self.transport('https://accounts.spotify.com/api/token', method='POST', data={
                'grant_type': 'authorization_code', 'code': result['code'], 'redirect_uri': REDIRECT,
                'client_id': self.client_id, 'code_verifier': verifier})
            self.update_token(data)
            profile, _ = self.get('/me', force=True)
            self.user_id = profile['id']
            self._save_token()
            return profile
        finally:
            server.server_close()

    def _cache_path(self, key):
        namespace = hashlib.sha256((self.client_id + ':' + self.user_id).encode()).hexdigest()[:24]
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / namespace / (digest + '.json')

    def cache_read(self, key):
        return read_json(self._cache_path(key))

    def _cooldown_check(self):
        if self.cooldown_until > self.clock():
            remaining = int(self.cooldown_until - self.clock()) + 1
            kind = 'Developer quota' if self.cooldown_reason == 'QUOTA_EXCEEDED' else 'Rate limit'
            raise APIError(f'{kind} pause active. Try again in {remaining}s, or use cached pages.',
                           429, self.cooldown_reason, remaining)

    def get(self, path, params=None, force=False, ttl=600):
        if not path.startswith('/') or path.startswith('//'):
            raise APIError('Invalid API path.')
        query = urllib.parse.urlencode(sorted((params or {}).items()))
        key = path + ('?' + query if query else '')
        with self.lock:
            cached = self.cache_read(key) if self.user_id else None
            if cached and not force and cached.get('saved_at', 0) + ttl > self.clock():
                return cached['data'], 'cache'
            try:
                self._cooldown_check()
            except APIError as exc:
                if cached:
                    self.last_warning = str(exc)
                    return cached['data'], 'stale'
                raise
            # One request at a time, with a small minimum gap. Never bulk-download the library.
            delay = max(0, 1.5 - (time.monotonic() - self.last_request)) if self.last_request else 0
            if delay:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                token = self.access_token()
                url = API_ROOT + path + ('?' + query if query else '')
                try:
                    data = self.transport(url, headers={'Authorization': 'Bearer ' + token})
                except APIError as exc:
                    if exc.status != 401:
                        raise
                    token = self.access_token(force=True)
                    data = self.transport(url, headers={'Authorization': 'Bearer ' + token})
                if not isinstance(data, dict):
                    raise APIError('Spotify returned an unexpected response.')
                self.last_warning = ''
                if self.user_id:
                    atomic_json(self._cache_path(key), {'saved_at': self.clock(), 'data': data})
                return data, 'live'
            except APIError as exc:
                if exc.status == 429:
                    self.cooldown_reason = exc.reason
                    self.cooldown_until = self.clock() + (exc.retry_after or (3600 if exc.reason == 'QUOTA_EXCEEDED' else 60))
                    self._save_cooldown()
                if cached:
                    self.last_warning = str(exc)
                    return cached['data'], 'stale'
                raise

    def identity(self):
        profile, _ = self.get('/me', force=True)
        uid = profile.get('id')
        if not uid:
            raise APIError('Spotify did not return an account ID.')
        if uid != self.user_id:
            self.user_id = uid
            self._save_token()
        return profile

    def playlists(self, offset=0, force=False):
        data, source = self.get('/me/playlists', {'limit': PAGE_SIZE, 'offset': offset}, force)
        return {'items': [normalize_playlist(p) for p in data.get('items') or [] if p],
                'total': data.get('total', 0), 'offset': offset, 'source': source}

    def tracks(self, playlist=None, offset=0, force=False):
        if playlist:
            if playlist.get('owner') != self.user_id and not playlist.get('collaborative'):
                raise APIError('Spotify restricts track browsing for followed playlists you do not own or collaborate on. Open this playlist in Spotify instead.', 403)
            pid = playlist['id']
            path = f'/playlists/{pid}/items'
            try:
                data, source = self.get(path, {'limit': PAGE_SIZE, 'offset': offset}, force)
            except APIError as exc:
                # Older extended-quota apps may still expose the legacy endpoint.
                if exc.status != 404:
                    raise
                data, source = self.get(f'/playlists/{pid}/tracks', {'limit': PAGE_SIZE, 'offset': offset}, force)
        else:
            data, source = self.get('/me/tracks', {'limit': PAGE_SIZE, 'offset': offset}, force)
        if 'items' not in data:
            raise APIError('Spotify did not provide track items for this collection. This may be a developer-app access restriction.', 403)
        return {'items': [normalize_track(t) for t in data.get('items') or [] if t],
                'total': data.get('total', 0), 'offset': offset, 'source': source}

    def disconnect(self):
        self.token, self.user_id = {}, ''
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass
        # Cached data is retained on disk for offline use; clear it separately if desired.
