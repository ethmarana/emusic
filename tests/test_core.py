import io
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import backend
import library

CLIENT = 'a' * 32
TRACK_ID = 'a' * 22


@pytest.fixture
def temp_client(tmp_path, monkeypatch):
    monkeypatch.setattr(library, 'APP', tmp_path / 'config')
    monkeypatch.setattr(library, 'CACHE', tmp_path / 'cache')
    return library.LibraryClient(CLIENT, token_file=tmp_path / 'config/token.json', cache_dir=tmp_path / 'cache',
                                 transport=lambda *a, **k: {})


def token(client, user='test-user'):
    client.user_id = user
    client.update_token({'access_token': 'access-test', 'refresh_token': 'refresh-test', 'expires_in': 3600})


def test_track_and_playlist_normalization():
    t = {'item': {'type': 'track', 'uri': 'spotify:track:' + TRACK_ID, 'name': 'Song',
                  'artists': [{'name': 'Artist'}], 'duration_ms': 195000,
                  'album': {'name': 'Album', 'images': [{'url': 'https://i.scdn.co/image/example'}]}}}
    assert library.normalize_track(t)['title'] == 'Song'
    assert library.normalize_track(t)['duration'] == 195
    assert library.normalize_track({'track': t['item']})['artist'] == 'Artist'
    assert library.normalize_track({'item': None})['title'] == 'Unavailable track'
    assert library.normalize_track({'is_local': True, 'track': t['item']})['local'] is True
    p = library.normalize_playlist({'id': TRACK_ID, 'name': 'Mine', 'owner': {'id': 'test-user'},
                                    'items': {'total': 12}})
    assert p['total'] == 12
    assert p['owner'] == 'test-user'


def test_one_page_only_and_2026_endpoint(temp_client):
    c = temp_client
    token(c)
    calls = []
    def transport(url, **kwargs):
        calls.append(url)
        return {'items': [{'item': {'name': 'Song', 'uri': 'spotify:track:' + TRACK_ID}}], 'total': 2000}
    c.transport = transport
    p = {'id': TRACK_ID, 'owner': c.user_id, 'collaborative': False}
    result = c.tracks(p, 50)
    assert result['total'] == 2000
    assert len(calls) == 1
    assert '/playlists/' + TRACK_ID + '/items?' in calls[0]
    assert 'offset=50' in calls[0]
    assert c.tracks(p, 50)['source'] == 'cache'
    assert len(calls) == 1
    c.tracks(None, 0)
    assert len(calls) == 2
    assert '/me/tracks?' in calls[1]


def test_restricted_playlist_is_not_fetched(temp_client):
    c = temp_client
    token(c)
    calls = []
    c.transport = lambda *a, **k: calls.append(a)
    with pytest.raises(library.APIError) as exc:
        c.tracks({'id': TRACK_ID, 'owner': 'another-user', 'collaborative': False})
    assert exc.value.status == 403
    assert calls == []
    assert backend.spotify_uri('spotify:playlist:' + TRACK_ID)


