"""
PlayerOne  v3.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Controller-friendly media front-end that wraps VLC.

New in v3.1
───────────
• Full media library module (media_library.py)
  - Persistent JSON metadata cache (~/.playerone/meta_cache.json)
  - ID3v2.3/2.4, MP4/M4A iTunes, OGG/FLAC Vorbis tag reading
  - Album-art extraction and cache
  - File-system watcher (polling) for incremental updates
  - Search across title/artist/album/filename
  - Sort-all by name/date/artist/album
  - Library settings panel with add/remove/rescan folder UI
• Search bar (controller: hold SELECT + left-stick, or click 🔍)
• Library status bar shows live scan progress
"""

import sys, os, time, threading, subprocess, socket, json, re, struct
import urllib.request, urllib.parse, urllib.error
import ctypes, ctypes.wintypes
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.filedialog as fd
from pathlib import Path

# ── import the media library module (sits next to this file) ─────────────────
try:
    import media_library as _ml
    _HAS_ML = True
except ImportError:
    _HAS_ML = False

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────
CFG = {
    "vlc_path":      "",
    "http_host":     "127.0.0.1",
    "http_port":     43210,   # uncommon port; 8080 collides with dev servers, HyperBeam, etc.
    "http_password": "vlccontroller",
    "volume_step":   5,
    "seek_step":     5,
    "cooldown_a":    0.5,
    "first_boot":    True,
    "library_paths": [],
    "scan_library":  True,
    "radio_feeds":   [
        {"name": "BBC World Service",   "url": "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"},
        {"name": "WNYC 93.9 FM",        "url": "https://fm939.wnyc.org/wnycfm-tunein.aac"},
        {"name": "NTS Radio 1",         "url": "https://stream-relay-geo.ntslive.net/stream"},
        {"name": "Soma FM – Groove Salad","url": "http://ice1.somafm.com/groovesalad-256-mp3"},
        {"name": "Classic FM",          "url": "https://media-ice.musicradio.com/ClassicFMMP3"},
        {"name": "Jazz24",              "url": "https://live.wostreaming.net/direct/ppm-jazz24aac-ibc1"},
        {"name": "Calm Radio – Classical","url": "https://streams.calmradio.com/api/42/128/stream"},
    ],
    "podcast_feeds": [
        {"name": "NASA Jet Propulsion Lab", "url": "https://www.jpl.nasa.gov/feeds/podcast"},
        {"name": "BBC In Our Time",         "url": "https://podcasts.files.bbci.co.uk/b006qykl.rss"},
    ],
}

def _config_dir():
    """Stable, writable per-user config directory (survives PyInstaller temp
    dirs, which is why first_boot used to reappear every launch)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = Path(base) / "PlayerOne"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        d = Path(base) / "playerone"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path(__file__).parent
    return d

CONFIG_PATH = _config_dir() / "playerone_config.json"

def _find_config():
    """Load config from the stable path, migrating a legacy file next to the
    exe/script if that's the only one present."""
    # stable location wins
    if CONFIG_PATH.exists():
        try:
            CFG.update(json.loads(CONFIG_PATH.read_text()))
            return CONFIG_PATH
        except Exception:
            pass
    # migrate a legacy config if we find one
    for base in (Path(sys.executable).parent, Path(__file__).parent):
        p = base / "playerone_config.json"
        if p.exists():
            try:
                CFG.update(json.loads(p.read_text()))
                CONFIG_PATH.write_text(json.dumps(CFG, indent=2))  # migrate forward
                return CONFIG_PATH
            except Exception:
                pass
    return CONFIG_PATH   # not found yet, but this is where we'll save

_find_config()

def _save_config():
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(CFG, indent=2))
    except Exception:
        pass

VLC_CANDIDATES = [
    CFG.get("vlc_path",""),
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    str(Path(sys.executable).parent / "vlc" / "vlc.exe"),
]

