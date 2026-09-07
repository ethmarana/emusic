-- NEON MUSIC / MONO. This file is safe to source repeatedly.
-- It never changes global opacity, borders, workspaces, or Waybar.
if _G.neon_music_widget_rule then
    _G.neon_music_widget_rule:set_enabled(false)
end
_G.neon_music_widget_rule = hl.window_rule({
    name = "neon-music-mono-widget",
    match = { class = "^(neon-music|neon_music)$" },
    float = true,
    center = true,
    border_size = 0,
    rounding = 18,
})

-- Only the exact official Spotify class is affected. The Python launcher
-- enables this rule before starting Spotify and disables it on exit.
if _G.neon_music_backend_rule then
    _G.neon_music_backend_rule:set_enabled(false)
end
_G.neon_music_backend_rule = hl.window_rule({
    name = "neon-music-mono-backend",
    match = { class = "^[Ss]potify$" },
    workspace = "special:neon-music-backend silent",
    no_initial_focus = true,
    focus_on_activate = false,
    suppress_event = "activate activatefocus",
})
_G.neon_music_backend_rule:set_enabled(false)
