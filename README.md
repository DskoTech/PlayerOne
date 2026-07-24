# PlayerOne

A standalone controller-friendly frontend for VLC Media Player.

## Requirements
- Windows 10/11
- Python 3.9+ (only needed if not using the built .exe)
- An Xbox / XInput compatible controller **or** an SDL3-supported gamepad
- VLC Media Player (the installer can download it for you)

## Quick Start

```
python install.py
```

The installer will:
1. Check your Python version
2. Install pip dependencies (`pyinstaller`, `pywin32`)
3. Detect or download VLC
4. Write `vlc_controller_config.json`
5. Build `dist/PlayerOne.exe`
6. Create Start Menu / Desktop shortcuts (optional)
7. Add to Windows startup (optional)

---

## Controller Map

| Button | Action |
|---|---|
| **START** | Open / close main menu |
| **SELECT** | Open / close context/right-click menu |
| **X** | Open / close filter·sort menu |
| **Y** | Toggle playlist queue panel |
| **A** | Confirm / Play·Pause *(0.5 s cooldown)* |
| **B** | Back / Stop / Cancel |
| **LB** | Previous track |
| **RB** | Next track |
| **L3** (left stick click) | Extended settings popup |
| **R3** (right stick click) | Fullscreen toggle |
| **Left stick ↑↓** | Navigate lists / menus |
| **Left stick ←→** | Navigate horizontal tabs |
| **Right stick ←→** | Seek ±5 s |
| **Right stick ↑↓** | Volume ±5% |

---

## Menu Structure

### Main Menu (START)
- Hide / Show top nav bar
- Playback controls
- Extended Settings shortcut
- **Quit at End of Playlist**
- **Quit**

### Context Menu (SELECT)
- Play / Stop
- Add / Remove from queue
- Loop · Shuffle · Repeat
- Open File / Open URL (launches virtual keyboard)

### Filter Menu (X)
- List / Grid view toggle
- Sort by Name / Date / Duration
- Filter options

### Playlist Panel (Y)
- Loop · Shuffle · Sort · Clear Queue controls sit **above** the queue list
- Navigate queue with left stick
- Press **B** to close

### Extended Settings (L3)
- Audio track, sync
- Video track, zoom, aspect ratio
- Subtitle track, sync

---

## Navigation Zones

```
┌────────────────────────────────────────────┐
│  Home  Video  Music  Browse  Discover      │  ← TOP NAV  (B to go here from sections)
├────────────────────────────────────────────┤
│  All  Playlists  Artists  Albums  Tracks…  │  ← SECTIONS (left stick ←→, B goes up, ↓ goes to content)
├────────────────────────────────────────────┤
│                                            │
│           CONTENT / MEDIA LIST             │  ← CONTENT (A = play, B goes up to sections)
│                                            │
├────────────────────────────────────────────┤
│  ⏮  ⏯  ⏹  ⏭      Now Playing   Vol: 80% │  ← PLAYBACK BAR
└────────────────────────────────────────────┘
```

When the Playlist Panel (Y) is open it docks on the right with queue controls above the list.

---

## How It Works

PlayerOne is a **pure front-end wrapper**. It:
- Starts `vlc.exe` as a subprocess with `--extraintf=http` and `--intf=dummy`
  (so VLC's own Qt UI never appears)
- Communicates with VLC's built-in HTTP JSON API on `127.0.0.1:8080` for all
  playback commands
- Handles all controller input itself via XInput (primary) or SDL3 (fallback)
- **Embeds VLC's actual video-output window** into PlayerOne's own window
  using Win32 `SetParent` — so video renders directly inside PlayerOne
  instead of a separate VLC window. This happens automatically a moment
  after playback starts, and the video area resizes with the window.
- Runs a watchdog thread that keeps VLC and PlayerOne's lifecycles in sync:
  **closing PlayerOne closes VLC**, and **if VLC exits or crashes, PlayerOne
  closes too.**

The password for the VLC HTTP interface is set to `vlccontroller` by default.  
You can change it in `vlc_controller_config.json`.

## Drag & Drop / Default Player

- Drag video, audio, or playlist files onto the PlayerOne window to queue
  and play them immediately.
- Files/folders passed as command-line arguments (`PlayerOne.exe "movie.mkv"`)
  are loaded and played on startup — this is what makes PlayerOne work when
  set as your default media player and you double-click a file in Explorer.
- On first launch, a setup wizard lets you associate video/audio/playlist
  file types with PlayerOne and choose whether to scan your Music/Videos/
  Pictures folders into the library. Re-trigger it any time by setting
  `"first_boot": true` in `vlc_controller_config.json`.

## Branding

PlayerOne ships with its own icon (`icon.ico`), used for the window title
bar, taskbar, and any file types you associate with it.

---

## Running from Source

```
python vlc_controller.py
```

No compilation needed. All dependencies are standard-library except `tkinter`
(bundled with CPython on Windows).

---

## File Layout

```
vlc_controller/
├── install.py            ← Run this first
├── vlc_controller.py     ← Main application
├── vlc_controller_config.json   ← Written by installer
├── icon.ico              ← Optional: place your own icon here
└── dist/
    └── PlayerOne.exe   ← Built by installer
```