def test_rate_limit_pauses_and_uses_cache(temp_client):
    c = temp_client
    token(c)
    calls = []
    def transport(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {'items': [{'track': {'name': 'Cached'}}], 'total': 1}
        raise library.APIError('Rate limited', 429, '', 120)
    c.transport = transport
    assert c.tracks()['source'] == 'live'
    assert c.tracks(force=True)['source'] == 'stale'
    assert len(calls) == 2
    assert c.cooldown_until > time.time() + 110
    assert c.tracks(force=True)['source'] == 'stale'
    assert len(calls) == 2
    with pytest.raises(library.APIError) as exc:
        c.playlists()
    assert exc.value.status == 429
    c2 = library.LibraryClient(CLIENT, token_file=c.token_file, cache_dir=c.cache_dir, transport=transport)
    assert c2.cooldown_until > time.time()
    c2.user_id = c.user_id
    assert c2.tracks(force=True)['source'] == 'stale'
    assert len(calls) == 2


def test_http_429_parsing_no_retry():
    calls = []
    def opener(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 429, 'Too Many Requests',
                                     {'Retry-After': '28'}, io.BytesIO(b'{"error":{"message":"API rate limit exceeded"}}'))
    with pytest.raises(library.APIError) as exc:
        library.request_json('https://api.spotify.com/v1/me/tracks', opener=opener)
    assert exc.value.status == 429
    assert exc.value.retry_after == 28
    assert len(calls) == 1
    with pytest.raises(library.APIError):
        library.request_json('https://evil.example/steal', opener=opener)
    assert len(calls) == 1


def test_cooldown_isolated_by_client_id(temp_client):
    c = temp_client
    c.cooldown_until = time.time() + 60
    c._save_cooldown()
    c.set_client_id('b' * 32)
    assert c.cooldown_until == 0
    assert not c.authenticated()


def test_pkce_local_callback(temp_client, monkeypatch):
    c = temp_client
    calls = []
    def transport(url, method='GET', data=None, **kwargs):
        calls.append((url, method, data))
        if url.endswith('/api/token'):
            assert data['code_verifier']
            assert data['grant_type'] == 'authorization_code'
            assert 'client_secret' not in data
            return {'access_token': 'test-access', 'refresh_token': 'test-refresh', 'expires_in': 3600}
        if url.endswith('/me'):
            return {'id': 'test-user', 'display_name': 'Test'}
        raise AssertionError(url)
    c.transport = transport
    browser = []
    def open_browser(url):
        browser.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query['code_challenge_method'] == ['S256']
        assert 'user-library-read' in query['scope'][0]
        callback = library.REDIRECT + '?' + urllib.parse.urlencode({'code': 'test-code', 'state': query['state'][0]})
        def visit():
            for _ in range(30):
                try:
                    urllib.request.urlopen(callback, timeout=2).read()
                    return
                except OSError:
                    time.sleep(.03)
            raise AssertionError('OAuth listener not available')
        threading.Thread(target=visit, daemon=True).start()
    profile = c.authorize(open_browser, timeout=5)
    assert profile['id'] == 'test-user'
    assert c.authenticated()
    assert (c.token_file.stat().st_mode & 0o777) == 0o600
    assert len(browser) == 1
    c.disconnect()
    assert not c.token_file.exists()


def test_modern_hyprland_move_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv('HYPRLAND_INSTANCE_SIGNATURE', 'test-instance')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(backend.shutil, 'which', lambda name: '/fake/' + name)
    window = {'address': '0xabc', 'class': 'Spotify', 'workspace': {'id': 3, 'name': '3'}}
    calls = []
    def fake_eval(code):
        calls.append(code)
        if 'hl.dsp.window.move' in code:
            args = code.split('workspace=', 1)[1].split(',window=', 1)[0]
            name = json.loads(args)
            window['workspace'] = {'name': name, 'id': -1 if name.startswith('special:') else int(name)}
        return 'ok'
    monkeypatch.setattr(backend, 'eval_lua', fake_eval)
    monkeypatch.setattr(backend, 'spotify_windows', lambda: [window])
    monkeypatch.setattr(backend, 'hypr', lambda *args: subprocess.CompletedProcess(args, 0, '{"id":3}', ''))
    manager = backend.WindowManager()
    manager.activate()
    assert manager.enabled
    assert window['workspace']['name'] == backend.HIDDEN
    assert manager.originals['0xabc'] == '3'
    manager.restore()
    assert window['workspace']['name'] == '3'
    assert not manager.enabled
    assert not manager.state_file.exists()
    assert any('hl.dsp.window.move' in code for code in calls)
    assert not any('movetoworkspacesilent' in code for code in calls)
    with pytest.raises(ValueError):
        backend.move_window('0xabc;rm -rf /', '3')
    assert backend.workspace_for({'workspace': {'id': -1337, 'name': 'music'}}) == 'name:music'


def test_python_sources_and_launcher():
    import ast
    root = Path(__file__).resolve().parents[1]
    for file in ('app.py', 'backend.py', 'library.py'):
        ast.parse((root / file).read_text())
    subprocess.run(['bash', '-n', str(root/'install.sh')], check=True)
    subprocess.run(['bash', '-n', str(root/'neon-music')], check=True)
    source = (root/'app.py').read_text()
    assert 'Qt.PenCapStyle.Round,' not in source
    assert 'Qt.PenJoinStyle.Round)' not in source
    assert 'QColor(27, 29, 32, 220)' in source
    assert '#a78bfa' not in source
    assert 'hl.window_rule' in (root/'hyprland.lua').read_text()