# ─────────────────────────────────────────────────────────
# MEDIA TYPES
# ─────────────────────────────────────────────────────────
VIDEO_EXTS = {
    ".mp4",".mkv",".avi",".mov",".wmv",".flv",".webm",".m4v",
    ".ts",".mts",".m2ts",".vob",".ogv",".3gp",".mpg",".mpeg",
    ".divx",".rmvb",".asf",".f4v",".h264",".hevc",".m2v",
}
AUDIO_EXTS = {
    ".mp3",".flac",".ogg",".wav",".aac",".m4a",".wma",".opus",
    ".ape",".mka",".aiff",".alac",".ac3",".dts",".tta",".wv",
}
IMAGE_EXTS = {".jpg",".jpeg",".png",".bmp",".gif",".webp",".tiff"}
PLAYLIST_EXTS = {".m3u",".m3u8",".xspf",".pls",".asx",".wpl",".cue"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | PLAYLIST_EXTS

# ─────────────────────────────────────────────────────────
# THEME
# ─────────────────────────────────────────────────────────
T = {
    "bg":        "#0d0d0d",
    "panel":     "#181818",
    "panel2":    "#1e1e1e",
    "card":      "#1a1a1a",
    "highlight": "#e8672a",
    "hl_text":   "#ffffff",
    "text":      "#f0f0f0",
    "subtext":   "#888888",
    "border":    "#2a2a2a",
    "row_alt":   "#141414",
    "popup_bg":  "#121212",
    "green":     "#3dba6f",
    "blue":      "#3d8eba",
    "red":       "#e84040",
}

# ─────────────────────────────────────────────────────────
# ICON
# ─────────────────────────────────────────────────────────
def _icon_path():
    for base in (Path(sys.executable).parent, Path(__file__).parent):
        p = base / "icon.ico"
        if p.is_file():
            return p
    return None

def _set_icon(win):
    p = _icon_path()
    if p:
        try: win.iconbitmap(str(p))
        except Exception: pass

# ─────────────────────────────────────────────────────────
# XINPUT
# ─────────────────────────────────────────────────────────
DPAD_UP=0x0001; DPAD_DOWN=0x0002; DPAD_LEFT=0x0004; DPAD_RIGHT=0x0008
BTN_START=0x0010; BTN_BACK=0x0020; L3=0x0040; R3=0x0080
LB=0x0100; RB=0x0200
BTN_A=0x1000; BTN_B=0x2000; BTN_X=0x4000; BTN_Y=0x8000
DEADZONE=10000

class _GP(ctypes.Structure):
    _fields_=[("wButtons",ctypes.wintypes.WORD),
              ("bLeftTrigger",ctypes.c_ubyte),("bRightTrigger",ctypes.c_ubyte),
              ("sThumbLX",ctypes.c_short),("sThumbLY",ctypes.c_short),
              ("sThumbRX",ctypes.c_short),("sThumbRY",ctypes.c_short)]

class _XS(ctypes.Structure):
    _fields_=[("dwPacketNumber",ctypes.wintypes.DWORD),("Gamepad",_GP)]

class XInput:
    MAX_SLOTS = 4   # XInput supports up to 4 controllers
    def __init__(self):
        self.ok=False; self.lib=None; self._s=_XS()
        for dll in ("xinput1_4.dll","xinput1_3.dll","xinput9_1_0.dll"):
            try: self.lib=ctypes.windll.LoadLibrary(dll); self.ok=True; break
            except (OSError, AttributeError): pass
    def _slot_state(self,i):
        if not self.ok: return None
        if self.lib.XInputGetState(i,ctypes.byref(self._s))!=0: return None
        g=self._s.Gamepad
        return g.wButtons,g.sThumbLX,g.sThumbLY,g.sThumbRX,g.sThumbRY
    def state(self,i=0):
        return self._slot_state(i)
    def states(self):
        """State of every connected slot (0-3).  Re-queried each call, so
        plugging/unplugging a pad is handled automatically."""
        out=[]
        if not self.ok: return out
        for i in range(self.MAX_SLOTS):
            s=self._slot_state(i)
            if s is not None: out.append(s)
        return out
    def count(self):
        return len(self.states())

class SDL3:
    _BTN={"a":0,"b":1,"x":2,"y":3,"back":4,"start":6,"ls":7,"rs":8,
          "lb":9,"rb":10,"du":11,"dd":12,"dl":13,"dr":14}
    def __init__(self):
        self.ok=False; self._sdl=None; self._gps=[]; self._open_ids=set()
        self._last_scan=0.0
        for n in ("SDL3.dll","libSDL3.so.0","libSDL3.dylib"):
            try: self._sdl=ctypes.CDLL(n); break
            except OSError: pass
        if not self._sdl: return
        try:
            self._sdl.SDL_Init(0x200)   # SDL_INIT_GAMEPAD
            self._scan()
        except Exception: pass
    def _scan(self):
        """(Re)enumerate gamepads so newly-plugged pads are picked up."""
        if not self._sdl: return
        try:
            if hasattr(self._sdl,"SDL_UpdateGamepads"):
                self._sdl.SDL_UpdateGamepads()
            cnt=ctypes.c_int(0)
            ids=self._sdl.SDL_GetGamepads(ctypes.byref(cnt))
            if ids and cnt.value>0:
                arr=ctypes.cast(ids,ctypes.POINTER(ctypes.c_uint32))
                for i in range(cnt.value):
                    gid=arr[i]
                    if gid not in self._open_ids:
                        gp=self._sdl.SDL_OpenGamepad(gid)
                        if gp:
                            self._gps.append(gp); self._open_ids.add(gid)
            self.ok=len(self._gps)>0
        except Exception: pass
    def _gp_state(self,gp):
        try:
            B=self._BTN
            def btn(b): return bool(self._sdl.SDL_GetGamepadButton(gp,b))
            def ax(a): return self._sdl.SDL_GetGamepadAxis(gp,a)
            bits=0
            if btn(B["du"]): bits|=DPAD_UP
            if btn(B["dd"]): bits|=DPAD_DOWN
            if btn(B["dl"]): bits|=DPAD_LEFT
            if btn(B["dr"]): bits|=DPAD_RIGHT
            if btn(B["start"]): bits|=BTN_START
            if btn(B["back"]):  bits|=BTN_BACK
            if btn(B["ls"]):    bits|=L3
            if btn(B["rs"]):    bits|=R3
            if btn(B["lb"]):    bits|=LB
            if btn(B["rb"]):    bits|=RB
            if btn(B["a"]):     bits|=BTN_A
            if btn(B["b"]):     bits|=BTN_B
            if btn(B["x"]):     bits|=BTN_X
            if btn(B["y"]):     bits|=BTN_Y
            return bits,ax(0),-ax(1),ax(2),-ax(3)
        except Exception:
            return None
    def states(self):
        if not self.ok: return []
        # cheap periodic rescan for hot-plugged pads
        now=time.time()
        if now-self._last_scan>2.0:
            self._last_scan=now; self._scan()
        elif hasattr(self._sdl,"SDL_UpdateGamepads"):
            try: self._sdl.SDL_UpdateGamepads()
            except Exception: pass
        out=[]
        for gp in list(self._gps):
            s=self._gp_state(gp)
            if s is not None: out.append(s)
        return out
    def count(self):
        return len(self._gps)
    def state(self,_=0):
        s=self.states()
        return s[0] if s else None


class ControllerHub:
    """
    Aggregates input from ALL connected controllers so any player's pad can
    drive the shared UI (this is a single-cursor couch app, so inputs are
    merged rather than split per player).

    - Button bits are OR-ed across every connected pad → any pad's press acts.
    - Each analog axis takes the largest-magnitude value across pads → whichever
      controller pushes a stick hardest wins that axis.
    - Both XInput (up to 4 slots) and SDL gamepads are polled; hot-plugging is
      handled because both backends re-enumerate while running.
    """
    def __init__(self):
        self.xi = XInput()
        # Only spin up SDL if XInput found nothing (avoids double-counting the
        # same pads through two APIs on Windows).
        self.sdl = None if self.xi.ok else SDL3()
        self.ok = self.xi.ok or (self.sdl and self.sdl.ok)
        self._last_count = -1

    def _all_states(self):
        states=[]
        states += self.xi.states()
        if self.sdl: states += self.sdl.states()
        return states

    def count(self):
        n=self.xi.count()
        if self.sdl: n+=self.sdl.count()
        return n

    def state(self):
        states=self._all_states()
        # report connect/disconnect changes once
        n=len(states)
        if n!=self._last_count:
            self._last_count=n
            print(f"[PlayerOne] Controllers active: {n}")
        if not states:
            return None
        btns=0; lx=ly=rx=ry=0
        for b,a,c,d,e in states:
            btns|=b
            if abs(a)>abs(lx): lx=a
            if abs(c)>abs(ly): ly=c
            if abs(d)>abs(rx): rx=d
            if abs(e)>abs(ry): ry=e
        return btns,lx,ly,rx,ry

# ─────────────────────────────────────────────────────────
# VLC HTTP API  —  works with VLC 3.x AND 4.x
# ─────────────────────────────────────────────────────────
#
# Reality check (verified against videolan/vlc master, i.e. the 4.x branch):
# VLC 4.x still ships the SAME Lua HTTP interface as 3.x.  There is no
# separate "/api/v2" REST server in shipping VLC — both major versions expose:
#
#   status:    GET /requests/status.json
#   command:   GET /requests/status.json?command=in_play&input=<uri>
#   playlist:  GET /requests/playlist.json
#
# Enabled the same way on both:  --extraintf=http --http-password=<pw>
# (empty username, HTTP Basic auth).  status.json returns a "version" field,
# so we read the real VLC version straight off the interface instead of
# guessing.
#
# We therefore use the Lua interface for BOTH versions.  A speculative REST
# path is kept only as a genuine last-ditch probe in case some exotic/future
# build ever exposes one — it is never the reason playback fails, because the
# direct-launch fallback (direct_play) covers a dead HTTP interface.
# ─────────────────────────────────────────────────────────

class VLCApi:
    LUA  = "lua"     # /requests/*.json  — VLC 3.x and 4.x
    REST = "rest"    # /api/v2/*         — experimental fallback only

    def __init__(self, host=None, port=None, password=None):
        h    = host     if host     is not None else CFG["http_host"]
        p    = port     if port     is not None else CFG["http_port"]
        pw   = password if password is not None else CFG["http_password"]
        self._host = h
        self._port = p
        self._pw   = pw
        self._iface   = None    # LUA / REST, detected lazily
        self._major   = None    # detected VLC major version (int) or None
        self.version_str   = ""     # full version string from the interface
        self.http_confirmed = False # True once a probe actually succeeded
        self._fallback_exe  = None  # vlc executable for direct-launch fallback
        self._404_hinted    = False

        # Lua interface opener (HTTP Basic auth, empty username)
        self._base_lua = f"http://{h}:{p}/requests"
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, f"http://{h}:{p}", "", pw)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(pm))

        # Experimental REST opener (same auth scheme, different base path)
        self._base_rest = f"http://{h}:{p}/api/v2"
        pm2 = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm2.add_password(None, f"http://{h}:{p}", "", pw)
        self._opener_rest = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(pm2))

    def set_fallback_exe(self, exe):
        self._fallback_exe = exe

    @property
    def major(self):
        return self._major

    # ── direct-launch fallback ───────────────────────────────────────────────
    def direct_play(self, uris, enqueue_only=False):
        """
        Bypass the HTTP API entirely: hand the file/URL to the running VLC
        via --one-instance (VLC routes it to the existing window).  Used when
        the HTTP interface is unreachable (404 / port hijacked / lua missing).
        Works identically on VLC 3 and 4.
        """
        if not self._fallback_exe:
            print("[PlayerOne] No VLC exe known for direct-launch fallback")
            return False
        if isinstance(uris, (str, Path)): uris = [uris]
        args = [self._fallback_exe, "--one-instance"]
        if enqueue_only: args.append("--playlist-enqueue")
        args += [str(u) for u in uris]   # VLC takes plain paths and URIs alike
        try:
            flags = 0x08000000 if os.name == "nt" else 0
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
            print(f"[PlayerOne] Direct-launch fallback: {args[2:]}")
            return True
        except Exception as e:
            print(f"[PlayerOne] Direct launch failed: {e}")
            return False

    # ── version / interface detection ────────────────────────────────────────
    def _parse_major(self, verstr):
        m = re.match(r"\s*(\d+)", verstr or "")
        return int(m.group(1)) if m else None

    def detect_version(self):
        """
        Probe the running VLC.  Returns the interface kind (LUA/REST).

        Primary: the Lua /requests/status.json interface, which BOTH VLC 3.x
        and 4.x expose.  We read the reported version so the app can label it
        and adapt if ever needed.  REST is only tried if Lua is unreachable.
        Blocking; call off the UI thread.
        """
        # ── primary: Lua interface (VLC 3.x + 4.x) ──
        try:
            url = f"{self._base_lua}/status.json"
            with self._opener.open(url, timeout=2) as r:
                if r.status == 200:
                    try:
                        data = json.loads(r.read())
                        self.version_str = str(data.get("version", "")).strip()
                        self._major = self._parse_major(self.version_str)
                    except Exception:
                        pass
                    self._iface = self.LUA
                    self.http_confirmed = True
                    vtxt = self.version_str or "unknown version"
                    print(f"[VLC] Lua HTTP interface OK — VLC {vtxt} "
                          f"(major {self._major}) at {url}")
                    return self.LUA
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("[VLC] Auth failed (401) — http_password mismatch. "
                      "Check the password in config / first-boot wizard.")
                return None   # don't mask an auth problem as 'not found'
        except Exception:
            pass

        # ── experimental fallback: REST /api/v2 (not present in shipping VLC) ──
        try:
            url = f"{self._base_rest}/player"
            with self._opener_rest.open(url, timeout=2) as r:
                if r.status == 200:
                    self._iface = self.REST
                    self.http_confirmed = True
                    print(f"[VLC] Experimental REST interface responded at {url}")
                    return self.REST
        except Exception:
            pass

        print("[VLC] No HTTP interface responded — file playback will use "
              "the direct-launch fallback instead.")
        return None

    def _ver(self):
        if self._iface is None:
            self.detect_version()
        return self._iface

    # ── low-level HTTP ───────────────────────────────────────────────────────
    def _get_lua(self, ep, params=None):
        """Lua interface: GET /requests/<ep>.json[?params]  (VLC 3.x + 4.x)"""
        base_url = f"{self._base_lua}/{ep}.json"
        if not params:
            url = base_url
        elif "input" in params:
            other = {k: v for k, v in params.items() if k != "input"}
            qs = urllib.parse.urlencode(other)
            enc = urllib.parse.quote(str(params["input"]), safe="/:@!$&'()*+,;=.-_~")
            url = f"{base_url}?{qs}&input={enc}" if qs else f"{base_url}?input={enc}"
        else:
            url = base_url + "?" + urllib.parse.urlencode(params)
        try:
            with self._opener.open(url, timeout=2.0) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"[VLC] HTTP {e.code} {url}")
            if e.code == 404 and not self._404_hinted:
                self._404_hinted = True
                print("[VLC] 404 on the Lua interface usually means another "
                      "program owns this port (so our request hit THAT server) "
                      "or this VLC build lacks the Lua http plugin. Playback "
                      "will fall back to direct launch.")
                # a 404 means this endpoint isn't really VLC's Lua interface
                self.http_confirmed = False
            return None
        except Exception as e:
            print(f"[VLC] {e}")
            return None

    def _get_rest(self, ep):
        url = f"{self._base_rest}/{ep}"
        try:
            with self._opener_rest.open(url, timeout=2.0) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[VLC REST GET] {ep}: {e}")
            return None

    def _post_rest(self, ep, body=None):
        url = f"{self._base_rest}/{ep}"
        try:
            data = json.dumps(body or {}).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            with self._opener_rest.open(req, timeout=2.0) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except Exception as e:
            print(f"[VLC REST POST] {ep}: {e}")
            return None

    def _fire(self, fn, *args, **kwargs):
        """Run fn(*args, **kwargs) in a background daemon thread."""
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()

    # ── status / playlist ────────────────────────────────────────────────────
    def status(self):
        """Return status dict.  Lua shape for both VLC 3.x and 4.x."""
        if self._ver() == self.REST:
            raw = self._get_rest("player")
            if not raw:
                return None
            st   = raw.get("state", "")
            tsec = raw.get("time", 0) // 1000
            length = raw.get("length", 0) // 1000
            vol  = int(raw.get("volume", 1.0) * 256)
            info = {}
            if "media" in raw:
                m = raw["media"]
                info = {"category": {"meta": {
                    "title":    m.get("title",""),
                    "artist":   m.get("artist",""),
                    "filename": m.get("uri","").split("/")[-1],
                }}}
            return {
                "state": {"playing":"playing","paused":"paused"}.get(st, "stopped"),
                "time": tsec, "length": length, "volume": vol,
                "information": info,
                "random": raw.get("shuffle", False),
                "loop":   raw.get("loop", False),
                "repeat": raw.get("repeat", "none") != "none",
            }
        # Lua (default, VLC 3.x + 4.x)
        raw = self._get_lua("status")
        if isinstance(raw, dict):
            raw["has_video"] = self._detect_video(raw)
        return raw

    @staticmethod
    def _detect_video(raw):
        """True if VLC's status reports a video track.  VLC lists each track as
        a category ('Stream 0', 'Video', …) with a 'Type' field under
        information.category; a video track has Type == 'Video'."""
        try:
            cats = raw.get("information", {}).get("category", {})
            if isinstance(cats, dict):
                for name, val in cats.items():
                    if name == "meta":
                        continue
                    if isinstance(val, dict):
                        t = str(val.get("Type", val.get("type", ""))).lower()
                        if t == "video":
                            return True
        except Exception:
            pass
        return False

    def playlist(self):
        if self._ver() == self.REST:
            raw = self._get_rest("playlist")
            if not raw:
                return None
            items = raw.get("items", []) if isinstance(raw, dict) else raw
            children = [{"name": it.get("title", it.get("uri","").split("/")[-1]),
                         "id":   it.get("id", i),
                         "uri":  it.get("uri","")}
                        for i, it in enumerate(items)]
            return {"children": [{"children": children}]}
        return self._normalize_playlist(self._get_lua("playlist"))

    def _normalize_playlist(self, raw):
        """
        Return a VLC3-shaped dict {"children":[{"children":[leaves]}]} no matter
        what the interface handed back.  VLC 3.x returns a nested dict; VLC 4.x's
        Lua playlist.json returns a bare list (which is what caused the
        'list' object has no attribute 'get' crash).  Handle both, plus the
        {"items":[...]} shape, defensively.
        """
        if raw is None:
            return {"children": [{"children": []}]}

        def leaves_of(node):
            out = []
            if isinstance(node, dict):
                kids = node.get("children")
                if isinstance(kids, list):
                    for c in kids:
                        out += leaves_of(c)
                elif node.get("uri") or (node.get("name") is not None
                                         and node.get("id") is not None):
                    out.append(node)
            elif isinstance(node, list):
                for c in node:
                    out += leaves_of(c)
            return out

        # For the VLC3 nested dict, prefer the active "Playlist" group so the
        # queue doesn't fill with the whole Media Library.
        src = raw
        if isinstance(raw, dict) and isinstance(raw.get("children"), list):
            named = None
            for c in raw["children"]:
                if isinstance(c, dict) and str(c.get("name","")).strip().lower() \
                        in ("playlist", "current playlist"):
                    named = c; break
            if named is None:
                for c in raw["children"]:
                    if isinstance(c, dict) and c.get("children"):
                        named = c; break
            src = named if named is not None else raw
        elif isinstance(raw, dict) and isinstance(raw.get("items"), list):
            src = raw["items"]

        leaves = leaves_of(src)
        children = []
        for it in leaves:
            uri = it.get("uri", "") if isinstance(it, dict) else ""
            name = (it.get("name") or it.get("title") if isinstance(it, dict) else None)
            if not name:
                name = os.path.basename(uri.rstrip("/")) or "?"
            children.append({"id": it.get("id","") if isinstance(it, dict) else "",
                             "name": name, "uri": uri})
        return {"children": [{"children": children}]}

    # ── commands ─────────────────────────────────────────────────────────────
    def cmd(self, command, **extra):
        if self._ver() != self.REST:
            # Lua interface (VLC 3.x + 4.x)
            params = {"command": command}
            params.update(extra)
            self._fire(self._get_lua, "status", params)
            return
        # Experimental REST mapping
        MAP = {
            "pl_pause":    ("player/pause",   None),
            "pl_stop":     ("player/stop",    None),
            "pl_next":     ("player/next",    None),
            "pl_previous": ("player/prev",    None),
            "fullscreen":  ("player/fullscreen", None),
            "pl_loop":     ("player/loop",    None),
            "pl_repeat":   ("player/repeat",  None),
            "pl_random":   ("player/shuffle", None),
            "pl_empty":    ("playlist/clear", None),
        }
        if command == "seek":
            val = extra.get("val","0")
            secs = int(str(val).replace("S","").replace("+",""))
            self._fire(self._post_rest, "player/seek", {"time": secs * 1000})
        elif command == "volume":
            val = str(extra.get("val","0")).replace("+","")
            cur = (self.status() or {}).get("volume", 256)
            pct = int(val)
            new_vol = max(0, min(512, cur + pct * 256 // 100)) / 256
            self._fire(self._post_rest, "player/volume", {"volume": new_vol})
        elif command in ("in_play", "in_enqueue"):
            uri = extra.get("input","")
            def _add_and_play():
                self._post_rest("playlist/items", {"uri": uri})
                if command == "in_play":
                    pl = self._get_rest("playlist")
                    if pl and pl.get("items"):
                        last_id = pl["items"][-1].get("id")
                        if last_id is not None:
                            self._post_rest(f"playlist/items/{last_id}/play", {})
            self._fire(_add_and_play)
        elif command == "pl_play":
            item_id = extra.get("id")
            if item_id:
                self._fire(self._post_rest, f"playlist/items/{item_id}/play", {})
            else:
                self._fire(self._post_rest, "player/play", {})
        elif command in MAP:
            ep, body = MAP[command]
            self._fire(self._post_rest, ep, body or {})

    # ── convenience wrappers ─────────────────────────────────────────────────
    def play_pause(self):    self.cmd("pl_pause")
    def stop(self):          self.cmd("pl_stop")
    def next(self):          self.cmd("pl_next")
    def prev(self):          self.cmd("pl_previous")
    def fullscreen(self):    self.cmd("fullscreen")
    def toggle_loop(self):   self.cmd("pl_loop")
    def toggle_repeat(self): self.cmd("pl_repeat")
    def toggle_random(self): self.cmd("pl_random")
    def seek(self, s):       self.cmd("seek",   val=f"{s:+d}S")
    def vol_up(self):        self.cmd("volume", val=f"+{CFG['volume_step']}")
    def vol_down(self):      self.cmd("volume", val=f"-{CFG['volume_step']}")
    def pl_empty(self):      self.cmd("pl_empty")
    def pl_play_id(self, i): self.cmd("pl_play", id=i)

    def enqueue(self, path, play_first=False):
        raw = str(path)
        uri = (raw if raw.startswith(("file:///","http://","https://",
                                       "rtsp://","mms://","rtp://"))
               else Path(raw).as_uri())
        print(f"[PlayerOne] {'play' if play_first else 'queue'}: {uri}")
        if not self.http_confirmed:
            def _probe_then_play():
                # one cheap re-probe in case VLC came up late
                self.detect_version()
                if self.http_confirmed:
                    self.cmd("in_play" if play_first else "in_enqueue", input=uri)
                else:
                    self.direct_play(raw, enqueue_only=not play_first)
            threading.Thread(target=_probe_then_play, daemon=True).start()
            return
        self.cmd("in_play" if play_first else "in_enqueue", input=uri)

    def enqueue_many(self, paths):
        def _send():
            if not self.http_confirmed:
                self.detect_version()
            if not self.http_confirmed:
                self.direct_play(list(paths))
                return
            for i, p in enumerate(paths):
                raw = str(p)
                uri = (raw if raw.startswith(("http://","https://","rtsp://"))
                       else Path(raw).as_uri())
                self.cmd("in_play" if i == 0 else "in_enqueue", input=uri)
                time.sleep(0.05)   # small gap so VLC processes them in order
        threading.Thread(target=_send, daemon=True).start()

    def detect_and_report(self, status_label_callback=None):
        """Call from background thread after launch to report VLC version."""
        iface = self.detect_version()
        if iface is None:
            msg = "VLC HTTP interface not reachable — using direct launch"
        elif iface == self.REST:
            msg = "VLC experimental REST interface"
        else:
            vtxt = self.version_str or "unknown version"
            msg = f"VLC {vtxt} (Lua HTTP interface)"
        if status_label_callback:
            status_label_callback(msg)

# ─────────────────────────────────────────────────────────
# ID3v2 TAG READER  (zero external deps)
# ─────────────────────────────────────────────────────────
def read_id3(path):
    """Return dict with title/artist/album/genre or empty strings."""
    result={"title":"","artist":"","album":"","genre":"","duration":0}
    try:
        data=Path(path).read_bytes()
        if data[:3]!=b"ID3": return result
        def _sz(b4):  # syncsafe int
            return ((b4[0]&0x7f)<<21)|((b4[1]&0x7f)<<14)|((b4[2]&0x7f)<<7)|(b4[3]&0x7f)
        def _str(b):
            enc=b[0]; raw=b[1:]
            try:
                if enc in(1,2): return raw.decode("utf-16","replace").strip("\x00").strip()
                return raw.decode("utf-8" if enc==3 else "latin-1","replace").strip("\x00").strip()
            except Exception: return ""
        tag_size=_sz(data[6:10])
        pos=10
        while pos+10<tag_size:
            fid=data[pos:pos+4]
            if fid==b"\x00\x00\x00\x00": break
            fsz=struct.unpack(">I",data[pos+4:pos+8])[0]
            if fsz<=0 or pos+10+fsz>len(data): break
            body=data[pos+10:pos+10+fsz]
            if   fid==b"TIT2": result["title"] =_str(body)
            elif fid==b"TPE1": result["artist"]=_str(body)
            elif fid==b"TALB": result["album"] =_str(body)
            elif fid==b"TCON": result["genre"] =_str(body).strip("()")
            pos+=10+fsz
    except Exception: pass
    return result

# ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────
# MEDIA LIBRARY  — delegates to media_library.py module
# ─────────────────────────────────────────────────────────
if _HAS_ML:
    # Use the full-featured module
    LIB = _ml.MediaLibrary(config_paths=CFG.get("library_paths", []))

    # Add fast cached-group methods that don't trigger tag reads
    def _by_artist_cached(self):
        """Group by cached artist tag, fall back to filename first letter."""
        d={}
        with self._lock: audio=list(self.audio)
        for p in audio:
            cached=self._cache.get(p) if hasattr(self,'_cache') else None
            a=(cached.get("artist","") if cached else "") or p.name[0].upper()
            d.setdefault(a,[]).append(p)
        return dict(sorted(d.items(),key=lambda x:x[0].lower()))

    def _by_genre_cached(self):
        d={}
        with self._lock: audio=list(self.audio)
        for p in audio:
            cached=self._cache.get(p) if hasattr(self,'_cache') else None
            g=(cached.get("genre","") if cached else "") or "Unknown"
            d.setdefault(g,[]).append(p)
        return dict(sorted(d.items(),key=lambda x:x[0].lower()))

    def _by_album_cached(self):
        d={}
        with self._lock: audio=list(self.audio)
        for p in audio:
            cached=self._cache.get(p) if hasattr(self,'_cache') else None
            al=(cached.get("album","") if cached else "") or "Unknown Album"
            ar=(cached.get("artist","") if cached else "") or ""
            if al not in d: d[al]={"artist":ar,"files":[]}
            d[al]["files"].append(p)
        return dict(sorted(d.items(),key=lambda x:x[0].lower()))

    import types
    LIB.by_artist_cached = types.MethodType(_by_artist_cached, LIB)
    LIB.by_genre_cached  = types.MethodType(_by_genre_cached,  LIB)
    LIB.by_album_cached  = types.MethodType(_by_album_cached,  LIB)

    def _lib_scan_start(callback=None):
        """Wire the module's observer to the legacy callback signature."""
        def _obs(reason):
            if callback is None:
                return
            if reason == "scan_done":
                callback("done")
            elif reason == "scan_progress":
                callback(LIB.scan_msg)
            elif reason in ("incremental_add", "files_added", "files_removed"):
                callback("done")   # trigger a UI refresh
        LIB.on_update(_obs)
        LIB.scan()
        LIB.start_watcher(interval=30)

else:
    # Minimal fallback (no media_library.py found)
    class _FallbackLib:
        DEFAULT_ROOTS=[Path.home()/"Videos", Path.home()/"Music",
                       Path("C:/Users/Public/Videos"), Path("C:/Users/Public/Music")]
        def __init__(self):
            self.videos=[]; self.audio=[]; self.playlists=[]
            self._meta={}; self._lock=threading.Lock(); self.ready=False
            self.scan_msg=""
        def on_update(self, cb): pass
        def scan(self, extra=None, callback=None):
            def _run():
                roots=list(self.DEFAULT_ROOTS)+[Path(p) for p in (CFG.get("library_paths") or [])]
                if extra: roots+=[Path(x) for x in extra]
                v,a,p=[],[],[]
                for root in roots:
                    if not root.exists(): continue
                    try:
                        for f in root.rglob("*"):
                            if not f.is_file(): continue
                            s=f.suffix.lower()
                            if s in VIDEO_EXTS: v.append(f)
                            elif s in AUDIO_EXTS: a.append(f)
                            elif s in PLAYLIST_EXTS: p.append(f)
                    except Exception: pass
                v.sort(key=lambda f:f.stat().st_mtime,reverse=True)
                a.sort(key=lambda f:f.name.lower())
                with self._lock:
                    self.videos=v; self.audio=a; self.playlists=p; self.ready=True
                if callback: callback("done")
            threading.Thread(target=_run,daemon=True).start()
        def start_watcher(self,interval=30): pass
        def add_folder(self,path): pass
        def remove_folder(self,path): pass
        def get_roots(self): return [str(r) for r in self.DEFAULT_ROOTS]
        def add_files(self,paths):
            with self._lock:
                for raw in paths:
                    p=Path(raw); s=p.suffix.lower()
                    if s in VIDEO_EXTS and p not in self.videos: self.videos.insert(0,p)
                    elif s in AUDIO_EXTS and p not in self.audio: self.audio.insert(0,p)
                    elif s in PLAYLIST_EXTS and p not in self.playlists: self.playlists.insert(0,p)
        def meta(self,path):
            p=Path(path)
            if p in self._meta: return self._meta[p]
            s=p.suffix.lower()
            m={"title":p.stem,"artist":"","album":"","genre":"","track":"","year":"",
               "has_art":False,"type":"audio" if s in AUDIO_EXTS else "video","path":str(p)}
            if s in AUDIO_EXTS:
                tags=read_id3(p)
                if tags["title"]: m["title"]=tags["title"]
                m["artist"]=tags["artist"]; m["album"]=tags["album"]; m["genre"]=tags["genre"]
            self._meta[p]=m; return m
        def art_path(self,path): return None
        def by_artist(self):
            d={}
            for p in self.audio:
                a=self.meta(p)["artist"] or "Unknown Artist"; d.setdefault(a,[]).append(p)
            return dict(sorted(d.items(),key=lambda x:x[0].lower()))
        def by_album(self):
            d={}
            for p in self.audio:
                m=self.meta(p); al=m["album"] or "Unknown Album"
                d.setdefault(al,{"artist":m["artist"],"files":[]})["files"].append(p)
            return dict(sorted(d.items(),key=lambda x:x[0].lower()))
        def by_genre(self):
            d={}
            for p in self.audio:
                g=self.meta(p)["genre"] or "Unknown"; d.setdefault(g,[]).append(p)
            return dict(sorted(d.items(),key=lambda x:x[0].lower()))
        # Fast cached versions — group by folder/first-letter without reading tags
        def by_artist_cached(self):
            # Group by cached meta if available, else first letter of filename
            d={}
            for p in self.audio:
                cached=self._meta.get(p)
                a=(cached["artist"] if cached and cached.get("artist") else None) or p.name[0].upper()
                d.setdefault(a,[]).append(p)
            return dict(sorted(d.items(),key=lambda x:x[0].lower()))
        def by_genre_cached(self):
            d={}
            for p in self.audio:
                cached=self._meta.get(p)
                g=(cached["genre"] if cached and cached.get("genre") else None) or "Unknown"
                d.setdefault(g,[]).append(p)
            return dict(sorted(d.items(),key=lambda x:x[0].lower()))
        def by_album_cached(self):
            d={}
            for p in self.audio:
                cached=self._meta.get(p)
                al=(cached["album"] if cached and cached.get("album") else None) or "Unknown Album"
                artist=(cached["artist"] if cached and cached.get("artist") else "") or ""
                if al not in d: d[al]={"artist":artist,"files":[]}
                d[al]["files"].append(p)
            return dict(sorted(d.items(),key=lambda x:x[0].lower()))
        def video_folders(self):
            d={}
            for p in self.videos: d.setdefault(str(p.parent),[]).append(p)
            return d
        def all_media(self):
            with self._lock: return self.videos+self.audio
        def recent(self,n=40):
            with self._lock: mixed=self.videos[:n//2]+self.audio[:n//2]
            mixed.sort(key=lambda p:p.stat().st_mtime,reverse=True); return mixed[:n]
        def recently_added_videos(self,n=50):
            with self._lock: return list(self.videos[:n])
        def recently_added_audio(self,n=50):
            with self._lock: return list(self.audio[:n])
        def search(self,q):
            ql=q.lower()
            with self._lock: cands=self.videos+self.audio
            return [p for p in cands if ql in p.name.lower()][:200]
        def sort_all(self,key="name"):
            with self._lock: c=self.videos+self.audio
            c.sort(key=lambda p:p.name.lower()); return c

    LIB = _FallbackLib()

    def _lib_scan_start(callback=None):
        LIB.scan(callback=callback)


# ─────────────────────────────────────────────────────────
# VLC PROCESS
# ─────────────────────────────────────────────────────────
def find_vlc():
    for p in VLC_CANDIDATES:
        if p and os.path.isfile(p): return p
    return None

def kill_existing_vlc(exclude_pid=None):
    """
    Close any VLC instance that's already running before we launch our own.
    Our controller assumes it owns the VLC process (single --one-instance
    target on a known HTTP port); a stray pre-existing VLC would either steal
    the --one-instance handoff or hold the port, so we clear them first.
    Best-effort and non-fatal.
    """
    if os.name == "nt":
        try:
            flags = 0x08000000  # CREATE_NO_WINDOW
            subprocess.run(["taskkill", "/F", "/IM", "vlc.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=flags, timeout=6)
            print("[PlayerOne] Closed any pre-existing VLC instances")
        except Exception as e:
            print(f"[PlayerOne] Could not close existing VLC: {e}")
    else:
        try:
            subprocess.run(["pkill", "-f", "vlc"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=6)
        except Exception:
            pass


def minimize_vlc_to_tray(pid, attempts=10):
    """
    Best-effort: hide VLC's main window from the taskbar so it lives in the
    system tray (VLC is launched with --qt-system-tray).  We enumerate the
    top-level windows belonging to `pid`, strip the taskbar button
    (WS_EX_APPWINDOW → WS_EX_TOOLWINDOW) and minimize them.  Windows only;
    silently does nothing elsewhere or if the window isn't up yet.

    Runs in a background thread and retries a few times because VLC's Qt
    window doesn't exist the instant the process starts.
    """
    if os.name != "nt" or not pid:
        return

    def _work():
        try:
            user32 = ctypes.windll.user32
            GWL_EXSTYLE   = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW  = 0x00040000
            SW_HIDE = 0; SW_MINIMIZE = 6; SW_SHOWMINNOACTIVE = 7

            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                          ctypes.c_ssize_t, ctypes.c_ssize_t)
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_ssize_t, ctypes.POINTER(ctypes.c_ulong)]
            user32.IsWindowVisible.argtypes = [ctypes.c_ssize_t]
            _gwl = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            _swl = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            _gwl.argtypes = [ctypes.c_ssize_t, ctypes.c_int]; _gwl.restype = ctypes.c_ssize_t
            _swl.argtypes = [ctypes.c_ssize_t, ctypes.c_int, ctypes.c_ssize_t]
            _swl.restype  = ctypes.c_ssize_t
            user32.ShowWindow.argtypes = [ctypes.c_ssize_t, ctypes.c_int]

            for _ in range(attempts):
                hit = [False]
                def _cb(hwnd, _lparam):
                    wpid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                    if wpid.value == pid and user32.IsWindowVisible(hwnd):
                        ex = _gwl(hwnd, GWL_EXSTYLE)
                        ex = (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
                        _swl(hwnd, GWL_EXSTYLE, ex)
                        # minimize WITHOUT hiding: a hidden window can't be found
                        # for video embedding, but a minimized tool-window has
                        # no taskbar button (→ effectively "in the tray").
                        user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
                        hit[0] = True
                    return True
                user32.EnumWindows(EnumProc(_cb), 0)
                if hit[0]:
                    print("[PlayerOne] VLC window moved to tray")
                    return
                time.sleep(0.4)
        except Exception as e:
            print(f"[PlayerOne] minimize-to-tray failed (non-fatal): {e}")

    threading.Thread(target=_work, daemon=True).start()


def _vlc_exe_major(exe):
    """
    Best-effort read of VLC's major version straight from the executable's
    file-version resource.  Windows only (uses version.dll); returns an int
    (3, 4, …) or None if it can't be determined.  No window is shown, unlike
    `vlc --version`.
    """
    if not exe or os.name != "nt":
        return None
    try:
        import struct
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(exe, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ver.GetFileVersionInfoW(exe, 0, size, buf)
        ptr = ctypes.c_void_p(); length = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\",
                                  ctypes.byref(ptr), ctypes.byref(length)):
            return None
        ffi = ctypes.string_at(ptr.value, length.value)
        # VS_FIXEDFILEINFO: dwFileVersionMS is bytes 8:12; high word = major
        ms = struct.unpack("<I", ffi[8:12])[0]
        major = ms >> 16
        return major or None
    except Exception:
        return None


def launch_vlc(vlc_exe):
    """
    Start VLC with the HTTP interface enabled.

    Rules
    -----
    • --extraintf=http  adds the HTTP interface ON TOP of the default Qt UI.
      Never use --intf=dummy: that disables Qt and breaks HTTP on most builds.
    • The Lua HTTP interface is identical on VLC 3.x and 4.x, so the http-*
      flags below work on both.
    • Some cosmetic Qt options (--qt-start-minimized, --no-qt-privacy-ask)
      exist on VLC 3.x but may be renamed/absent on 4.x; we only pass them
      when the exe reports major version 3, so a VLC-4 build won't choke on
      an unknown option.  (VLC usually only warns on unknown options, but we
      stay strict to keep 4.x startup clean.)
    • We capture stderr to a PIPE so we can surface errors to the user.
    """
    major = _vlc_exe_major(vlc_exe)
    if major:
        print(f"[PlayerOne] Detected VLC major version {major} from executable")

    # ── port-conflict guard ──────────────────────────────────────────────
    # A "HTTP 404 /requests/status.json" failure is almost always another
    # program already listening on the configured port.  VLC then fails to
    # bind, our probes hit the OTHER server, and every request 404s.  The
    # default port (43210) is deliberately uncommon, but if something is
    # already accepting connections on it BEFORE we launch VLC, switch to a
    # free port for this session.
    try:
        s = socket.create_connection((CFG["http_host"], CFG["http_port"]), 0.4)
        s.close()
        # occupied — find a free one
        probe = socket.socket()
        probe.bind((CFG["http_host"], 0))
        free_port = probe.getsockname()[1]
        probe.close()
        print(f"[PlayerOne] Port {CFG['http_port']} already in use by another "
              f"program — using port {free_port} for VLC instead")
        CFG["http_port"] = free_port
    except OSError:
        pass   # nothing listening: port is ours

    args = [
        vlc_exe,
        "--one-instance",
        "--extraintf=http",
        f"--http-host={CFG['http_host']}",
        f"--http-port={CFG['http_port']}",
        f"--http-password={CFG['http_password']}",
        "--no-video-title-show",
    ]
    # Qt cosmetic flags: safe on VLC 3.x.  Omit on 4.x / unknown to avoid
    # an unknown-option abort on newer builds.
    if major is None or major <= 3:
        args += ["--qt-start-minimized", "--no-qt-privacy-ask"]
    # Tray icon: gives the minimized-to-tray VLC a reachable icon.  This maps
    # to the qt-system-tray preference and is accepted on both 3.x and 4.x.
    args += ["--qt-system-tray"]
    args += ["--no-crashdump"]
    # CREATE_NO_WINDOW (0x08000000) prevents a console flash on Windows.
    # We keep stderr as PIPE so launch_vlc_safe() can read it on failure.
    flags = 0x08000000 if os.name == "nt" else 0
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )


def _vlc_stderr_drain(proc):
    """Read stderr in background; return last 20 lines as a string."""
    lines = []
    try:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                lines.append(line)
    except Exception:
        pass
    return "\n".join(lines[-20:])


def wait_for_vlc(timeout=14):
    t = time.time() + timeout
    while time.time() < t:
        try:
            s = socket.create_connection((CFG["http_host"], CFG["http_port"]), 1)
            s.close()
            return True
        except OSError:
            time.sleep(0.35)
    return False


def show_vlc_error(root, vlc_path, stderr_text=""):
    """
    Show a friendly error dialog when VLC won't start,
    with diagnosis hints and the raw stderr output.
    """
    import tkinter.messagebox as mb

    hints = []

    if not vlc_path:
        hints.append("• VLC was not found. Install VLC from https://www.videolan.org/")
    else:
        hints.append(f"• VLC path:  {vlc_path}")

    port = CFG.get("http_port", 43210)
    hints.append(f"• HTTP port {port} may already be in use by another app or VLC instance.")
    hints.append("• Try closing all VLC windows and restarting PlayerOne.")
    hints.append("• Make sure you are using VLC 3.x (not a nightly/4.x build).")
    hints.append("• Antivirus software can block VLC's HTTP server — check exclusions.")

    body = "\n".join(hints)
    if stderr_text:
        body += f"\n\nVLC output:\n{stderr_text}"

    top = tk.Toplevel(root)
    top.title("VLC could not start")
    top.configure(bg=T["bg"])
    top.geometry("560x420")
    top.attributes("-topmost", True)
    _set_icon(top)

    tk.Label(top, text="⚠  VLC could not start", bg=T["bg"], fg=T["highlight"],
             font=("Segoe UI", 13, "bold")).pack(pady=(16, 6), padx=20, anchor="w")

    tk.Label(top,
             text="PlayerOne requires VLC's HTTP interface to control playback.\n"
                  "Most features will be unavailable until VLC is running.",
             bg=T["bg"], fg=T["text"], font=("Segoe UI", 10),
             justify="left", wraplength=520).pack(padx=20, anchor="w")

    box = tk.Text(top, bg=T["panel"], fg=T["subtext"], font=("Consolas", 9),
                  relief="flat", height=12, wrap="word")
    box.pack(fill=tk.BOTH, expand=True, padx=16, pady=10)
    box.insert("1.0", body)
    box.config(state="disabled")

    btn_row = tk.Frame(top, bg=T["bg"]); btn_row.pack(pady=8)
    tk.Button(btn_row, text="Continue Anyway", command=top.destroy,
              bg=T["border"], fg=T["text"], relief="flat",
              font=("Segoe UI", 10), padx=12, pady=6).pack(side=tk.LEFT, padx=6)
    tk.Button(btn_row, text="Retry VLC Launch",
              command=lambda: [top.destroy(), _retry_vlc(root)],
              bg=T["highlight"], fg=T["bg"], relief="flat",
              font=("Segoe UI", 10, "bold"), padx=12, pady=6).pack(side=tk.LEFT, padx=6)


def _retry_vlc(root):
    """Attempt to re-launch VLC after an error."""
    vlc_path = find_vlc()
    if not vlc_path:
        import tkinter.messagebox as mb
        mb.showerror("VLC Not Found",
                     "VLC still not found.\n"
                     "Please install it from https://www.videolan.org/ "
                     "then restart PlayerOne.")
        return
    proc = launch_vlc(vlc_path)
    threading.Thread(target=_vlc_stderr_drain, args=(proc,), daemon=True).start()
    if wait_for_vlc(timeout=12):
        import tkinter.messagebox as mb
        mb.showinfo("VLC Ready", "VLC started successfully.")
    else:
        ret = proc.poll()
        show_vlc_error(root, vlc_path,
                       f"VLC exited with code {ret}" if ret is not None
                       else "VLC HTTP interface still not responding after 12 s.")

# ─────────────────────────────────────────────────────────
# WIN32 VIDEO-WINDOW EMBEDDING
# ─────────────────────────────────────────────────────────
_u32=ctypes.windll.user32 if hasattr(ctypes,"windll") else None

def _pid_hwnds(pid):
    hwnds=[]
    if not _u32: return hwnds
    CB=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def _cb(h,_):
        if _u32.IsWindowVisible(h):
            dp=ctypes.wintypes.DWORD()
            _u32.GetWindowThreadProcessId(h,ctypes.byref(dp))
            if dp.value==pid: hwnds.append(h)
        return True
    _u32.EnumWindows(CB(_cb),0)
    return hwnds

def _area(h):
    r=ctypes.wintypes.RECT()
    _u32.GetWindowRect(h,ctypes.byref(r))
    return max(0,r.right-r.left)*max(0,r.bottom-r.top)

def find_video_hwnd(pid):
    cands=_pid_hwnds(pid)
    if not cands: return None
    # A minimized window (tray mode) has a tiny rect, so don't reject it on
    # area alone — prefer any non-iconic sizable window, else fall back to the
    # largest candidate (which _finish_embed will restore before embedding).
    def _iconic(h):
        try:
            _u32.IsIconic.argtypes=[ctypes.c_ssize_t]; _u32.IsIconic.restype=ctypes.c_bool
            return bool(_u32.IsIconic(h))
        except Exception: return False
    sizable=[h for h in cands if not _iconic(h) and _area(h)>2000]
    if sizable:
        sizable.sort(key=_area,reverse=True)
        return sizable[0]
    cands.sort(key=_area,reverse=True)
    return cands[0]

def embed_hwnd(child,container):
    GWL=-16
    WS_CHILD=0x40000000; WS_POP=0x80000000
    WS_CAP=0x00C00000;   WS_TF=0x00040000
    WS_BDR=0x00800000;   WS_DLG=0x00400000; WS_VIS=0x10000000
    s=_u32.GetWindowLongPtrW(child,GWL)
    s&=~(WS_POP|WS_CAP|WS_TF|WS_BDR|WS_DLG)
    s|=(WS_CHILD|WS_VIS)
    _u32.SetWindowLongPtrW(child,GWL,s)
    _u32.SetParent(child,container)
    _u32.SetWindowPos(child,0,0,0,0,0,0x0020|0x0004|0x0010)

def resize_child(child,w,h):
    if child:
        try: _u32.MoveWindow(child,0,0,max(1,w),max(1,h),True)
        except Exception: pass

# ─────────────────────────────────────────────────────────
# FILE-ASSOCIATION WIZARD
# ─────────────────────────────────────────────────────────
def run_first_boot(root, exe_path=None):
    try:
        import winreg
    except ImportError:
        winreg=None

    top=tk.Toplevel(root)
    top.title("PlayerOne – Setup")
    _set_icon(top)
    top.configure(bg=T["bg"])
    top.resizable(False,False)
    top.grab_set(); top.focus_set()
    top.geometry("520x560")

    def H(txt,size=14,color=None):
        tk.Label(top,text=txt,bg=T["bg"],fg=color or T["highlight"],
                 font=("Segoe UI",size,"bold")).pack(pady=(12,2),padx=28,anchor="w")
    def P(txt):
        tk.Label(top,text=txt,bg=T["bg"],fg=T["subtext"],
                 font=("Segoe UI",9)).pack(padx=28,anchor="w",pady=(0,6))

    H("Welcome to PlayerOne")
    P("Set up file associations so double-clicking media opens PlayerOne.")

    # ── file type groups
    groups={
        "Video files (mp4, mkv, avi, mov…)": (True, VIDEO_EXTS),
        "Audio files (mp3, flac, aac, wav…)": (True, AUDIO_EXTS),
        "Playlists (m3u, xspf, pls…)":        (True, PLAYLIST_EXTS),
    }
    checks={}
    fr_types=tk.Frame(top,bg=T["panel"],bd=0)
    fr_types.pack(fill=tk.X,padx=24,pady=4)
    for label,(default,exts) in groups.items():
        var=tk.BooleanVar(value=default)
        checks[label]=(var,exts)
        tk.Checkbutton(fr_types,text=label,variable=var,
                       bg=T["panel"],fg=T["text"],
                       selectcolor=T["bg"],activebackground=T["panel"],
                       activeforeground=T["text"],
                       font=("Segoe UI",10)).pack(anchor="w",padx=10,pady=5)

    H("Library",size=11,color=T["text"])
    P("PlayerOne will scan these folders for media on startup.")

    lib_var=tk.BooleanVar(value=True)
    fr_lib=tk.Frame(top,bg=T["panel"])
    fr_lib.pack(fill=tk.X,padx=24,pady=4)
    tk.Checkbutton(fr_lib,text="Scan Videos / Music / Pictures folders",
                   variable=lib_var,bg=T["panel"],fg=T["text"],
                   selectcolor=T["bg"],activebackground=T["panel"],
                   activeforeground=T["text"],
                   font=("Segoe UI",10)).pack(anchor="w",padx=10,pady=5)

    status=tk.Label(top,text="",bg=T["bg"],fg=T["subtext"],font=("Segoe UI",8))
    status.pack(pady=2)

    def _apply():
        CFG["scan_library"]=lib_var.get()
        CFG["first_boot"]=False

        if not exe_path:
            status.config(text="Skipped file associations (no .exe found – run install.py to build).",
                          fg=T["subtext"])
            _save_config()
            return

        if not winreg:
            status.config(text="winreg not available – associations skipped.",fg=T["subtext"])
            _save_config()
            return

        pid="PlayerOne.MediaFile"
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  rf"Software\Classes\{pid}") as k:
                winreg.SetValue(k,"",winreg.REG_SZ,"PlayerOne Media File")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  rf"Software\Classes\{pid}\shell\open\command") as k:
                winreg.SetValue(k,"",winreg.REG_SZ,f'"{exe_path}" "%1"')
            ico=Path(exe_path).parent/"icon.ico"
            if ico.is_file():
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                      rf"Software\Classes\{pid}\DefaultIcon") as k:
                    winreg.SetValue(k,"",winreg.REG_SZ,str(ico))
            n=0
            for label,(var,exts) in checks.items():
                if not var.get(): continue
                for ext in exts:
                    try:
                        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                              rf"Software\Classes\{ext}") as k:
                            winreg.SetValue(k,"",winreg.REG_SZ,pid)
                        n+=1
                    except Exception: pass
            try: ctypes.windll.shell32.SHChangeNotify(0x08000000,0,None,None)
            except Exception: pass
            status.config(text=f"✓  Registered {n} file extensions.",fg=T["green"])
        except Exception as e:
            status.config(text=f"Error: {e}",fg=T["red"])
        _save_config()

    def _skip():
        CFG["first_boot"]=False
        _save_config()
        top.destroy()

    br=tk.Frame(top,bg=T["bg"]); br.pack(pady=16)
    tk.Button(br,text="Apply & Continue",command=lambda:[_apply(),top.destroy()],
              bg=T["highlight"],fg=T["bg"],font=("Segoe UI",11,"bold"),
              relief="flat",padx=18,pady=9).pack(side=tk.LEFT,padx=8)
    tk.Button(br,text="Skip",command=_skip,
              bg=T["border"],fg=T["text"],font=("Segoe UI",10),
              relief="flat",padx=18,pady=9).pack(side=tk.LEFT,padx=8)

    root.wait_window(top)

