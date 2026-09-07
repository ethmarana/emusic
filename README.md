# EMUSIC

A floating, monochrome Spotify controller for Arch Linux and modern Hyprland Lua. The official desktop client remains the playback engine. This is a personal, read-only library browser, not a replacement streaming service.

## What is included

- Frameless, resizable 930 × 640 floating dashboard with charcoal translucent glass, a quiet white starfield, Orbitron branding, and silver controls.
- Album art, song and artist, live progress, seeking, volume, previous/next, play/pause, shuffle and repeat through local MPRIS/playerctl.
- Liked Songs, your playlists, paginated track browsing, filtering loaded tracks and double-click playback. Playlist artwork is cached locally.
- Optional Spotify Web API connection for library data. Only selected pages are requested, one at a time. No background full-library crawl or Web API playback polling.
- A scoped Hyprland Lua rule that hides the official Spotify window on `special:neon-music-backend`. It is installed before Spotify is launched and is disabled when the widget closes. Existing Spotify windows are returned to their original workspaces.
- A visible `Show/Hide Spotify` button and a `--restore` recovery command. The widget never closes or kills the official player automatically.

## Install / upgrade

You already have playerctl and PyQt6. If necessary:

```bash
sudo pacman -S --needed playerctl python-pyqt6
```

Extract the ZIP, then run:

```bash
cd ~/Downloads
unzip neon-music-mono.zip
cd neon-music-v2
bash install.sh
```

The installer installs into `~/.local/share/neon-music`, `~/.local/bin/neon-music`, `~/.config/neon-music`, and the per-user desktop applications directory. It backs up the previous widget source and Lua rule if present. It does not modify the main Hyprland config, Waybar, the official Spotify config, spotify-player, or existing Spotify authentication. It preserves the new widget's settings and token files during upgrades.

If your previous Lua edition is already installed, **keep the dofile line you added earlier**. It now points to the updated rule file. If it is missing, add this line once to the bottom of `~/.config/hypr/hyprland.lua`:

```lua
dofile(os.getenv("HOME") .. "/.config/neon-music/hyprland.lua")
```

Do not add an old `source = ...` line. Run `hyprctl reload` once to remove the old fixed-size widget rule. The updated rule does not force a size or position, so you can resize and drag the window normally. The widget dynamically activates the backend rule before starting Spotify; merely sourcing the file does not hide Spotify.

Launch:

```bash
~/.local/bin/neon-music
```

The application launcher entry is also named **NEON MUSIC / MONO**. If another copy is already running, the launcher focuses it instead of creating a second instance. Close the old version before upgrading. If Spotify is already running, the widget reuses it; otherwise it starts the official app after activating the hiding rule. The official client may still need its own initial login or update window before playback is ready.

## Connect playlists and Liked Songs

MPRIS does not expose your Spotify library. Reading playlists therefore requires an optional, independently authorized Spotify Web API connection. This app does not use the spotify-player cache, copy its credentials, or embed somebody else's developer Client ID.

1. Open the widget's gear button. Use your existing Spotify developer app if it is eligible, or create one through Spotify's developer dashboard if your account permits it. Do not create additional apps merely to evade a rate limit.
2. Add the exact redirect URI `http://127.0.0.1:8765/callback` to that app's redirect URI settings.
3. Enter your app's 32-character Client ID in the widget and click **Connect library**. No client secret is required or requested. Your browser will open Spotify's authorization page using OAuth Authorization Code with PKCE.
4. Grant the read-only library permissions if you agree. The widget requests `user-library-read`, `playlist-read-private`, and `playlist-read-collaborative`. It does not request playlist editing, account management, or Web API playback permissions.

Access and refresh tokens are stored in `~/.config/neon-music/token.json` with owner-only file permissions. The local OAuth listener binds only to 127.0.0.1 and validates a random state value. You can disconnect from Settings, which deletes the widget's token file. Spotify itself remains logged in. Cached library pages are retained until you remove them yourself.

### Current Spotify limitations

Spotify's February 2026 development-mode changes restrict playlist item access to playlists you own or collaborate on. Followed Spotify/editorial playlists may be visible by name but their tracks may be unavailable to your developer app. Liked Songs is a separate endpoint. Existing extended-quota apps may have different access. Spotify also requires Premium for development-mode app owners and imposes app/user limits.

A 429 is not something this widget can bypass. On 429, it respects Retry-After when supplied, otherwise pauses requests conservatively; developer quota errors use a longer cooldown. The cooldown is persisted, and previously cached pages can remain available. There is no automatic retry storm. A 403 or unavailable endpoint is reported instead of silently pretending a playlist is empty. The official Spotify app's local playback remains usable even if the library connection is denied or throttled.

For a restricted playlist, **Play collection** sends its URI to the official client through MPRIS. **Open in Spotify** explicitly reveals the official window if you need Spotify's own library UI. Double-clicking an individual listed song opens that song locally; this may not preserve the original playlist queue. Use Play collection to begin in playlist context. Some Spotify versions do not support every OpenUri operation, so unsupported requests are reported rather than silently falling back to the Web API.

## Window behavior and recovery

The new Lua rule is scoped to the exact official `Spotify` class. It does not hide Chromium, other music apps, dialogs, or your widget. The official window is moved to the named hidden special workspace; it is not killed and remains available through MPRIS. Closing NEON MUSIC restores any Spotify windows it moved, without closing playback.

If the widget crashes while Spotify is hidden, recover it with:

```bash
~/.local/bin/neon-music --restore
```

This disables the temporary backend rule and restores Spotify windows, focusing the last restored one. A `Show/Hide Spotify` header button provides the same visible/hidden toggle while the app is running. A widget restart can also recover the saved workspace state. The restore state is scoped to the current Hyprland instance and stored in your runtime directory.

If you want to revert the original compact widget, the installer keeps a timestamped backup in `~/.local/share/neon-music/backup-*`. The v2 installer does not delete your previous files. Remove the single dofile line only if you want to stop loading NEON MUSIC's Lua rules entirely.

## Files and customization

- `app.py`: PyQt6 UI, monochrome glass, playback panel, playlist and track browsing.
- `backend.py`: local playerctl commands and modern Hyprland Lua window management.
- `library.py`: read-only Web API client, PKCE authentication, cache and rate-limit handling.
- `hyprland.lua`: scoped floating-widget rule and controllable hidden-backend rule.
- `neon-music`: launcher using Arch's `/usr/bin/python`.
- `install.sh`: non-destructive per-user installer.

The source uses no third-party Python packages beyond PyQt6. Its network client uses Python's standard library. All normal playback controls are local; album-art URLs are fetched directly from Spotify's CDN and cached separately. No audio files are downloaded or decrypted by the widget.

## Testing and troubleshooting

The Python source, shell scripts, library parsing, cooldown behavior, OAuth flow and mocked Hyprland window operations were checked during development. The GUI and actual Spotify/Hyprland integration could not be launched in the build environment, so this is a tested source bundle rather than a claim of a live end-to-end test on your machine.

For an error, run:

```bash
~/.local/bin/neon-music
```

and copy the traceback. For a Lua rule error, use `hyprctl configerrors` and `hyprctl -j clients`. For a library error, inspect the message inside the widget; do not repeatedly reset authentication or hammer the refresh button. The app never needs root access after installing its normal Arch dependencies.

Official references: https://wiki.hypr.land/configuring/core/rules/window-rules/ ; https://wiki.hypr.land/Configuring/Basics/Dispatchers/ ; https://developer.spotify.com/documentation/web-api/concepts/rate-limits ; https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide ; https://developer.spotify.com/documentation/web-api/concepts/authorization
