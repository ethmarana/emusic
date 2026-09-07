"""Official Spotify MPRIS controls and scoped Hyprland Lua window management."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

HIDDEN = 'special:neon-music-backend'
PLAYER = 'spotify'
SEP = '\x1f'
META = SEP.join(['{{title}}', '{{artist}}', '{{album}}', '{{mpris:artUrl}}', '{{mpris:length}}', '{{mpris:trackid}}'])
RULE_FILE = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'neon-music/hyprland.lua'


def run(args, timeout=5):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, '', str(exc))


def control(*args):
    result = run(['playerctl', '--player=' + PLAYER, *args])
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Spotify is not available through MPRIS.')
    return result.stdout.strip()


def snapshot():
    result = run(['playerctl', '--player=' + PLAYER, 'status'], timeout=3)
    status = result.stdout.strip() if result.returncode == 0 else 'Unavailable'
    data = dict(status=status, title='', artist='', album='', art='', duration=0., position=0., trackid='', shuffle='', loop='', volume='')
    if status == 'Unavailable':
        return data
    result = run(['playerctl', '--player=' + PLAYER, 'metadata', '--format', META], timeout=3)
    if result.returncode == 0:
        fields = result.stdout.rstrip('\n').split(SEP)
        fields += [''] * (6 - len(fields))
        data.update(zip(('title', 'artist', 'album', 'art', 'length', 'trackid'), fields[:6]))
        try:
            data['duration'] = max(0., float(data.pop('length') or 0) / 1_000_000)
        except ValueError:
            data['duration'] = 0.
    for key, command in (('position', 'position'), ('shuffle', 'shuffle'), ('loop', 'loop'), ('volume', 'volume')):
        result = run(['playerctl', '--player=' + PLAYER, command], timeout=3)
        if result.returncode == 0:
            if key == 'position':
                try:
                    data[key] = max(0., float(result.stdout.strip()))
                except ValueError:
                    pass
            else:
                data[key] = result.stdout.strip()
    return data


def spotify_uri(value):
    if re.fullmatch(r'spotify:(track|album|playlist):[A-Za-z0-9]{22}', value or ''):
        return value
    if value == 'spotify:collection:tracks':
        return value
    raise ValueError('Not a supported Spotify content URI.')


def hypr(*args):
    return run(['hyprctl', *args])


def eval_lua(code):
    result = hypr('eval', code)
    if result.returncode or result.stdout.lstrip().startswith('error:'):
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Hyprland rejected the Lua command.')
    return result.stdout.strip()


def clients():
    result = hypr('-j', 'clients')
    if result.returncode:
        return []
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        return []


def spotify_windows():
    return [w for w in clients() if (w.get('class') or w.get('initialClass') or '').lower() == 'spotify']


def workspace_for(w):
    ws = w.get('workspace') or {}
    name = ws.get('name') or ''
    if name.startswith('special:'):
        return name
    identifier = ws.get('id')
    if isinstance(identifier, int) and identifier > 0:
        return str(identifier)
    if name.startswith('name:'):
        return name
    if name:
        return 'name:' + name
    return '1'


def move_window(address, workspace, follow=False):
    if not re.fullmatch(r'0x[0-9a-fA-F]+', address or ''):
        raise ValueError('Invalid window address.')
    if not re.fullmatch(r'(?:special:[A-Za-z0-9_-]+|name:[A-Za-z0-9_-]+|[1-9][0-9]*)', workspace or ''):
        raise ValueError('Invalid workspace.')
    code = 'hl.dispatch(hl.dsp.window.move({workspace=' + json.dumps(workspace) + ',window=' + json.dumps('address:' + address) + ',follow=' + ('true' if follow else 'false') + '}))'
    return eval_lua(code)


class WindowManager:
    """Hide only official Spotify windows, preserving their original workspaces."""
    def __init__(self):
        self.originals = {}
        self.home_workspace = '1'
        self.enabled = False
        self.available = bool(os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') and shutil.which('hyprctl'))
        self.last_error = ''
        self.state_file = Path(os.environ.get('XDG_RUNTIME_DIR', str(Path.home() / '.cache'))) / 'neon-music-restore.json'
        self._read_state()

    def _read_state(self):
        try:
            data = json.loads(self.state_file.read_text())
            if data.get('instance') == os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
                self.originals = data.get('originals', {})
                self.home_workspace = data.get('home_workspace', '1')
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self):
        from library import atomic_json
        atomic_json(self.state_file, {'instance': os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'), 'originals': self.originals, 'home_workspace': self.home_workspace})

    @staticmethod
    def disable_rule():
        return eval_lua('if _G.neon_music_backend_rule then _G.neon_music_backend_rule:set_enabled(false) end')

    def activate(self):
        self.last_error = ''
        if not self.available:
            self.last_error = 'Hyprland is unavailable; automatic window hiding is disabled.'
            return
        result = hypr('-j', 'activeworkspace')
        try:
            ws = json.loads(result.stdout)
            self.home_workspace = str(ws.get('id') or 1)
        except (ValueError, TypeError):
            pass
        try:
            # The installed Lua file is idempotent and defines the scoped rule.
            # It is loaded BEFORE the official Spotify process is started.
            eval_lua('dofile(' + json.dumps(str(RULE_FILE)) + ')')
            eval_lua('_G.neon_music_backend_rule:set_enabled(true)')
            self.enabled = True
            self.hide()
        except (RuntimeError, ValueError) as exc:
            self.last_error = str(exc)
            self.enabled = False

    def hide(self):
        if not self.available or not self.enabled:
            return
        for w in spotify_windows():
            address = w.get('address', '')
            if not re.fullmatch(r'0x[0-9a-fA-F]+', address):
                continue
            if workspace_for(w) == HIDDEN:
                continue
            if address not in self.originals:
                original = workspace_for(w)
                if not re.fullmatch(r'(?:special:[A-Za-z0-9_-]+|name:[A-Za-z0-9_-]+|[1-9][0-9]*)', original):
                    original = self.home_workspace
                self.originals[address] = original
                self._save_state()
            try:
                move_window(address, HIDDEN)
            except (RuntimeError, ValueError) as exc:
                self.last_error = str(exc)

    def restore(self, focus=False):
        if not self.available:
            return
        self.enabled = False
        try:
            self.disable_rule()
        except RuntimeError as exc:
            self.last_error = str(exc)
        target = ''
        for w in spotify_windows():
            address = w.get('address', '')
            if not re.fullmatch(r'0x[0-9a-fA-F]+', address):
                continue
            if workspace_for(w) != HIDDEN:
                continue
            destination = self.originals.get(address, self.home_workspace)
            try:
                move_window(address, destination)
                target = address
            except (RuntimeError, ValueError) as exc:
                self.last_error = str(exc)
        if focus and target:
            try:
                eval_lua('hl.dispatch(hl.dsp.focus({window=' + json.dumps('address:' + target) + '}))')
            except RuntimeError as exc:
                self.last_error = str(exc)
        self.originals.clear()
        try:
            self.state_file.unlink()
        except OSError:
            pass

    def show_spotify(self):
        self.restore(focus=True)

    def start_spotify(self):
        if not shutil.which('spotify'):
            raise RuntimeError('The official Spotify desktop app is not installed or is not in PATH.')
        if run(['pgrep', '-x', 'spotify']).returncode == 0:
            return False
        subprocess.Popen(['spotify'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        return True