# ─────────────────────────────────────────────────────────
# PLAYLIST PARSER
# ─────────────────────────────────────────────────────────
def parse_playlist(path):
    p=Path(path); sfx=p.suffix.lower(); results=[]
    try:
        text=p.read_text(errors="replace")
        if sfx in(".m3u",".m3u8"):
            for line in text.splitlines():
                line=line.strip()
                if line and not line.startswith("#"):
                    fp=Path(line) if Path(line).is_absolute() else p.parent/line
                    if fp.exists(): results.append(fp)
        elif sfx==".pls":
            for line in text.splitlines():
                if re.match(r"File\d+=",line,re.I):
                    val=line.split("=",1)[1].strip()
                    fp=Path(val) if Path(val).is_absolute() else p.parent/val
                    if fp.exists(): results.append(fp)
        elif sfx==".xspf":
            for m in re.finditer(r"<location>file:///([^<]+)</location>",text):
                fp=Path(urllib.parse.unquote(m.group(1).replace("/","\\",1)))
                if fp.exists(): results.append(fp)
        elif sfx==".cue":
            for line in text.splitlines():
                m=re.match(r'\s*FILE\s+"([^"]+)"',line,re.I)
                if m:
                    fp=Path(m.group(1)) if Path(m.group(1)).is_absolute() else p.parent/m.group(1)
                    if fp.exists(): results.append(fp)
    except Exception: pass
    return results

# ─────────────────────────────────────────────────────────
# VIRTUAL KEYBOARD
# ─────────────────────────────────────────────────────────
class VoiceInput:
    """
    Best-effort voice-to-text.  Hold-to-record: start() begins capturing
    microphone audio, stop_and_recognize(cb) stops and calls cb(text) on
    success (cb is invoked from a background thread).

    Uses the `speech_recognition` library if installed (pip install
    SpeechRecognition pyaudio).  Falls back to Windows SAPI dictation via
    win32com if available.  If neither works, cb(None, error_msg) is called.
    Everything is non-fatal — the keyboard works fine without voice.
    """
    def __init__(self):
        self._sr = None
        self._frames = []
        self._stream = None
        self._recording = False
        try:
            import speech_recognition as sr
            self._sr = sr
        except Exception:
            self._sr = None

    @property
    def available(self):
        return self._sr is not None

    def start(self):
        if not self._sr or self._recording:
            return False
        sr = self._sr
        self._frames = []
        self._recording = True
        def _capture():
            try:
                with sr.Microphone() as source:
                    self._sample_rate  = source.SAMPLE_RATE
                    self._sample_width = source.SAMPLE_WIDTH
                    while self._recording:
                        try:
                            buf = source.stream.read(source.CHUNK)
                            self._frames.append(buf)
                        except Exception:
                            break
            except Exception as e:
                print(f"[Voice] mic error: {e}")
                self._recording = False
        threading.Thread(target=_capture, daemon=True).start()
        return True

    def stop_and_recognize(self, cb):
        """cb(text_or_None, error_or_None) — called from a worker thread."""
        if not self._sr:
            cb(None, "SpeechRecognition not installed"); return
        self._recording = False
        sr = self._sr
        def _work():
            time.sleep(0.15)   # let capture thread flush
            if not self._frames:
                cb(None, "No audio captured"); return
            try:
                audio = sr.AudioData(b"".join(self._frames),
                                     self._sample_rate, self._sample_width)
                r = sr.Recognizer()
                try:
                    text = r.recognize_google(audio)
                except sr.UnknownValueError:
                    cb(None, "Could not understand audio"); return
                except sr.RequestError:
                    # offline fallback: Windows SAPI-free sphinx if present
                    try:
                        text = r.recognize_sphinx(audio)
                    except Exception:
                        cb(None, "Speech service unavailable"); return
                cb(text, None)
            except Exception as e:
                cb(None, str(e))
        threading.Thread(target=_work, daemon=True).start()


class VirtualKeyboard(tk.Toplevel):
    """
    Controller-navigable on-screen keyboard.

    Layers
    ------
    lower  : abc + digits
    upper  : ABC + digits    (via ⇧ one-shot Shift or ⇪ Caps Lock)
    sym    : URL / punctuation symbols  (via Sym toggle)

    Extra keys
    ----------
    ⇧  Shift      — uppercase for the next single character
    ⇪  Caps       — uppercase lock until pressed again
    Sym / ABC     — toggle symbol layer
    🎤 Mic        — press to start recording, press again to stop &
                    insert recognized text.  On a controller you can also
                    HOLD the Y button to record and release to confirm.
    """
    LOWER=[list("1234567890"),list("qwertyuiop"),list("asdfghjkl"),
           list("zxcvbnm"),["⇧","⇪","Sym","Space","⌫","Enter","🎤"]]
    SYM  =[list("1234567890"),list(".-_@:/?=&#"),list("%+~!$*'(),"),
           list(";[]{}<>\\^|\""),["ABC","⇪","Sym","Space","⌫","Enter","🎤"]]

    def __init__(self,parent,var,on_done,placeholder=""):
        super().__init__(parent)
        self.overrideredirect(True); self.attributes("-topmost",True)
        self.configure(bg=T["panel"])
        self._var=var; self._done=on_done; self._cx=self._cy=0
        self._placeholder=placeholder
        self._shift=False; self._caps=False; self._sym=False
        self._voice=VoiceInput(); self._voice_active=False
        parent.update_idletasks()
        pw,ph=parent.winfo_width(),parent.winfo_height()
        px,py=parent.winfo_rootx(),parent.winfo_rooty()
        self.geometry(f"680x340+{px+pw//2-340}+{py+ph-360}")
        self._entry_lbl=tk.Label(self,bg=T["bg"],fg=T["text"],
                 font=("Segoe UI",12),anchor="w",padx=8)
        self._entry_lbl.pack(fill=tk.X,pady=(6,0))
        self._var.trace_add("write",lambda *a:self._update_entry())
        self._status=tk.Label(self,text="",bg=T["panel"],fg=T["subtext"],
                 font=("Segoe UI",8),anchor="w",padx=8)
        self._status.pack(fill=tk.X)
        self._kbframe=tk.Frame(self,bg=T["panel"]); self._kbframe.pack()
        self._btns=[]
        self._build_rows()
        self._update_entry()
        self._hl()

    # ── layout ───────────────────────────────────────────────────────────────
    def _rows(self):
        rows = self.SYM if self._sym else self.LOWER
        if self._sym: return rows
        if self._shift or self._caps:
            return [[c.upper() if len(c)==1 and c.isalpha() else c
                     for c in row] for row in rows]
        return rows

    def _build_rows(self):
        for w in self._kbframe.winfo_children(): w.destroy()
        self._btns=[]
        for r,row in enumerate(self._rows()):
            fr=tk.Frame(self._kbframe,bg=T["panel"]); fr.pack(pady=2)
            rb=[]
            for c,ch in enumerate(row):
                w=5 if len(ch)==1 else 7
                b=tk.Label(fr,text=ch,width=w,bg=T["popup_bg"],fg=T["text"],
                           font=("Segoe UI",11),pady=7,relief="flat")
                b.pack(side=tk.LEFT,padx=2)
                b.bind("<Button-1>",lambda e,ch=ch:self._press(ch))
                rb.append((b,ch))
            self._btns.append(rb)
        # keep cursor in bounds after rebuild
        self._cy=min(self._cy,len(self._btns)-1)
        self._cx=min(self._cx,len(self._btns[self._cy])-1)

    def _update_entry(self):
        v=self._var.get()
        if v:
            self._entry_lbl.config(text=v,fg=T["text"])
        else:
            self._entry_lbl.config(text=self._placeholder,fg=T["subtext"])

    def _refresh_layer(self):
        self._build_rows(); self._hl()

    def _hl(self):
        for r,row in enumerate(self._btns):
            for c,(b,ch) in enumerate(row):
                sel=r==self._cy and c==self._cx
                # show latched modifier state
                latch=((ch in("⇧","Shift") and self._shift) or
                       (ch=="⇪" and self._caps) or
                       (ch=="Sym" and self._sym) or
                       (ch=="🎤" and self._voice_active))
                bg=T["highlight"] if sel else (T["border"] if latch else T["popup_bg"])
                b.config(bg=bg, fg=T["bg"] if sel else T["text"])

    def navigate(self,dx,dy):
        self._cy=max(0,min(len(self._btns)-1,self._cy+dy))
        self._cx=max(0,min(len(self._btns[self._cy])-1,self._cx+dx))
        self._hl()

    def activate(self): self._press(self._btns[self._cy][self._cx][1])

    # ── voice (also driven by controller hold-Y via voice_start/voice_stop) ──
    def voice_start(self):
        if self._voice_active: return
        if not self._voice.available:
            self._status.config(text="Voice input needs:  pip install SpeechRecognition pyaudio")
            return
        if self._voice.start():
            self._voice_active=True
            self._status.config(text="🎤 Recording… release / press 🎤 again to confirm")
            self._hl()

    def voice_stop(self):
        if not self._voice_active: return
        self._voice_active=False
        self._status.config(text="Recognizing…"); self._hl()
        def _cb(text,err):
            def _apply():
                if not self.winfo_exists(): return
                if text:
                    self._var.set(self._var.get()+text)
                    self._status.config(text="")
                else:
                    self._status.config(text=f"Voice: {err}")
            try: self.after(0,_apply)
            except Exception: pass
        self._voice.stop_and_recognize(_cb)

    def _toggle_voice(self):
        if self._voice_active: self.voice_stop()
        else: self.voice_start()

    # ── key handling ─────────────────────────────────────────────────────────
    def _press(self,ch):
        v=self._var.get()
        if   ch=="⌫":     self._var.set(v[:-1])
        elif ch=="Space": self._var.set(v+" ")
        elif ch=="Enter": self._done(self._var.get()); self.destroy()
        elif ch in("⇧","Shift"):
            self._shift=not self._shift
            if self._shift: self._caps=False
            self._refresh_layer()
        elif ch=="⇪":
            self._caps=not self._caps; self._shift=False
            self._refresh_layer()
        elif ch in("Sym","ABC"):
            self._sym=not self._sym; self._refresh_layer()
        elif ch=="🎤":
            self._toggle_voice()
        else:
            self._var.set(v+ch)
            if self._shift and not self._caps:
                self._shift=False; self._refresh_layer()

# ─────────────────────────────────────────────────────────
# POPUP MENU
# ─────────────────────────────────────────────────────────
class PopupMenu(tk.Toplevel):
    """Controller-navigable overlay menu.
    items: list of (label, callback)  – callback=None → section header, "---" → separator
    """
    def __init__(self,parent,title,items,on_select,on_close,width=440):
        super().__init__(parent)
        self.overrideredirect(True); self.attributes("-topmost",True)
        self.configure(bg=T["popup_bg"])
        self._items=[]; self._sel=[]; self._cur=0
        self.on_select=on_select; self.on_close=on_close; self._tag=title
        parent.update_idletasks()
        px,py=parent.winfo_rootx(),parent.winfo_rooty()
        pw,ph=parent.winfo_width(),parent.winfo_height()
        self.geometry(f"{width}x540+{px+pw//2-width//2}+{py+ph//2-270}")
        tk.Label(self,text=title,bg=T["highlight"],fg=T["bg"],
                 font=("Segoe UI",13,"bold"),pady=10).pack(fill=tk.X)
        cv=tk.Canvas(self,bg=T["popup_bg"],highlightthickness=0)
        self._cv=cv
        sb=ttk.Scrollbar(self,orient="vertical",command=cv.yview)
        self._sf=tk.Frame(cv,bg=T["popup_bg"])
        self._sf.bind("<Configure>",lambda e:cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0,0),window=self._sf,anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left",fill="both",expand=True); sb.pack(side="right",fill="y")
        for label,cb in items: self._add(label,cb)
        if self._sel: self._hl(self._sel[0])

    def _add(self,label,cb):
        if label=="---":
            tk.Frame(self._sf,bg=T["border"],height=1).pack(fill=tk.X,padx=8,pady=2); return
        if cb is None:
            tk.Label(self._sf,text=label.upper(),bg=T["popup_bg"],fg=T["subtext"],
                     font=("Segoe UI",8,"bold"),anchor="w",padx=12,pady=3).pack(fill=tk.X); return
        idx=len(self._items)
        row=tk.Frame(self._sf,bg=T["popup_bg"]); row.pack(fill=tk.X,padx=4,pady=1)
        lbl=tk.Label(row,text=label,bg=T["popup_bg"],fg=T["text"],
                     font=("Segoe UI",11),anchor="w",padx=12,pady=9)
        lbl.pack(fill=tk.X)
        self._items.append((row,lbl,label,cb)); self._sel.append(idx)
        for w in(row,lbl): w.bind("<Button-1>",lambda e,i=idx:self._activate(i))

    def _hl(self,idx):
        for i,(row,lbl,_,_) in enumerate(self._items):
            sel=(i==idx)
            row.config(bg=T["highlight"] if sel else T["popup_bg"])
            lbl.config(bg=T["highlight"] if sel else T["popup_bg"],
                       fg=T["bg"] if sel else T["text"])
        self._cur=idx
        self._scroll_into_view(idx)

    def _scroll_into_view(self,idx):
        """Keep the highlighted row visible when navigating past the top/bottom."""
        if not (0<=idx<len(self._items)): return
        cv=getattr(self,"_cv",None)
        if cv is None: return
        try:
            cv.update_idletasks()
            row=self._items[idx][0]
            ry=row.winfo_y(); rh=row.winfo_height()
            total=self._sf.winfo_height()
            ch=cv.winfo_height()
            if total<=ch or total<=0: return          # everything fits; no scroll
            top=cv.canvasy(0)                          # current viewport top (px)
            if ry<top:                                 # above view → scroll up
                cv.yview_moveto(max(0.0, ry/total))
            elif ry+rh>top+ch:                         # below view → scroll down
                cv.yview_moveto(min(1.0, (ry+rh-ch)/total))
        except Exception:
            pass

    def navigate(self,d):
        if not self._sel: return
        try: pos=self._sel.index(self._cur)
        except ValueError: pos=0
        self._hl(self._sel[(pos+d)%len(self._sel)])

    def activate(self): self._activate(self._cur)
    def _activate(self,idx):
        if 0<=idx<len(self._items):
            _,_,label,cb=self._items[idx]; self.on_select(label,cb)

# ─────────────────────────────────────────────────────────
# RICH LIST ROW  (two lines: title + subtitle + duration)
# ─────────────────────────────────────────────────────────
class RichList(tk.Frame):
    """
    Virtualized scrollable list of two-line media rows.

    Only the rows currently visible in the viewport (plus a small buffer) are
    materialized as Tk widgets — so loading a section with 8000+ tracks no
    longer builds 8000 widget trees on the main thread (which froze the app).
    The inner canvas is sized to the full logical height so the scrollbar
    behaves normally; widgets are placed at absolute Y offsets and rebuilt as
    the user scrolls.
    """
    ROW_H=54
    HDR_H=42
    BUFFER=6          # extra rows rendered above/below the viewport

    def __init__(self,parent,on_play,on_need_enrich=None,on_focus=None,**kw):
        super().__init__(parent,bg=T["bg"],**kw)
        self._on_play=on_play
        self._on_need_enrich=on_need_enrich
        self._on_focus=on_focus
        self._items=[]            # data only (cheap to hold thousands)
        self._offsets=[]          # cumulative y offset per item
        self._total_h=0
        self._cur=0
        self._rendered={}         # idx -> row widget (only visible ones)
        self._render_job=None

        cv=tk.Canvas(self,bg=T["bg"],highlightthickness=0)
        sb=ttk.Scrollbar(self,orient="vertical",command=cv.yview)
        cv.configure(yscrollcommand=self._on_scroll)
        sb.pack(side=tk.RIGHT,fill=tk.Y)
        cv.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        self._cv=cv; self._sb=sb

        self._frame=tk.Frame(cv,bg=T["bg"])
        self._frame_id=cv.create_window((0,0),window=self._frame,anchor="nw")
        cv.bind("<Configure>",self._on_cv_resize)
        # mouse wheel scrolling
        cv.bind("<MouseWheel>",self._on_wheel)
        cv.bind("<Button-4>",lambda e:self._wheel(-1))
        cv.bind("<Button-5>",lambda e:self._wheel(1))

        self._hint=tk.Label(self,text="Drop files here, or use Open File from the context menu",
                            bg=T["bg"],fg=T["subtext"],font=("Segoe UI",12))

    # ── geometry helpers ─────────────────────────────────
    def _item_h(self,item):
        return self.HDR_H if item.get("is_header") else self.ROW_H

    def _recompute_offsets(self):
        self._offsets=[]
        y=0
        for it in self._items:
            self._offsets.append(y)
            y+=self._item_h(it)
        self._total_h=max(y,1)

    def _on_cv_resize(self,e):
        self._cv.itemconfig(self._frame_id,width=e.width)
        self._frame.configure(width=e.width)
        self._schedule_render()

    def _on_wheel(self,e):
        self._wheel(-1 if e.delta>0 else 1)

    def _wheel(self,direction):
        self._cv.yview_scroll(direction*3,"units")
        # yview_scroll triggers _on_scroll → render

    def _on_scroll(self,first,last):
        # called on EVERY view change (wheel, drag, programmatic moveto)
        self._sb.set(first,last)
        self._schedule_render()

    def _schedule_render(self):
        if self._render_job is not None:
            return
        self._render_job=self.after(16,self._do_render)

    # ── load ─────────────────────────────────────────────
    def load(self,items):
        # tear down any rendered widgets (only the visible handful exist)
        for w in self._rendered.values():
            try: w.destroy()
            except Exception: pass
        self._rendered={}
        self._items=items
        self._cur=0
        self._recompute_offsets()

        # size the inner frame + scrollregion to the FULL logical height
        self._frame.configure(height=self._total_h)
        self._cv.configure(scrollregion=(0,0,self._cv.winfo_width(),self._total_h))
        self._cv.yview_moveto(0)

        if not items:
            self._hint.place(relx=0.5,rely=0.45,anchor="center")
            return
        self._hint.place_forget()

        self._cur=self._first_selectable()
        self._do_render()

    # ── virtualized rendering ────────────────────────────
    def _visible_range(self):
        self._cv.update_idletasks()
        vh=self._cv.winfo_height() or 1
        top=self._cv.canvasy(0)
        bottom=top+vh
        # binary-ish scan is overkill; offsets are sorted, do a linear clip
        first=0; last=len(self._items)-1
        for i,off in enumerate(self._offsets):
            if off+self._item_h(self._items[i])>=top:
                first=i; break
        for i in range(first,len(self._items)):
            if self._offsets[i]>bottom:
                last=i; break
        else:
            last=len(self._items)-1
        first=max(0,first-self.BUFFER)
        last=min(len(self._items)-1,last+self.BUFFER)
        return first,last

    def _do_render(self):
        self._render_job=None
        if not self._items:
            return
        first,last=self._visible_range()
        want=set(range(first,last+1))
        # drop widgets that scrolled out of range
        for idx in list(self._rendered.keys()):
            if idx not in want:
                try: self._rendered[idx].destroy()
                except Exception: pass
                del self._rendered[idx]
        # create widgets newly in range
        for idx in want:
            if idx in self._rendered:
                continue
            item=self._items[idx]
            y=self._offsets[idx]
            if item.get("is_header"):
                w=self._make_header(item.get("title",""))
                w.place(x=0,y=y,relwidth=1.0,height=self.HDR_H)
            else:
                w=self._make_row(idx,item,selected=(idx==self._cur))
                w.place(x=0,y=y,relwidth=1.0,height=self.ROW_H)
            self._rendered[idx]=w
        # ask the app to enrich just the rows we can actually see
        if self._on_need_enrich:
            need=[i for i in want
                  if self._items[i].get("_enrich") and not self._items[i].get("is_header")
                  and not self._items[i].get("_enriched")]
            if need:
                try: self._on_need_enrich(need)
                except Exception: pass

    def _make_header(self,text):
        fr=tk.Frame(self._frame,bg=T["bg"])
        tk.Label(fr,text=text,bg=T["bg"],fg=T["highlight"],
                 font=("Segoe UI",10,"bold"),padx=14,pady=3).pack(side=tk.LEFT)
        tk.Frame(fr,bg=T["border"],height=1).pack(side=tk.LEFT,fill=tk.X,expand=True,padx=8,pady=12)
        return fr

    def _make_row(self,idx,item,selected=False):
        base_bg=T["highlight"] if selected else (T["row_alt"] if idx%2==0 else T["bg"])
        fg=T["bg"] if selected else T["text"]
        sfg=T["bg"] if selected else T["subtext"]
        fr=tk.Frame(self._frame,bg=base_bg,cursor="hand2")

        icon="▶" if item.get("badge","").startswith("V") or Path(str(item.get("path",""))).suffix.lower() in VIDEO_EXTS else "♪"
        icon_col=tk.Frame(fr,bg=base_bg,width=40); icon_col.pack_propagate(False); icon_col.pack(side=tk.LEFT,fill=tk.Y)
        tk.Label(icon_col,text=icon,bg=base_bg,fg=sfg,font=("Segoe UI",14)).pack(expand=True)

        txt_col=tk.Frame(fr,bg=base_bg); txt_col.pack(side=tk.LEFT,fill=tk.BOTH,expand=True,pady=8)
        tk.Label(txt_col,text=item.get("title",""),bg=base_bg,fg=fg,
                 font=("Segoe UI",11),anchor="w").pack(fill=tk.X,padx=4)
        sub=item.get("sub","")
        # always create the sub label (even if empty) so enrichment can fill it
        tk.Label(txt_col,text=sub,bg=base_bg,fg=sfg,
                 font=("Segoe UI",9),anchor="w").pack(fill=tk.X,padx=4)

        badge=item.get("badge","")
        if badge:
            bd_col=tk.Frame(fr,bg=base_bg,width=64); bd_col.pack_propagate(False)
            bd_col.pack(side=tk.RIGHT,fill=tk.Y,padx=8)
            tk.Label(bd_col,text=badge,bg=base_bg,fg=sfg,font=("Segoe UI",8)).pack(expand=True)

        for w in fr.winfo_children()+[fr]:
            w.bind("<Button-1>",lambda e,i=idx:self._click(i))
            w.bind("<Double-Button-1>",lambda e,i=idx:self._play(i))
        return fr

    def _click(self,idx):
        if self._on_focus:
            try: self._on_focus()
            except Exception: pass
        if self._cur==idx: self._play(idx)
        else: self._hl(idx)

    def _play(self,idx):
        if 0<=idx<len(self._items):
            p=self._items[idx].get("path")
            if p: self._on_play(p)

    def update_row(self,idx,title,sub):
        """Called from enrichment via root.after(); only affects rendered rows."""
        if idx>=len(self._items): return
        item=self._items[idx]
        if item.get("is_header"): return
        item["title"]=title; item["sub"]=sub; item["_enriched"]=True
        w=self._rendered.get(idx)
        if w is None: return   # not visible right now; data is stored for later
        try:
            txt_col=w.winfo_children()[1]
            lbls=txt_col.winfo_children()
            if lbls: lbls[0].config(text=title)
            if len(lbls)>1: lbls[1].config(text=sub)
        except Exception:
            pass

    # ── selection / highlight ────────────────────────────
    def _repaint_selection(self,old_idx):
        for idx in (old_idx,self._cur):
            w=self._rendered.get(idx)
            if w is None: continue
            it=self._items[idx] if idx<len(self._items) else None
            if not it or it.get("is_header"): continue
            sel=(idx==self._cur)
            bg=T["highlight"] if sel else (T["row_alt"] if idx%2==0 else T["bg"])
            fg=T["bg"] if sel else T["text"]
            sfg=T["bg"] if sel else T["subtext"]
            try:
                w.config(bg=bg)
                cols=w.winfo_children()
                for ci,col in enumerate(cols):
                    col.config(bg=bg)
                    for gc in col.winfo_children():
                        # txt_col is index 1: title uses fg, sub/icon use sfg
                        use_fg = fg if (ci==1 and gc is col.winfo_children()[0]) else sfg
                        try: gc.config(bg=bg,fg=use_fg)
                        except Exception:
                            try: gc.config(bg=bg)
                            except Exception: pass
            except Exception: pass

    def _hl(self,idx):
        if idx<0 or idx>=len(self._items): return
        old=self._cur
        self._cur=idx
        self._scroll_into_view(idx)   # may trigger a render
        self._do_render()             # ensure target row exists
        self._repaint_selection(old)

    def _scroll_into_view(self,idx):
        if not (0<=idx<len(self._offsets)): return
        try:
            self._cv.update_idletasks()
            vh=self._cv.winfo_height() or 1
            top=self._cv.canvasy(0); bottom=top+vh
            y=self._offsets[idx]; h=self._item_h(self._items[idx])
            if y<top:
                self._cv.yview_moveto(max(0.0,y/self._total_h))
            elif y+h>bottom:
                self._cv.yview_moveto(min(1.0,(y+h-vh)/self._total_h))
        except Exception: pass

    def _first_selectable(self):
        for i,item in enumerate(self._items):
            if not item.get("is_header"): return i
        return 0

    # ── controller navigation ────────────────────────────
    def navigate(self,dy):
        idx=self._cur+dy
        safety=0
        while 0<=idx<len(self._items) and self._items[idx].get("is_header") and safety<8:
            idx+=dy; safety+=1
        idx=max(0,min(len(self._items)-1,idx))
        if self._items and not self._items[idx].get("is_header"):
            self._hl(idx)

    def at_top(self):
        return self._cur<=self._first_selectable()

    def selected_path(self):
        if self._items and self._cur<len(self._items):
            return self._items[self._cur].get("path")
        return None

    def activate(self):
        self._play(self._cur)

    def all_paths(self):
        return [it["path"] for it in self._items if not it.get("is_header") and it.get("path")]

# ─────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────
class App:
    Z_TOP="top"; Z_SEC="sec"; Z_CONT="cont"; Z_QUEUE="queue"
    Z_POPUP="popup"; Z_VKBD="vkbd"

    TABS=["Home","Video","Music","Browse","Discover"]
    SECTIONS={
        "Home":     ["Recent","Videos","Music","Playlists"],
        "Video":    ["All Videos","Recently Added","By Folder"],
        "Music":    ["All Tracks","Artists","Albums","Genres","Playlists"],
        "Browse":   ["Files","Services","URL","Disc","Saved Playlists"],
        "Discover": ["Internet Radio","Podcasts"],
    }

    def __init__(self,root,proc,api,initial_files=None):
        self.root=root; self.proc=proc; self.api=api
        self.zone=self.Z_CONT
        self.top_idx=0; self.sec_idx=0
        self.popup=None; self.vkbd=None; self._ptag=""
        self._last_a=0; self._last_btns=0
        self._top_vis=True; self._queue_open=False
        self._status={}; self._queue_items=[]
        self._cur_tab="Home"; self._cur_sec="Recent"
        self._lib_msg="Scanning library…"
        self._video_hwnd=None; self._video_vis=False
        self._last_state=""
        # random-mode state
        self._rand_auto=False          # fully-automated random loop on/off
        self._rand_job=None            # scheduled after() id for the auto loop
        self._rand_manual=False        # manual mode: seek controls → random jump
        self._r_loop=False             # r-key random-time loop
        self._r_job=None
        self._quitting=False
        self._enrich_cancel=False
        self._enrich_token=None
        self._refresh_pending=False

        self._ctrl=ControllerHub()
        if self._ctrl.ok:
            print(f"[PlayerOne] Controller input ready ({self._ctrl.count()} connected)")
        else:
            print("[PlayerOne] No controller detected (keyboard still works)")

        self._build_ui()
        self._bind_keyboard()
        self._setup_dnd()
        self._start_polls()
        self._start_watchdog()

        if initial_files:
            self.root.after(1800,lambda:self._load_files(initial_files,play=True))

        if CFG.get("scan_library",True):
            if _HAS_ML:
                self._refresh_pending = False

                def _obs(reason):
                    if reason == "scan_progress":
                        # Only update the status label — never rebuild the content list
                        msg = LIB.scan_msg
                        self.root.after(0, lambda m=msg: self._lib_lbl.config(text=f"  {m}"))
                        return
                    if reason in ("scan_done", "incremental_add",
                                  "files_added", "files_removed",
                                  "meta_prefetch_done"):
                        # Debounce: schedule one refresh, ignore duplicates within 500 ms
                        if not self._refresh_pending:
                            self._refresh_pending = True
                            self.root.after(500, self._debounced_refresh)

                def _apply_obs(r): _obs(r)
                LIB.on_update(_apply_obs)
                LIB.scan()
                LIB.start_watcher(interval=60)   # poll every 60 s instead of 30
            else:
                LIB.scan(callback=self._lib_scan_cb)
        else:
            LIB.ready = True
            self._refresh_content()

    def _debounced_refresh(self):
        self._refresh_pending = False
        self._refresh_content()

    def _lib_scan_cb(self,msg):
        """Legacy callback for fallback library."""
        self._lib_msg=msg
        if msg=="done":
            self.root.after(0,self._refresh_content)
        else:
            self.root.after(0,lambda:self._lib_lbl.config(text=f"  {msg}"))

    # ═══════════════════════════════════════════════
    # BUILD UI
    # ═══════════════════════════════════════════════
    def _build_ui(self):
        self.root.configure(bg=T["bg"])
        self.root.title("PlayerOne")
        self.root.state("zoomed")
        _set_icon(self.root)

        style=ttk.Style(); style.theme_use("clam")
        style.configure("TScale",background=T["panel"],troughcolor=T["border"],slidercolor=T["highlight"])
        style.configure("TScrollbar",background=T["border"],troughcolor=T["bg"],arrowcolor=T["subtext"],
                        gripcount=0,borderwidth=0)
        style.layout("TScrollbar",[("Scrollbar.trough",{"children":[("Scrollbar.thumb",{"expand":"1","sticky":"nswe"})]})])

        # ── top nav bar
        self._top_frame=tk.Frame(self.root,bg=T["panel"],height=48)
        self._top_frame.pack(fill=tk.X,side=tk.TOP); self._top_frame.pack_propagate(False)
        self._tab_lbls=[]
        for i,tab in enumerate(self.TABS):
            l=tk.Label(self._top_frame,text=tab,bg=T["panel"],fg=T["text"],
                       font=("Segoe UI",12,"bold"),padx=22,pady=12)
            l.pack(side=tk.LEFT)
            l.bind("<Button-1>",lambda e,i=i:self._on_tab_click(i))
            self._tab_lbls.append(l)

        # ── section bar
        self._sec_frame=tk.Frame(self.root,bg=T["panel2"],height=38)
        self._sec_frame.pack(fill=tk.X,side=tk.TOP); self._sec_frame.pack_propagate(False)
        self._sec_lbls=[]
        self._rebuild_sections()

        # ── body
        self._body=tk.Frame(self.root,bg=T["bg"])
        self._body.pack(fill=tk.BOTH,expand=True)

        # video stage (hidden until playback)
        self._vid_frame=tk.Frame(self._body,bg="black")
        self._vid_frame.bind("<Configure>",self._on_vid_resize)


        # content (rich list)
        self._content=RichList(self._body,on_play=self._play_path,
                               on_need_enrich=self._enrich_indices,
                               on_focus=lambda: setattr(self,"zone",self.Z_CONT))
        self._content.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)

        # ── queue panel (right, hidden by default)
        self._queue_panel=tk.Frame(self._body,bg=T["panel"],width=320)
        self._queue_panel.pack_propagate(False)

        qctrl=tk.Frame(self._queue_panel,bg=T["panel"]); qctrl.pack(fill=tk.X,pady=4,padx=4)
        for sym,cmd in [("↺",self.api.toggle_loop),("⇌",self.api.toggle_random),("✕",self.api.pl_empty)]:
            tk.Button(qctrl,text=sym,command=cmd,bg=T["border"],fg=T["text"],
                      relief="flat",font=("Segoe UI",12),width=3).pack(side=tk.LEFT,padx=2)
        tk.Label(qctrl,text="QUEUE",bg=T["panel"],fg=T["subtext"],
                 font=("Segoe UI",8,"bold")).pack(side=tk.LEFT,padx=10)

        self._q_list=tk.Listbox(self._queue_panel,bg=T["panel"],fg=T["text"],
                                selectbackground=T["highlight"],selectforeground=T["bg"],
                                font=("Segoe UI",10),borderwidth=0,highlightthickness=0)
        qs=ttk.Scrollbar(self._queue_panel,orient="vertical",command=self._q_list.yview)
        self._q_list.configure(yscrollcommand=qs.set)
        qs.pack(side=tk.RIGHT,fill=tk.Y)
        self._q_list.pack(fill=tk.BOTH,expand=True,padx=4,pady=4)
        self._q_list.bind("<Double-Button-1>",self._queue_activate)

        # ── playback bar (taller to hold artwork)
        pb=tk.Frame(self.root,bg=T["panel"],height=90)
        pb.pack(fill=tk.X,side=tk.BOTTOM); pb.pack_propagate(False)
        self._playback_bar=pb

        # Album art thumbnail (64×64)
        self._art_frame=tk.Frame(pb,bg=T["panel"],width=64,height=64)
        self._art_frame.pack_propagate(False)
        self._art_frame.pack(side=tk.LEFT,padx=(10,6),pady=10)
        self._art_lbl=tk.Label(self._art_frame,text="♪",bg=T["border"],fg=T["subtext"],
                                font=("Segoe UI",22),width=4,height=2)
        self._art_lbl.pack(fill=tk.BOTH,expand=True)
        self._art_photo=None   # keep reference to avoid GC

        # Track info + progress
        info_col=tk.Frame(pb,bg=T["panel"]); info_col.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)

        top_row=tk.Frame(info_col,bg=T["panel"]); top_row.pack(fill=tk.X,padx=4,pady=(8,0))
        self._now_lbl=tk.Label(top_row,text="PlayerOne",bg=T["panel"],fg=T["text"],
                               font=("Segoe UI",11,"bold"),anchor="w")
        self._now_lbl.pack(side=tk.LEFT,fill=tk.X,expand=True)
        self._time_lbl=tk.Label(top_row,text="0:00 / 0:00",bg=T["panel"],fg=T["subtext"],
                                font=("Segoe UI",9))
        self._time_lbl.pack(side=tk.RIGHT,padx=8)

        self._prog_var=tk.DoubleVar()
        prog=ttk.Scale(info_col,from_=0,to=100,variable=self._prog_var,
                  orient="horizontal")
        prog.pack(fill=tk.X,padx=4,pady=2)
        prog.bind("<ButtonRelease-1>", self._on_seek_click)

        br=tk.Frame(info_col,bg=T["panel"]); br.pack(pady=2,anchor="w")
        self._state_sym=tk.Label(br,text="■",bg=T["panel"],fg=T["subtext"],
                                 font=("Segoe UI",14))
        self._state_sym.pack(side=tk.LEFT,padx=(4,8))
        for sym,cmd in [("⏮",self.api.prev),("⏯",self.api.play_pause),
                        ("⏹",self.api.stop),("⏭",self.api.next)]:
            tk.Button(br,text=sym,command=cmd,bg=T["panel"],fg=T["text"],
                      font=("Segoe UI",15),relief="flat",padx=8).pack(side=tk.LEFT,padx=1)
        self._vol_lbl=tk.Label(br,text="Vol: --",bg=T["panel"],fg=T["subtext"],
                               font=("Segoe UI",10))
        self._vol_lbl.pack(side=tk.LEFT,padx=14)

        # ── search bar (hidden by default, shown when activated)
        self._search_frame=tk.Frame(self.root,bg=T["panel"],height=38)
        self._search_var=tk.StringVar()
        self._search_active=False
        search_inner=tk.Frame(self._search_frame,bg=T["panel"])
        search_inner.pack(fill=tk.X,padx=8,pady=4)
        tk.Label(search_inner,text="🔍",bg=T["panel"],fg=T["highlight"],
                 font=("Segoe UI",12)).pack(side=tk.LEFT,padx=4)
        self._search_entry=tk.Entry(search_inner,textvariable=self._search_var,
                                    bg=T["panel2"],fg=T["text"],insertbackground=T["highlight"],
                                    font=("Segoe UI",11),relief="flat",bd=0)
        self._search_entry.pack(side=tk.LEFT,fill=tk.X,expand=True,padx=4)
        self._search_entry.bind("<Return>",lambda e:self._do_search())
        self._search_entry.bind("<Escape>",lambda e:self._close_search())
        self._search_var.trace_add("write",lambda *_:self._search_debounce())
        tk.Button(search_inner,text="✕",command=self._close_search,
                  bg=T["panel"],fg=T["subtext"],relief="flat",
                  font=("Segoe UI",11)).pack(side=tk.RIGHT,padx=4)
        self._search_debounce_id=None

        # ── status / hint footer
        foot=tk.Frame(self.root,bg=T["bg"]); foot.pack(fill=tk.X,side=tk.BOTTOM)
        self._foot=foot
        self._fullscreen=False
        self._lib_lbl=tk.Label(foot,text="",bg=T["bg"],fg=T["subtext"],
                               font=("Segoe UI",8),anchor="w")
        self._lib_lbl.pack(side=tk.LEFT,padx=8)
        hint=("↑↓←→ Navigate  Enter Confirm  Backspace Back  Space Play/Pause  "
              "F Fullscreen  / Search  M Menu  |  "
              "[START] Menu  [SELECT] Options  [X] Filter  [Y] Queue  [L3] Settings  [R3] Fullscreen")
        tk.Label(foot,text=hint,bg=T["bg"],fg=T["subtext"],
                 font=("Segoe UI",8),anchor="e").pack(side=tk.RIGHT,padx=8)

        self._update_tab_hl(); self._update_sec_hl()

    # ── section bar rebuild
    def _rebuild_sections(self):
        for l in self._sec_lbls: l.destroy()
        self._sec_lbls=[]
        secs=self.SECTIONS.get(self._cur_tab,[])
        for i,s in enumerate(secs):
            l=tk.Label(self._sec_frame,text=s,bg=T["panel2"],fg=T["subtext"],
                       font=("Segoe UI",10),padx=14,pady=8)
            l.pack(side=tk.LEFT)
            l.bind("<Button-1>",lambda e,i=i:self._on_sec_click(i))
            self._sec_lbls.append(l)
        self.sec_idx=0
        self._cur_sec=secs[0] if secs else ""

    def _update_tab_hl(self):
        for i,l in enumerate(self._tab_lbls):
            z_top=(self.zone==self.Z_TOP)
            l.config(fg=T["highlight"] if i==self.top_idx else T["text"],
                     font=("Segoe UI",12,"bold") if (i==self.top_idx and z_top) else ("Segoe UI",11))

    def _update_sec_hl(self):
        for i,l in enumerate(self._sec_lbls):
            z_sec=(self.zone==self.Z_SEC)
            l.config(fg=T["highlight"] if i==self.sec_idx else T["subtext"],
                     font=("Segoe UI",10,"bold") if (i==self.sec_idx and z_sec) else ("Segoe UI",10))

    # ═══════════════════════════════════════════════
    # KEYBOARD BINDINGS
    # ═══════════════════════════════════════════════
    def _bind_keyboard(self):
        """
        Bind keyboard keys so the app is fully usable without a controller.
        Return / Enter  → confirm (same as A button)
        BackSpace / Esc → back    (same as B button)
        Arrow keys      → navigate
        Space           → play/pause
        F               → fullscreen
        Ctrl+F / /      → open search
        """
        r = self.root

        # Mouse: after clicking anywhere that isn't a text field, return keyboard
        # focus to the root so Enter/Space/etc. keep working alongside the mouse.
        def _global_click(e):
            w = getattr(e, "widget", None)
            if isinstance(w, (tk.Entry, tk.Text)):
                return
            try: self.root.focus_set()
            except Exception: pass
        r.bind("<Button-1>", _global_click, add="+")

        # Navigation
        r.bind("<Up>",        lambda e: self._kb_nav(0, -1))
        r.bind("<Down>",      lambda e: self._kb_nav(0,  1))
        r.bind("<Left>",      lambda e: self._kb_nav(-1, 0))
        r.bind("<Right>",     lambda e: self._kb_nav( 1, 0))

        # Confirm / back
        r.bind("<Return>",    lambda e: self._kb_confirm())
        r.bind("<KP_Enter>",  lambda e: self._kb_confirm())
        r.bind("<BackSpace>", lambda e: self._kb_back())
        r.bind("<Escape>",    lambda e: self._kb_escape())

        # Playback shortcuts
        r.bind("<space>",     lambda e: self._kb_space())
        r.bind("<f>",         lambda e: self._toggle_fullscreen())
        r.bind("<F>",         lambda e: self._toggle_fullscreen())
        r.bind("<Prior>",     lambda e: self.api.prev())   # Page Up
        r.bind("<Next>",      lambda e: self.api.next())   # Page Down
        r.bind("<period>",    lambda e: self.api.next())
        r.bind("<comma>",     lambda e: self.api.prev())

        # Search
        r.bind("<Control-f>", lambda e: self._open_search())
        r.bind("<slash>",     lambda e: self._open_search())

        # Menus
        r.bind("<m>",         lambda e: self._open_start())
        r.bind("<Menu>",      lambda e: self._open_context())  # context-menu key

        # random-mode controls
        r.bind("<r>",         lambda e: self._kb_r())
        r.bind("<R>",         lambda e: self._kb_r())
        r.bind("<bracketleft>",  lambda e: self._seek_or_random(-10))  # [ : back 10s / random
        r.bind("<bracketright>", lambda e: self._seek_or_random(+10))  # ] : fwd 10s / random

    def _kb_r(self):
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return
        self._on_r_key()


    def _kb_nav(self, dx, dy):
        """Keyboard navigation — skip if focus is in a text entry widget."""
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return   # let the widget handle its own arrows
        self._nav(dx, dy)

    def _kb_confirm(self):
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return
        self._btn_a()

    def _kb_back(self):
        focused = self.root.focus_get()
        if isinstance(focused, tk.Entry):
            return   # let entry handle BackSpace for text editing
        if self._search_active:
            self._close_search()
            return
        self._btn_b()

    def _kb_escape(self):
        # Escape leaves fullscreen first; otherwise behaves like Back.
        if getattr(self, "_fullscreen", False):
            self._toggle_fullscreen()
            return
        self._kb_back()

    def _kb_space(self):
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return
        t = time.time()
        if t - self._last_a >= CFG["cooldown_a"]:
            self._last_a = t
            self.api.play_pause()
    def _setup_dnd(self):
        """
        Register a WM_DROPFILES handler by subclassing the Tk HWND.

        On 64-bit Windows every WNDPROC parameter is pointer-sized (8 bytes).
        Using c_long (4 bytes) for HWND/WPARAM/LPARAM corrupts the stack and
        causes the access-violation we saw.  The fix is to use c_ssize_t
        (alias for LONG_PTR / INT_PTR) throughout, and to set explicit
        argtypes on every ctypes call we make.
        """
        if os.name != "nt":
            return
        if getattr(self, "_dnd_installed", False):
            return
        try:
            u32   = ctypes.windll.user32
            sh32  = ctypes.windll.shell32
            HWND  = ctypes.c_ssize_t   # pointer-sized on both 32 and 64-bit
            UINT  = ctypes.c_uint
            WP    = ctypes.c_ssize_t   # WPARAM  = UINT_PTR
            LP    = ctypes.c_ssize_t   # LPARAM  = LONG_PTR
            LRES  = ctypes.c_ssize_t   # LRESULT = LONG_PTR
            WM_DROPFILES = 0x0233
            GWLP_WNDPROC = -4

            hwnd = HWND(self.root.winfo_id())

            # Tell shell32 we accept drops
            sh32.DragAcceptFiles.argtypes = [HWND, ctypes.c_bool]
            sh32.DragAcceptFiles.restype  = None
            sh32.DragAcceptFiles(hwnd, True)

            # Build the WNDPROC function type with correct pointer-sized args
            WndProcType = ctypes.WINFUNCTYPE(LRES, HWND, UINT, WP, LP)

            # Get / set the window procedure pointer
            # GetWindowLongPtrW is the 64-bit version; fall back to GetWindowLongW
            try:
                _gwl = u32.GetWindowLongPtrW
                _swl = u32.SetWindowLongPtrW
            except AttributeError:
                _gwl = u32.GetWindowLongW
                _swl = u32.SetWindowLongW

            _gwl.argtypes = [HWND, ctypes.c_int]
            _gwl.restype  = ctypes.c_ssize_t
            _swl.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
            _swl.restype  = ctypes.c_ssize_t

            old_proc_addr = _gwl(hwnd, GWLP_WNDPROC)

            # CallWindowProcW needs the same pointer-sized signature
            _cwp = u32.CallWindowProcW
            _cwp.argtypes = [ctypes.c_ssize_t, HWND, UINT, WP, LP]
            _cwp.restype  = LRES

            def _new_proc(hwnd2, msg, wp, lp):
                # This runs inside Windows' message pump.  If a Python
                # exception ever unwinds INTO native code here, the whole
                # process crashes (no traceback) — which is exactly the
                # drag-and-drop crash.  So we swallow everything and always
                # return a plain integer.
                try:
                    if msg == WM_DROPFILES:
                        self.root.after(0, self._handle_drop, int(wp))
                        return 0
                    return int(_cwp(old_proc_addr, hwnd2, msg, wp, lp))
                except Exception:
                    try:
                        return int(_cwp(old_proc_addr, hwnd2, msg, wp, lp))
                    except Exception:
                        return 0

            # Keep a strong reference so the callback is never garbage-collected
            self._wndproc = WndProcType(_new_proc)
            # SetWindowLongPtrW expects a plain integer for the new wndproc address.
            new_addr = ctypes.cast(self._wndproc, ctypes.c_void_p).value
            _swl(hwnd, GWLP_WNDPROC, new_addr)
            self._dnd_installed = True

        except Exception as e:
            print(f"[PlayerOne] DnD setup failed (drag-and-drop disabled): {e}")

    def _handle_drop(self, hdrop):
        """
        Process a WM_DROPFILES HDROP handle.
        hdrop arrives as a c_ssize_t from our WNDPROC; cast it to HANDLE (void*).
        """
        try:
            sh32 = ctypes.windll.shell32
            HDROP   = ctypes.c_ssize_t
            UINT    = ctypes.c_uint
            LPWSTR  = ctypes.c_wchar_p

            sh32.DragQueryFileW.argtypes = [HDROP, UINT, LPWSTR, UINT]
            sh32.DragQueryFileW.restype  = UINT
            sh32.DragFinish.argtypes     = [HDROP]
            sh32.DragFinish.restype      = None

            handle = HDROP(hdrop)
            n = sh32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            files = []
            for i in range(n):
                sz  = sh32.DragQueryFileW(handle, i, None, 0) + 1
                buf = ctypes.create_unicode_buffer(sz)
                sh32.DragQueryFileW(handle, i, buf, sz)
                files.append(buf.value)
            sh32.DragFinish(handle)
            if files:
                self._load_files(files, play=True)
        except Exception as e:
            print(f"[PlayerOne] Drop handler error: {e}")

    # ═══════════════════════════════════════════════
    # FILE LOADING  (drag-drop + CLI + open dialog)
    # ═══════════════════════════════════════════════
    def _load_files(self,raw_paths,play=True):
        expanded=[]
        for raw in raw_paths:
            p=Path(str(raw).strip())
            if not p.exists(): continue
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                        expanded.append(f)
            elif p.suffix.lower() in PLAYLIST_EXTS:
                expanded+=parse_playlist(p)
            elif p.suffix.lower() in MEDIA_EXTS:
                expanded.append(p)
        if not expanded: return
        LIB.add_files(expanded)
        self.api.enqueue_many([str(f) for f in expanded])
        if play:
            self.root.after(250,lambda:self.api.cmd("pl_play"))
            if Path(expanded[0]).suffix.lower() in VIDEO_EXTS:
                self._switch_to_video_tab()
        self._refresh_content()

    def _is_video_target(self, raw):
        """True if a path/URL looks like a video by extension (handles URLs
        with query strings / fragments too)."""
        low = str(raw).lower().split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return any(low.endswith(ext) for ext in VIDEO_EXTS)

    def _switch_to_video_tab(self):
        """Bring the Video tab forward (e.g. because a video just started)."""
        if self._cur_tab == "Video":
            return
        try:
            idx = self.TABS.index("Video")
        except ValueError:
            return
        self._select_tab(idx)
        self.zone = self.Z_CONT
        self._update_tab_hl(); self._update_sec_hl()

    def _play_path(self, path):
        """Play a single file, URL, or activate a special item."""
        raw = str(path)

        # Special non-playable items
        if raw.startswith("__"):
            self._activate_special(raw)
            return

        # Network stream / URL — pass directly
        if raw.startswith(("http://", "https://", "rtsp://",
                            "mms://", "rtp://", "udp://")):
            print(f"[PlayerOne] Play URL: {raw}")
            self.api.enqueue(raw, play_first=True)
            if self._is_video_target(raw):
                self._switch_to_video_tab()
            return

        # Local file
        p = Path(raw)
        if not p.exists():
            print(f"[PlayerOne] File not found: {p}")
            return

        if p.suffix.lower() in PLAYLIST_EXTS:
            files = parse_playlist(p)
            if files:
                print(f"[PlayerOne] Play playlist: {p}  ({len(files)} tracks)")
                self.api.enqueue_many([str(f) for f in files])
                if Path(files[0]).suffix.lower() in VIDEO_EXTS:
                    self._switch_to_video_tab()
            return

        uri = p.as_uri()   # always file:///C:/... — never a bare path
        print(f"[PlayerOne] Play: {uri}")
        self.api.enqueue(uri, play_first=True)
        if p.suffix.lower() in VIDEO_EXTS:
            self._switch_to_video_tab()

    # ═══════════════════════════════════════════════
    # TAB / SECTION / CONTENT
    # ═══════════════════════════════════════════════
    def _select_tab(self,idx):
        self.top_idx=idx
        prev_tab=self._cur_tab
        self._cur_tab=self.TABS[idx]
        # Leaving the Video tab → hide its stage
        if prev_tab=="Video" and self._cur_tab!="Video":
            self._hide_video()
        # Entering Video while playing → re-show video
        if self._cur_tab=="Video" and self._last_state in("playing","paused"):
            self._show_video()
            if self._video_hwnd is None:
                threading.Thread(target=self._embed_async,daemon=True).start()
        self._rebuild_sections()
        self._update_tab_hl(); self._update_sec_hl()
        self._refresh_content()

    def _select_section(self,idx):
        self.sec_idx=idx
        secs=self.SECTIONS.get(self._cur_tab,[])
        self._cur_sec=secs[idx] if idx<len(secs) else ""
        self._update_sec_hl()
        self._refresh_content()
        # URL section goes straight to the stream-entry keyboard
        if self._cur_tab=="Browse" and self._cur_sec=="URL":
            self.root.after(80, self._open_url_dlg)

    def _refresh_content(self):
        """
        Build content items and load them.
        Tag reads (LIB.meta) are NEVER done on the main thread for large lists —
        they block on disk I/O and freeze the UI.  Instead we:
          1. Immediately load filename-based rows (instant, no disk I/O)
          2. Kick a background thread to enrich them with ID3 tags
          3. Background thread calls self.root.after() to update rows in-place
        """
        t, s = self._cur_tab, self._cur_sec

        # --- cancel any in-progress enrichment ---
        self._enrich_cancel = True

        items = []
        if t == "Home":
            if s == "Recent":       items = self._items_recent_fast()
            elif s == "Videos":     items = self._items_fast(LIB.recently_added_videos(), "video")
            elif s == "Music":      items = self._items_fast(LIB.recently_added_audio(),  "audio")
            elif s == "Playlists":  items = self._items_fast(LIB.playlists, "playlist")

        elif t == "Video":
            if s == "All Videos":      items = self._items_fast(LIB.videos, "video")
            elif s == "Recently Added":items = self._items_fast(LIB.recently_added_videos(), "video")
            elif s == "By Folder":     items = self._items_by_folder()


        elif t == "Music":
            if s == "All Tracks":  items = self._items_fast(LIB.audio, "audio")
            elif s == "Artists":   items = self._items_grouped_fast(LIB.by_artist_cached())
            elif s == "Albums":    items = self._items_albums_fast()
            elif s == "Genres":    items = self._items_grouped_fast(LIB.by_genre_cached())
            elif s == "Playlists": items = self._items_fast(LIB.playlists, "playlist")

        elif t == "Browse":
            if s == "Files":              items = self._items_fast(LIB.all_media(), "mixed")
            elif s == "Services":         items = self._items_services()
            elif s == "URL":              items = self._items_url_prompt()
            elif s == "Disc":             items = self._items_disc()
            elif s == "Saved Playlists":  items = self._items_fast(LIB.playlists, "playlist")

        elif t == "Discover":
            if s == "Internet Radio": items = self._items_radio()
            elif s == "Podcasts":     items = self._items_podcasts()

        # New enrichment generation BEFORE load(): load() renders visible rows
        # and immediately requests enrichment for them, so the token must be
        # current or that first batch would be cancelled instantly.
        self._enrich_token = object()
        self._content.load(items)

        # lib status footer
        if LIB.ready:
            self._lib_lbl.config(
                text=f"  {len(LIB.videos)} videos · {len(LIB.audio)} tracks · {len(LIB.playlists)} playlists")
        else:
            self._lib_lbl.config(text=f"  {getattr(LIB,'scan_msg','Scanning…')}")

        # Enrichment is lazy: RichList calls self._enrich_indices() with the
        # indices of the rows actually on screen, so we never read 8000 files'
        # tags up front (that, plus building 8000 widgets, was the freeze).

    def _enrich_indices(self, indices):
        """Enrich only the given (visible) rows in the background."""
        items = self._content._items
        token = self._enrich_token
        to_do = []
        for idx in indices:
            if 0 <= idx < len(items):
                it = items[idx]
                if it.get("_enrich") and not it.get("is_header") and not it.get("_enriched"):
                    it["_enriched"] = True     # claim it so we don't re-queue
                    to_do.append((idx, it.get("path")))
        if not to_do:
            return
        def _work():
            for idx, p in to_do:
                if self._enrich_token is not token:
                    return
                if not p:
                    continue
                try:
                    m = LIB.meta(Path(p))
                    title = m.get("title") or Path(p).stem
                    sub   = " · ".join(filter(None, [m.get("artist",""), m.get("album","")]))
                except Exception:
                    continue
                self.root.after(0, self._content.update_row, idx, title, sub)
        threading.Thread(target=_work, daemon=True).start()

    def _enrich_rows(self, audio_items, token):
        """Background: read tags and post row-update callbacks to Tk thread."""
        for idx, item in audio_items:
            if self._enrich_token is not token:
                return   # section changed, abort
            p = item.get("path")
            if not p:
                continue
            try:
                m = LIB.meta(Path(p))
                title = m.get("title") or Path(p).stem
                sub   = " · ".join(filter(None, [m.get("artist",""), m.get("album","")]))
            except Exception:
                continue
            # post update back to UI thread
            self.root.after(0, self._content.update_row, idx, title, sub)

    # ── item builders (FAST — filename only, no tag reads) ────────────────────
    def _mk(self, path, title, sub="", badge="", is_header=False, _enrich=False):
        return {"path": path, "title": title, "sub": sub,
                "badge": badge, "is_header": is_header, "_enrich": _enrich}

    def _items_fast(self, paths, kind):
        """Build rows using filename only — no LIB.meta calls."""
        out = []
        for p in paths:
            p = Path(p)
            if kind == "audio":
                # Show filename as title; subtitle will be filled in by _enrich_rows
                out.append(self._mk(p, p.stem, "", _enrich=True))
            elif kind == "playlist":
                out.append(self._mk(p, p.stem, str(p.parent)))
            else:
                out.append(self._mk(p, p.stem, str(p.parent)))
        return out

    def _items_flat(self, paths, kind):
        """Alias kept for compatibility — routes to fast version."""
        return self._items_fast(paths, kind)

    def _items_recent_fast(self):
        rv = LIB.recently_added_videos(20)
        ra = LIB.recently_added_audio(20)
        if not rv and not ra:
            if not LIB.ready:
                return [self._mk(None, "Scanning library…", "This may take a moment")]
            return [self._mk(None, "No media found",
                             "Add folders via Settings → Library Settings, or drop files here")]
        out = []
        if rv:
            out.append(self._mk(None, "Recent Videos", is_header=True))
            for p in rv[:10]:
                out.append(self._mk(p, p.stem, str(p.parent)))
        if ra:
            out.append(self._mk(None, "Recent Music", is_header=True, _enrich=False))
            for p in ra[:10]:
                out.append(self._mk(p, p.stem, "", _enrich=True))
        return out

    def _items_grouped_fast(self, groups):
        """Groups dict: {name: [Path, …]}  — no tag reads."""
        out = []
        for group_name, files in groups.items():
            out.append(self._mk(None, group_name, is_header=True))
            for p in files:
                out.append(self._mk(Path(p), Path(p).stem, "", _enrich=True))
        return out

    def _items_albums_fast(self):
        out = []
        for al, info in LIB.by_album_cached().items():
            out.append(self._mk(None, al, info.get("artist",""), is_header=True))
            for p in info.get("files",[]):
                out.append(self._mk(Path(p), Path(p).stem, "", _enrich=True))
        return out

    def _items_by_folder(self):
        out=[]
        for folder,files in LIB.video_folders().items():
            out.append(self._mk(None,Path(folder).name,folder,is_header=True))
            for p in files:
                out.append(self._mk(p,p.stem))
        return out


    DEFAULT_SERVICES = [
        ("NASA TV (Public)",   "https://ntv1.akamaized.net/hls/live/2014075/NASA-NTV1-HLS/master.m3u8"),
        ("Red Bull TV",        "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8"),
        ("Al Jazeera English", "https://live-hls-web-aje.getaj.net/AJE/index.m3u8"),
        ("DW English",         "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8"),
        ("Bloomberg TV",       "https://bloomberg.com/media-manifest/streams/us.m3u8"),
    ]

    def _items_services(self):
        """Configured streaming services (config `services`) + built-ins."""
        items=[self._mk(None,"📡  Streaming Services",is_header=True)]
        user=CFG.get("services",[])
        if user:
            for svc in user:
                items.append(self._mk(svc.get("url",""),
                                      svc.get("name","Service"),
                                      "Custom · "+svc.get("url","")))
            items.append(self._mk(None,"Built-in",is_header=True))
        for name,url in self.DEFAULT_SERVICES:
            items.append(self._mk(url,name,"Built-in stream · Press A to play"))
        items.append(self._mk("__add_service__","＋ Add service…",
                              "Enter a stream / playlist URL"))
        return items

    def _add_service_dlg(self):
        var=tk.StringVar()
        def _done(url):
            if url.strip():
                name=url.split("/")[2] if "//" in url else url[:24]
                CFG.setdefault("services",[]).append(
                    {"name":name,"url":url.strip()})
                _save_config()
                self._refresh_content()
        self.vkbd=VirtualKeyboard(self.root,var,_done,
                                  placeholder="https://example.com/stream.m3u8")
        self.zone=self.Z_VKBD

    def _items_url_prompt(self):
        return [self._mk("__url__","Open Network URL or Stream",
                         "Press A / Enter to type a URL")]

    def _items_disc(self):
        drives=[]
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            d=Path(f"{letter}:/")
            if d.exists():
                try:
                    # check for VIDEO_TS or AUDIO_TS (DVD)
                    if (d/"VIDEO_TS").exists() or (d/"AUDIO_TS").exists():
                        drives.append(self._mk(f"dvd:///{letter}:/","DVD – Drive "+letter,str(d)))
                    else:
                        drives.append(self._mk(f"file:///{letter}:/",f"Drive {letter}",str(d)))
                except Exception:
                    drives.append(self._mk(f"file:///{letter}:/",f"Drive {letter}"))
        if not drives:
            drives=[self._mk(None,"No removable drives detected",
                             "Insert a disc or connect a device")]
        return drives

    def _items_radio(self):
        """
        Show curated built-in stations + any user-configured ones from config.
        Fetching live from radio-browser.info happens in background; we show
        the built-in list immediately and append discovered stations after.
        """
        BUILTIN = [
            ("BBC World Service",       "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"),
            ("BBC Radio 1",             "http://stream.live.vc.bbcmedia.co.uk/bbc_radio_one"),
            ("NPR News",                "https://npr-ice.streamguys1.com/live.mp3"),
            ("KEXP 90.3 Seattle",       "https://kexp-mp3-128.streamguys1.com/kexp128.mp3"),
            ("Jazz 24",                 "https://live.wostreaming.net/manifest/ppm-jazz24aac-ibc1"),
            ("Classical KUSC",          "https://kusc.streamguys1.com/kusc128.mp3"),
            ("SomaFM Groove Salad",     "https://ice6.somafm.com/groovesalad-128-mp3"),
            ("SomaFM Drone Zone",       "https://ice6.somafm.com/dronezone-128-mp3"),
            ("Radio Paradise",          "https://stream.radioparadise.com/mp3-192"),
            ("1.FM Top 40",             "https://strm112.1.fm/top40_mobile_mp3"),
        ]
        items = [self._mk(None, "🎙  Internet Radio", is_header=True)]
        for name, url in BUILTIN:
            items.append(self._mk(url, name, "Built-in · Click to play"))
        for r in CFG.get("radio_feeds", []):
            items.append(self._mk(r.get("url",""), r.get("name","Custom Station"),
                                  "Custom · " + r.get("url","")))
        items.append(self._mk("__add_radio__", "＋ Add custom station…",
                              "Opens a URL entry field"))
        return items

    def _items_podcasts(self):
        """Show configured podcast feeds; fetch episode lists in background."""
        feeds = CFG.get("podcast_feeds", [])
        if not feeds:
            return [
                self._mk(None, "🎙  Podcasts", is_header=True),
                self._mk("__add_podcast__", "＋ Add podcast feed…",
                         "Enter an RSS/Atom feed URL"),
            ]
        items = [self._mk(None, "🎙  Podcast Feeds", is_header=True)]
        for feed in feeds:
            items.append(self._mk(feed.get("url",""),
                                  feed.get("name","Podcast"),
                                  "Click to expand episodes"))
        items.append(self._mk("__add_podcast__", "＋ Add podcast feed…", ""))
        return items

    def _fetch_podcast_episodes(self, rss_url, feed_name):
        """Background: fetch RSS feed and inject episode rows into content."""
        def _run():
            try:
                req = urllib.request.Request(rss_url,
                    headers={"User-Agent": "PlayerOne/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    xml = r.read().decode("utf-8", "replace")
                episodes = []
                for m in re.finditer(
                        r"<item>.*?<title>([^<]+)</title>.*?"
                        r"<enclosure[^>]+url=\"([^\"]+)\"[^>]*/?>",
                        xml, re.DOTALL):
                    episodes.append((m.group(1).strip(), m.group(2).strip()))
                if episodes:
                    new_items = [self._mk(None, feed_name, is_header=True)]
                    for title, url in episodes[:40]:
                        new_items.append(self._mk(url, title, "Podcast episode"))
                    self.root.after(0, self._content.load, new_items)
                else:
                    self.root.after(0, self._content.load, [
                        self._mk(None, "No episodes found", rss_url)])
            except Exception as e:
                self.root.after(0, self._content.load, [
                    self._mk(None, "Could not fetch feed", str(e))])
        threading.Thread(target=_run, daemon=True).start()

    # ── special item activations (URL prompt, add radio, podcast expand) ──────
    def _activate_special(self, path_str):
        """Called by _play_path for non-file special item keys."""
        if path_str == "__url__":
            self._open_url_dlg()
        elif path_str == "__add_service__":
            self._add_service_dlg()
        elif path_str == "__add_radio__":
            self._add_radio_dlg()
        elif path_str == "__add_podcast__":
            self._add_podcast_dlg()
        elif path_str.startswith("http") and "rss" in path_str.lower():
            # treat as podcast feed
            self._fetch_podcast_episodes(path_str, "Podcast")

    def _add_radio_dlg(self):
        var = tk.StringVar()
        def _done(url):
            if url.strip():
                name = url.split("/")[-1] or "Custom Station"
                CFG.setdefault("radio_feeds", []).append(
                    {"name": name, "url": url.strip()})
                _save_config()
                self._refresh_content()
        self.vkbd = VirtualKeyboard(self.root, var, _done,
                                    placeholder="https://stream.example.com/radio.mp3")
        self.zone = self.Z_VKBD

    def _add_podcast_dlg(self):
        var = tk.StringVar()
        def _done(url):
            if url.strip():
                CFG.setdefault("podcast_feeds", []).append(
                    {"name": "Podcast", "url": url.strip()})
                _save_config()
                self._refresh_content()
        self.vkbd = VirtualKeyboard(self.root, var, _done,
                                    placeholder="https://feeds.example.com/podcast.rss")
        self.zone = self.Z_VKBD

    def _all_media(self):
        with LIB._lock:
            return LIB.videos+LIB.audio

    # ═══════════════════════════════════════════════
    # STATUS / QUEUE POLL
    # ═══════════════════════════════════════════════
    def _start_polls(self):
        self._poll_status()

    def _poll_status(self):
        def _fetch():
            st=self.api.status(); pl=self.api.playlist()
            self.root.after(0,self._apply_status,st,pl)
        threading.Thread(target=_fetch,daemon=True).start()
        self.root.after(1000,self._poll_status)

    def _on_seek_click(self, event):
        """Seek to position when user clicks the progress bar."""
        try:
            length = self._status.get("length", 0)
            if length <= 0: return
            pct = self._prog_var.get() / 100.0
            target = int(pct * length)
            self.api.cmd("seek", val=str(target))
        except Exception:
            pass

    def _apply_status(self, st, pl):
        if not st: return
        self._status = st
        info = st.get("information", {}).get("category", {}).get("meta", {})
        title  = info.get("title","") or info.get("filename","") or "PlayerOne"
        artist = info.get("artist","") or info.get("now_playing","") or ""
        display = f"{title}  —  {artist}" if artist else title
        self._now_lbl.config(text=display)

        length = st.get("length", 0) or 1
        pos    = st.get("time", 0)
        self._prog_var.set((pos / length) * 100)

        def _fmt(s):
            s = int(s)
            return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}" if s >= 3600 \
                   else f"{s//60}:{s%60:02d}"
        self._time_lbl.config(text=f"{_fmt(pos)} / {_fmt(length)}")

        vol = int(st.get("volume", 0) / 2.56)
        self._vol_lbl.config(text=f"🔊 {vol}%")

        state = st.get("state", "")
        col = T["green"] if state=="playing" else T["highlight"] if state=="paused" else T["subtext"]
        sym = "▶" if state=="playing" else "⏸" if state=="paused" else "■"
        self._state_sym.config(text=sym, fg=col)

        if state != self._last_state:
            self._last_state = state
            self._on_state_change(state)
            # Try to show album art when a new track starts
            if state == "playing":
                # Fallback: if VLC reports an actual video track and we're not
                # already on the Video tab, switch to it (covers streams whose
                # URL didn't reveal the type).
                if st.get("has_video") and self._cur_tab != "Video":
                    self.root.after(0, self._switch_to_video_tab)
                threading.Thread(target=self._load_art, args=(info,),
                                 daemon=True).start()

        if pl:
            self._refresh_queue(pl)

    def _load_art(self, info):
        """Background: find album art from cache or art folder and update thumbnail."""
        try:
            import tkinter.font
            from pathlib import Path as P

            # Try to get art from the cached library file
            filename = info.get("filename", "")
            if filename:
                p = P(filename)
                art_path = LIB.art_path(p) if hasattr(LIB, "art_path") else None
                if art_path and art_path.exists():
                    raw = art_path.read_bytes()
                    self.root.after(0, self._set_art_bytes, raw)
                    return

            # No cached art — clear the thumbnail
            self.root.after(0, self._clear_art)
        except Exception:
            self.root.after(0, self._clear_art)

    def _set_art_bytes(self, raw_bytes):
        """Display album art bytes in the thumbnail label (main thread)."""
        try:
            import io
            # Tkinter can load PNG/GIF natively; JPEG needs PIL.
            # Try PhotoImage directly first (works for PNG)
            try:
                img = tk.PhotoImage(data=raw_bytes)
            except tk.TclError:
                # Try PIL if available
                try:
                    from PIL import Image, ImageTk
                    img = ImageTk.PhotoImage(Image.open(io.BytesIO(raw_bytes)).resize((64,64)))
                except ImportError:
                    self._clear_art(); return

            # Subsample to fit 64×64
            try:
                w, h = img.width(), img.height()
                factor = max(1, max(w, h) // 64)
                img = img.subsample(factor, factor)
            except Exception:
                pass

            self._art_photo = img   # keep reference
            self._art_lbl.config(image=img, text="")
        except Exception:
            self._clear_art()

    def _clear_art(self):
        self._art_lbl.config(image="", text="♪")
        self._art_photo = None

    def _refresh_queue(self,pl):
        self._q_list.delete(0,tk.END)
        self._queue_items=[]
        ch=[]
        if isinstance(pl,dict) and pl.get("children"):
            for c in pl["children"]:
                if isinstance(c,dict) and c.get("children"):
                    ch=c["children"]; break
        elif isinstance(pl,list):
            ch=pl
        for it in ch:
            if not isinstance(it,dict): continue
            self._queue_items.append({"id":it.get("id",""),"name":it.get("name","?")})
            self._q_list.insert(tk.END,f"  {it.get('name','?')}")

    # ═══════════════════════════════════════════════
    # VIDEO EMBEDDING
    # ═══════════════════════════════════════════════
    def _on_state_change(self,state):
        if state in ("playing","paused"):
            if self._cur_tab == "Video":
                self._show_video()
                if self._video_hwnd is None:
                    threading.Thread(target=self._embed_async, daemon=True).start()
        elif state in ("stopped",""):
            self._hide_video()

    def _show_video(self):
        if self._video_vis: return
        self._content.pack_forget()
        self._vid_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._video_vis = True

    def _hide_video(self):
        if not self._video_vis: return
        self._vid_frame.pack_forget()
        self._content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._video_vis = False
        self._video_hwnd = None

    def _embed_async(self, retries=40, delay=0.25):
        """Background thread: find VLC's video HWND and embed it."""
        if not self.proc: return
        for _ in range(retries):
            h = find_video_hwnd(self.proc.pid)
            if h:
                self.root.after(0, self._finish_embed, h)
                return
            time.sleep(delay)

    def _finish_embed(self, h):
        try:
            # VLC may be minimized-to-tray; restore it so it embeds at full size
            try:
                if os.name == "nt":
                    _u32.ShowWindow(h, 9)   # SW_RESTORE
            except Exception:
                pass
            container = self._vid_frame.winfo_id()
            embed_hwnd(h, container)
            self._video_hwnd = h
            resize_child(h, self._vid_frame.winfo_width(), self._vid_frame.winfo_height())
        except Exception:
            self._video_hwnd = None

    def _on_vid_resize(self,e):
        if self._video_hwnd: resize_child(self._video_hwnd,e.width,e.height)

    # ═══════════════════════════════════════════════
    # WATCHDOG
    # ═══════════════════════════════════════════════
    def _start_watchdog(self):
        threading.Thread(target=self._watchdog,daemon=True).start()

    def _watchdog(self):
        if not self.proc: return
        while True:
            time.sleep(1)
            if self.proc.poll() is not None:
                self.root.after(0,self._on_vlc_gone); return

    def _on_vlc_gone(self):
        if self._quitting: return
        self._quitting=True
        try: self.root.quit()
        except Exception: pass

    def _quit(self):
        if self._quitting: return
        self._quitting=True
        self._rand_auto=False; self._r_loop=False   # stop random loops
        # Close VLC FIRST, then tear down the UI.
        try: self.api.stop()
        except Exception: pass
        if self.proc:
            try: self.proc.terminate()
            except Exception: pass
            # give it a moment, then force-kill if still alive
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try: self.proc.kill()
                except Exception: pass
        # sweep any lingering VLC we launched (e.g. --one-instance handoff)
        try: kill_existing_vlc()
        except Exception: pass
        try: self.root.quit()
        except Exception: pass

    # ═══════════════════════════════════════════════
    # POPUP MENUS
    # ═══════════════════════════════════════════════
    def _popup(self,title,items,tag):
        if self.popup: self._close_popup()
        # Set the tag/zone BEFORE constructing the PopupMenu: PopupMenu.__init__
        # calls update_idletasks(), which can dispatch a queued controller
        # button callback mid-construction.  With the tag already set, that
        # re-entrant call sees the popup as "opening" and toggles instead of
        # spawning a second instance (this was the X-filter double-open bug).
        self._ptag=tag; self.zone=self.Z_POPUP
        self.popup=True   # sentinel so toggle checks pass during construction
        self.popup=PopupMenu(self.root,title,items,
                             on_select=self._popup_select,
                             on_close=self._close_popup)

    def _popup_select(self,label,cb):
        self._close_popup()
        if cb: cb()

    def _close_popup(self):
        if self.popup is not None:
            try:
                if hasattr(self.popup,"destroy"): self.popup.destroy()
            except Exception: pass
            self.popup=None
        self._ptag=""; self.zone=self.Z_CONT

    def _open_start(self):
        if self.popup and self._ptag=="start": self._close_popup(); return
        vis="Hide Top Menu" if self._top_vis else "Show Top Menu"
        lib_count=(f"{len(LIB.videos)}v · {len(LIB.audio)}t" if LIB.ready
                   else "Scanning…")
        items=[
            (vis,self._toggle_topnav),
            ("🔍 Search Library",self._open_search),
            ("---",None),
            ("PLAYBACK",None),
            ("Play / Pause",self.api.play_pause),("Stop",self.api.stop),
            ("Previous",self.api.prev),("Next",self.api.next),
            ("---",None),("VIEW",None),
            ("Show / Hide Queue",self._toggle_queue),
            ("Toggle Fullscreen",self._toggle_fullscreen),
            ("Minimize PlayerOne",self._minimize_self),
            ("---",None),("RANDOM",None),
            (("✓ " if self._rand_manual else "")+"Random Mode: Manual (seek = random)",
                self._toggle_manual_random),
            (("✓ " if self._rand_auto else "")+"Random Mode: Auto (3–8 s)",
                self._toggle_auto_random),
            ("---",None),
            (f"LIBRARY  ({lib_count})",None),
            ("Open File…",self._open_file_dlg),
            ("Open URL / Stream…",self._open_url_dlg),
            ("Open Disc",lambda:self.api.enqueue("dvd:///",play_first=True)),
            ("Library Settings…",self._open_library_settings),
            ("---",None),("QUIT",None),
            ("Quit at End of Playlist",lambda:self.api.cmd("pl_quit")),
            ("Quit PlayerOne",self._quit),
        ]
        self._popup("☰  MENU",items,"start")

    def _open_context(self):
        if self.popup and self._ptag=="context": self._close_popup(); return
        items=[
            ("PLAYBACK",None),
            ("Play / Pause",self.api.play_pause),("Stop",self.api.stop),
            ("---",None),("QUEUE",None),
            ("Add to Queue",self._add_selected_to_queue),
            ("Clear Queue",self.api.pl_empty),
            ("---",None),("OPTIONS",None),
            ("Loop",self.api.toggle_loop),
            ("Repeat",self.api.toggle_repeat),
            ("Shuffle",self.api.toggle_random),
            ("---",None),
            ("Open File…",self._open_file_dlg),
            ("Open URL / Stream…",self._open_url_dlg),
        ]
        self._popup("⋯  OPTIONS",items,"context")

    def _open_filter(self):
        if self.popup and self._ptag=="filter": self._close_popup(); return
        items=[
            ("SORT",None),
            ("By Name",lambda:self._sort("name")),
            ("By Date Added",lambda:self._sort("date")),
            ("By Artist",lambda:self._sort("artist")),
            ("By Album",lambda:self._sort("album")),
            ("---",None),("FILTER",None),
            ("All",self._refresh_content),
            ("Videos Only",self._filter_videos),
            ("Audio Only",self._filter_audio),
            ("---",None),("SEARCH",None),
            ("Search Library…",self._open_search),
        ]
        self._popup("⊞  FILTER / SORT",items,"filter")

    def _filter_videos(self):
        with LIB._lock if hasattr(LIB,"_lock") else __import__("contextlib").nullcontext():
            paths=list(LIB.videos)
        self._content.load(self._items_flat(paths,"video"))

    def _filter_audio(self):
        with LIB._lock if hasattr(LIB,"_lock") else __import__("contextlib").nullcontext():
            paths=list(LIB.audio)
        self._content.load(self._items_flat(paths,"audio"))

    def _open_settings(self):
        if self.popup and self._ptag=="settings": self._close_popup(); return
        items=[
            ("AUDIO",None),
            ("Volume Up",self.api.vol_up),("Volume Down",self.api.vol_down),
            ("---",None),("VIDEO",None),
            ("Toggle Fullscreen",self._toggle_fullscreen),
            ("---",None),("SUBTITLES",None),
            ("Cycle Subtitle Track",lambda:self.api.cmd("subtitle-track")),
            ("---",None),("LIBRARY",None),
            ("Library Settings…",self._open_library_settings),
            ("Search Library…",self._open_search),
            ("Rescan Now",LIB.scan if hasattr(LIB,"scan") else lambda:None),
        ]
        self._popup("⚙  SETTINGS",items,"settings")

    # ── helpers
    def _toggle_topnav(self):
        self._top_vis=not self._top_vis
        if self._top_vis:
            self._top_frame.pack(fill=tk.X,side=tk.TOP,before=self._sec_frame)
        else:
            self._top_frame.pack_forget()

    def _toggle_queue(self):
        self._queue_open=not self._queue_open
        if self._queue_open:
            self._queue_panel.pack(side=tk.RIGHT,fill=tk.Y)
            self.zone=self.Z_QUEUE
        else:
            self._queue_panel.pack_forget()
            self.zone=self.Z_CONT

    # ── RANDOM MODES ─────────────────────────────────────
    def _flash(self, msg):
        try: self._lib_lbl.config(text=f"  {msg}")
        except Exception: pass
        print(f"[PlayerOne] {msg}")

    def _random_video_pool(self):
        """The 'whole playlist of videos' the random modes jump around in."""
        return list(LIB.videos)

    def _random_jump(self):
        """Jump to a RANDOM video at a RANDOM play time."""
        pool = self._random_video_pool()
        if not pool:
            self._flash("Random: no videos in library")
            return
        import random
        p = random.choice(pool)
        self._play_path(str(p))
        self.root.after(1200, self._seek_random_time)

    def _seek_random_time(self):
        """Seek the current media to a random position."""
        import random
        st = self._status or {}
        length = int(st.get("length", 0) or 0)
        if length > 6:
            t = random.randint(0, length - 3)
        else:
            t = random.randint(0, 300)   # unknown length — VLC clamps past-end
        self.api.cmd("seek", val=str(t))

    def _toggle_manual_random(self):
        self._rand_manual = not self._rand_manual
        self._flash("Manual random mode: " + ("ON — seek jumps randomly"
                                              if self._rand_manual else "OFF"))

    def _seek_or_random(self, d):
        """Seek by d seconds, OR (in manual random mode) jump randomly."""
        if self._rand_manual:
            self._random_jump()
        else:
            self.api.seek(d)

    def _toggle_auto_random(self):
        self._rand_auto = not self._rand_auto
        if self._rand_auto:
            self._flash("Auto random mode: ON (3–8 s). Press R for random-time loop.")
            self._schedule_auto_random()
        else:
            self._flash("Auto random mode: OFF")
            for job in ("_rand_job", "_r_job"):
                jid = getattr(self, job)
                if jid:
                    try: self.root.after_cancel(jid)
                    except Exception: pass
                    setattr(self, job, None)
            self._r_loop = False

    def _schedule_auto_random(self):
        if not self._rand_auto:
            return
        import random
        self._random_jump()
        self._rand_job = self.root.after(random.randint(3000, 8000),
                                         self._schedule_auto_random)

    def _on_r_key(self):
        """R toggles a random-time loop within the current video (until R again)."""
        self._r_loop = not self._r_loop
        if self._r_loop:
            self._flash("R: random-time loop ON")
            self._schedule_r_loop()
        else:
            self._flash("R: random-time loop OFF")
            if self._r_job:
                try: self.root.after_cancel(self._r_job)
                except Exception: pass
                self._r_job = None

    def _schedule_r_loop(self):
        if not self._r_loop:
            return
        import random
        self._seek_random_time()
        self._r_job = self.root.after(random.randint(3000, 8000),
                                      self._schedule_r_loop)

    def _toggle_fullscreen(self):
        """
        App-level fullscreen: hide the top nav bar, section bar, playback bar
        and status footer, and put the window into borderless fullscreen that
        covers the taskbar.

        Note: this is Tk borderless-fullscreen (the whole screen, no window
        chrome, over the taskbar) — the closest a Tk/Python app can get.  True
        GPU 'exclusive' display-mode fullscreen isn't available to Tkinter; the
        embedded VLC surface fills the screen either way.
        """
        self._fullscreen = not self._fullscreen
        bars = [getattr(self,"_top_frame",None), getattr(self,"_sec_frame",None),
                getattr(self,"_playback_bar",None), getattr(self,"_foot",None)]
        if self._fullscreen:
            for b in bars:
                if b is not None:
                    try: b.pack_forget()
                    except Exception: pass
            try:
                self.root.attributes("-fullscreen", True)
                self.root.attributes("-topmost", True)
            except Exception: pass
        else:
            # restore chrome in original stacking order
            try: self.root.attributes("-fullscreen", False)
            except Exception: pass
            try: self.root.attributes("-topmost", False)
            except Exception: pass
            if getattr(self,"_top_frame",None) is not None:
                self._top_frame.pack(fill=tk.X,side=tk.TOP,before=self._body)
            if getattr(self,"_sec_frame",None) is not None:
                self._sec_frame.pack(fill=tk.X,side=tk.TOP,before=self._body)
            if getattr(self,"_playback_bar",None) is not None:
                self._playback_bar.pack(fill=tk.X,side=tk.BOTTOM)
            if getattr(self,"_foot",None) is not None:
                self._foot.pack(fill=tk.X,side=tk.BOTTOM)
        # keep the embedded video sized to the (now larger/smaller) frame
        self.root.after(60, self._resize_embedded_video)

    def _resize_embedded_video(self):
        if getattr(self,"_video_hwnd",None):
            try:
                resize_child(self._video_hwnd, self._vid_frame.winfo_width(),
                                               self._vid_frame.winfo_height())
            except Exception: pass

    def _minimize_self(self):
        """Minimize the PlayerOne window to the taskbar."""
        try:
            # if a borderless/topmost popup stole focus, make sure it's gone
            if self.popup:
                self._close_popup()
            self.root.iconify()
        except Exception as e:
            print(f"[PlayerOne] Minimize failed: {e}")

    def _add_selected_to_queue(self):
        p=self._content.selected_path()
        if p: self.api.enqueue(str(p),play_first=False)

    def _queue_activate(self,e=None):
        cur=self._q_list.curselection()
        if cur and cur[0]<len(self._queue_items):
            self.api.pl_play_id(self._queue_items[cur[0]]["id"])

    def _open_file_dlg(self):
        exts=" ".join("*"+e for e in sorted(MEDIA_EXTS))
        files=fd.askopenfilenames(title="Open Media",
                                  filetypes=[("Media files",exts),("All files","*.*")])
        if files: self._load_files(list(files),play=True)

    def _open_url_dlg(self):
        var=tk.StringVar()
        self.vkbd=VirtualKeyboard(self.root,var,
            on_done=lambda v:(self.api.enqueue(v,play_first=True),
                              setattr(self,"zone",self.Z_CONT)))
        self.zone=self.Z_VKBD

    def _add_lib_folder(self):
        folder=fd.askdirectory(title="Add folder to library")
        if folder:
            paths=CFG.setdefault("library_paths",[])
            if folder not in paths: paths.append(folder)
            _save_config()
            LIB.add_folder(folder)

    def _sort(self,key):
        """Sort current view or all-media by key."""
        if hasattr(LIB,"sort_all"):
            paths=LIB.sort_all(key)
        else:
            paths=self._content.all_paths()
            if key=="name":   paths.sort(key=lambda p:Path(p).name.lower())
            elif key=="date": paths.sort(key=lambda p:Path(p).stat().st_mtime,reverse=True)
        mixed=self._items_flat(paths,"mixed")
        self._content.load(mixed)

    # ── search ──────────────────────────────────────────────────
    def _open_search(self):
        """Show search bar and focus the entry."""
        if not self._search_active:
            self._search_active=True
            # insert search frame above sections bar
            self._search_frame.pack(fill=tk.X,side=tk.TOP,
                                    after=self._top_frame,before=self._sec_frame)
        self._search_entry.focus_set()
        self._search_entry.select_range(0,tk.END)

    def _close_search(self):
        """Hide search bar and return to normal content view."""
        self._search_active=False
        self._search_frame.pack_forget()
        self._search_var.set("")
        self._refresh_content()
        self.zone=self.Z_CONT

    def _search_debounce(self):
        """Debounce: run search 300 ms after last keystroke."""
        if self._search_debounce_id:
            self.root.after_cancel(self._search_debounce_id)
        self._search_debounce_id=self.root.after(300,self._do_search)

    def _do_search(self):
        q=self._search_var.get().strip()
        if not q:
            self._refresh_content(); return
        results=LIB.search(q) if hasattr(LIB,"search") else []
        if not results:
            self._content.load([self._mk(None,f'No results for "{q}"',
                                         "Try a different term")])
            return
        items=[]
        for p in results:
            p=Path(p); sfx=p.suffix.lower()
            if sfx in AUDIO_EXTS:
                m=LIB.meta(p); title=m["title"] or p.stem
                sub=" · ".join(filter(None,[m["artist"],m["album"]]))
                items.append(self._mk(p,title,sub))
            else:
                items.append(self._mk(p,p.stem,str(p.parent)))
        self._content.load(items)
        self._lib_lbl.config(text=f"  {len(items)} results for \"{q}\"")

    # ── library settings popup ──────────────────────────────────
    def _open_library_settings(self):
        """Show the full library settings panel as a popup."""
        if self.popup: self._close_popup()
        top=tk.Toplevel(self.root)
        top.title("Library Settings")
        top.configure(bg=T["bg"])
        top.geometry("500x440")
        top.attributes("-topmost",True)
        _set_icon(top)
        self.root.update_idletasks()
        px,py=self.root.winfo_rootx(),self.root.winfo_rooty()
        pw,ph=self.root.winfo_width(),self.root.winfo_height()
        top.geometry(f"500x440+{px+pw//2-250}+{py+ph//2-220}")

        if _HAS_ML:
            _ml.make_library_settings_panel(
                top, LIB,
                on_change=self._refresh_content,
                theme=T)
        else:
            tk.Label(top,text="Library Settings",bg=T["bg"],fg=T["highlight"],
                     font=("Segoe UI",13,"bold")).pack(pady=12)
            tk.Button(top,text="Add Folder",command=self._add_lib_folder,
                      bg=T["border"],fg=T["text"],relief="flat",
                      font=("Segoe UI",10),padx=12,pady=6).pack(pady=6)

        tk.Button(top,text="Close",command=top.destroy,
                  bg=T["highlight"],fg=T["bg"],relief="flat",
                  font=("Segoe UI",10,"bold"),padx=12,pady=6).pack(pady=8)

    # ═══════════════════════════════════════════════
    # CONTROLLER
    # ═══════════════════════════════════════════════
    def _start_input_loop(self):
        threading.Thread(target=self._loop,daemon=True).start()

    def _loop(self):
        DELAY=0.38; RATE=0.11; STICK=0.14
        _held={}; _stick=0.0
        while True:
            time.sleep(0.016)
            st=self._ctrl.state()
            if st is None: self._last_btns=0; continue
            btns,lx,ly,rx,ry=st; now=time.time()
            def pressed(m): return bool(btns&m) and not bool(self._last_btns&m)
            def released(m): return not bool(btns&m) and bool(self._last_btns&m)
            def held(m): return bool(btns&m)

            # sticks
            if now-_stick>STICK:
                fired=False
                if abs(rx)>DEADZONE:
                    d=CFG["seek_step"]*(1 if rx>0 else -1)
                    self.root.after(0,self._seek_or_random,d); fired=True
                if abs(ry)>DEADZONE:
                    self.root.after(0,self.api.vol_up if ry>0 else self.api.vol_down); fired=True
                dx=1 if lx>DEADZONE else(-1 if lx<-DEADZONE else 0)
                dy=1 if ly<-DEADZONE else(-1 if ly>DEADZONE else 0)
                if dx or dy:
                    self.root.after(0,self._nav,dx,dy); fired=True
                if fired: _stick=now

            # dpad with repeat
            for mask,(dx2,dy2) in[(DPAD_UP,(0,-1)),(DPAD_DOWN,(0,1)),
                                   (DPAD_LEFT,(-1,0)),(DPAD_RIGHT,(1,0))]:
                if pressed(mask):
                    _held[mask]=now+DELAY; self.root.after(0,self._nav,dx2,dy2)
                elif held(mask) and _held.get(mask,now+1)<now:
                    _held[mask]=now+RATE; self.root.after(0,self._nav,dx2,dy2)
                elif released(mask): _held.pop(mask,None)

            # face buttons
            if pressed(BTN_A):
                t2=time.time()
                if t2-self._last_a>=CFG["cooldown_a"]:
                    self._last_a=t2; self.root.after(0,self._btn_a)
            if pressed(BTN_B):   self.root.after(0,self._btn_b)
            if pressed(BTN_X):   self.root.after(0,self._open_filter)
            if self.zone==self.Z_VKBD and self.vkbd:
                # Hold Y to record voice input, release to recognize & insert
                if pressed(BTN_Y):  self.root.after(0,self.vkbd.voice_start)
                if released(BTN_Y): self.root.after(0,self.vkbd.voice_stop)
            elif pressed(BTN_Y): self.root.after(0,self._toggle_queue)
            if pressed(BTN_START): self.root.after(0,self._open_start)
            if pressed(BTN_BACK):  self.root.after(0,self._open_context)
            if pressed(LB):    self.root.after(0,self.api.prev)
            if pressed(RB):    self.root.after(0,self.api.next)
            if pressed(L3):    self.root.after(0,self._open_settings)
            if pressed(R3):    self.root.after(0,self._toggle_fullscreen)
            self._last_btns=btns

    def _nav(self,dx,dy):
        z=self.zone
        if z==self.Z_POPUP and self.popup:
            if dy==1: self.popup.navigate(+1)
            elif dy==-1: self.popup.navigate(-1)
        elif z==self.Z_VKBD and self.vkbd:
            self.vkbd.navigate(dx,dy)
        elif z==self.Z_TOP:
            if dx:
                self.top_idx=max(0,min(len(self.TABS)-1,self.top_idx+dx))
                self._update_tab_hl()
            elif dy==1:
                self.zone=self.Z_SEC
                self._update_tab_hl(); self._update_sec_hl()
        elif z==self.Z_SEC:
            if dx:
                secs=self.SECTIONS.get(self._cur_tab,[])
                self.sec_idx=max(0,min(len(secs)-1,self.sec_idx+dx))
                self._update_sec_hl()
            elif dy==-1:
                if self._top_vis:
                    self.zone=self.Z_TOP
                    self._update_tab_hl(); self._update_sec_hl()
            elif dy==1:
                self.zone=self.Z_CONT
                self._update_sec_hl()
        elif z==self.Z_CONT:
            if dy==-1 and self._content.at_top():
                self.zone=self.Z_SEC; self._update_sec_hl()
            else:
                self._content.navigate(dy)
        elif z==self.Z_QUEUE:
            cur=self._q_list.curselection()
            if dy==-1:
                idx=max(0,(cur[0]-1) if cur else 0)
            else:
                idx=min(self._q_list.size()-1,(cur[0]+1) if cur else 0)
            self._q_list.selection_clear(0,tk.END)
            self._q_list.selection_set(idx); self._q_list.see(idx)

    def _on_tab_click(self,i):
        self._select_tab(i)
        self.zone=self.Z_SEC
        self._update_tab_hl(); self._update_sec_hl()

    def _on_sec_click(self,i):
        self._select_section(i)
        self.zone=self.Z_CONT
        self._update_sec_hl()

    def _btn_a(self):
        z=self.zone
        if z==self.Z_POPUP and self.popup: self.popup.activate()
        elif z==self.Z_VKBD and self.vkbd: self.vkbd.activate()
        elif z==self.Z_TOP:
            self._select_tab(self.top_idx)
            self.zone=self.Z_SEC
            self._update_tab_hl(); self._update_sec_hl()
        elif z==self.Z_SEC:
            self._select_section(self.sec_idx)
            self.zone=self.Z_CONT
        elif z==self.Z_CONT:
            p=self._content.selected_path()
            if p:
                self._play_path(p)
            else:
                self.api.play_pause()
        elif z==self.Z_QUEUE:
            self._queue_activate()

    def _btn_b(self):
        z=self.zone
        if z==self.Z_POPUP: self._close_popup()
        elif z==self.Z_VKBD and self.vkbd:
            self.vkbd.destroy(); self.vkbd=None; self.zone=self.Z_CONT
        elif z==self.Z_CONT:
            self.zone=self.Z_SEC; self._update_sec_hl()
        elif z==self.Z_SEC:
            if self._top_vis:
                self.zone=self.Z_TOP
                self._update_tab_hl(); self._update_sec_hl()
        elif z==self.Z_QUEUE:
            self._toggle_queue()
        else:
            self.api.stop()

_SINGLE_INSTANCE_LOCK = None   # keep the handle/socket alive for the process life

def acquire_single_instance():
    """
    Ensure only one PlayerOne runs at a time.  Returns True if we got the lock,
    False if another instance already holds it.

    Windows: a named mutex.  Everywhere: also bind a loopback socket to a fixed
    port as a cross-platform backstop (and so a crashed process's lock is
    released automatically by the OS).
    """
    global _SINGLE_INSTANCE_LOCK
    got = True
    # ── loopback-socket lock (cross-platform, auto-released on exit/crash) ──
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(("127.0.0.1", 49732))   # fixed, app-specific lock port
        s.listen(1)
        _SINGLE_INSTANCE_LOCK = s
    except OSError:
        got = False
    # ── named mutex (Windows, belt-and-braces) ──
    if os.name == "nt":
        try:
            mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "PlayerOne_SingleInstance_Mutex")
            ERROR_ALREADY_EXISTS = 183
            if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                got = False
            else:
                # stash so it isn't GC'd/closed
                if _SINGLE_INSTANCE_LOCK is None:
                    _SINGLE_INSTANCE_LOCK = mutex
                else:
                    _SINGLE_INSTANCE_LOCK = (_SINGLE_INSTANCE_LOCK, mutex)
        except Exception:
            pass
    return got


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────
def main():
    # ── enforce single instance ────────────────────────────────────────
    if not acquire_single_instance():
        print("[PlayerOne] Another instance is already running — exiting.")
        try:
            import tkinter as _tk, tkinter.messagebox as _mb
            _r = _tk.Tk(); _r.withdraw()
            _mb.showinfo("PlayerOne", "PlayerOne is already running.")
            _r.destroy()
        except Exception:
            pass
        return
    # ── parse CLI args (file paths for default-player mode)
    initial_files=[]
    for arg in sys.argv[1:]:
        p=Path(arg)
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            initial_files.append(p)
        elif p.is_dir():
            initial_files+=[f for f in sorted(p.rglob("*"))
                            if f.is_file() and f.suffix.lower() in MEDIA_EXTS]

    # ── launch VLC headlessly
    vlc_path = find_vlc()
    proc = None
    _stderr_buf = [""]   # mutable so the thread can write into it

    if vlc_path:
        # Close any VLC already running so we cleanly own the process/port.
        kill_existing_vlc()
        time.sleep(0.3)
        print(f"[PlayerOne] Launching VLC: {vlc_path}")
        proc = launch_vlc(vlc_path)

        # Move VLC's window to the tray (best-effort, background + retries)
        minimize_vlc_to_tray(proc.pid)

        # Drain stderr in background so we can show it on failure
        def _drain():
            _stderr_buf[0] = _vlc_stderr_drain(proc)
        threading.Thread(target=_drain, daemon=True).start()
    else:
        print("[PlayerOne] VLC not found. Run install.py first.")

    api = VLCApi()
    if vlc_path:
        api.set_fallback_exe(vlc_path)

    # ── Tk (must come before any dialog)
    root = tk.Tk()
    root.title("PlayerOne")
    _set_icon(root)

    # dark win chrome (Win10/11)
    try:
        root.update()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
    except Exception:
        pass

    # ── wait for VLC HTTP and handle failure
    vlc_ok = False
    if vlc_path and proc:
        vlc_ok = wait_for_vlc(timeout=14)
        if not vlc_ok:
            time.sleep(0.5)
            exit_code = proc.poll()
            stderr_text = _stderr_buf[0]
            if exit_code is not None:
                diag = (f"VLC exited immediately with code {exit_code}.\n\n{stderr_text}")
            else:
                diag = (f"VLC HTTP interface not responding on port {CFG['http_port']} after 14 s.\n\n{stderr_text}")
            root.after(200, lambda: show_vlc_error(root, vlc_path, diag))
        else:
            # Detect interface + VLC version in background
            def _do_detect():
                iface = api.detect_version()
                if iface is None:
                    label = "no HTTP interface (direct-launch mode)"
                elif iface == api.REST:
                    label = "experimental REST interface"
                else:
                    label = f"Lua HTTP interface, VLC {api.version_str or '?'}"
                root.after(0, lambda: print(f"[PlayerOne] Using {label}"))
            threading.Thread(target=_do_detect, daemon=True).start()
    elif not vlc_path:
        root.after(200, lambda: show_vlc_error(root, None,
            "VLC was not found. Install from https://www.videolan.org/ and restart."))

    # ── first-boot wizard
    if CFG.get("first_boot", True):
        exe = None
        cand = Path(sys.executable)
        if cand.suffix.lower() == ".exe" and "python" not in cand.name.lower():
            exe = str(cand)
        run_first_boot(root, exe_path=exe)

    # ── build app
    app = App(root, proc, api, initial_files=initial_files)
    app._start_input_loop()

    root.protocol("WM_DELETE_WINDOW", app._quit)
    root.mainloop()

if __name__=="__main__":
    main()
