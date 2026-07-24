"""
media_library.py  ―  PlayerOne Media Library
=============================================
Drop-in replacement / extension for the inline MediaLibrary class.

Features
--------
• Recursive folder scan  (threaded, non-blocking)
• Persistent JSON metadata cache  (~/.playerone/meta_cache.json)
• Zero-dependency ID3v2 + MP4/M4A + OGG/FLAC tag reading
• Album-art (APIC frame) extraction → PhotoImage compatible PNG bytes
• File-system watcher  (polling-based, no pyinotify / watchdog needed)
• Incremental add/remove as watcher detects changes
• Observer callbacks for UI updates
• All-media combined view, search, sort-by
"""

from __future__ import annotations
import json
import os
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# ─── extension sets (mirrors vlc_controller.py) ──────────────────────────────
VIDEO_EXTS = {
    ".mp4",".mkv",".avi",".mov",".wmv",".flv",".webm",".m4v",
    ".ts",".mts",".m2ts",".vob",".ogv",".3gp",".mpg",".mpeg",
    ".divx",".rmvb",".asf",".f4v",".h264",".hevc",".m2v",
}
AUDIO_EXTS = {
    ".mp3",".flac",".ogg",".wav",".aac",".m4a",".wma",".opus",
    ".ape",".mka",".aiff",".alac",".ac3",".dts",".tta",".wv",
}
PLAYLIST_EXTS = {".m3u",".m3u8",".xspf",".pls",".asx",".wpl",".cue"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | PLAYLIST_EXTS

# ─── cache location ───────────────────────────────────────────────────────────
_CACHE_DIR  = Path.home() / ".playerone"
_CACHE_FILE = _CACHE_DIR / "meta_cache.json"
_ART_DIR    = _CACHE_DIR / "art"

# ─── default scan roots ───────────────────────────────────────────────────────
DEFAULT_ROOTS: List[Path] = [
    Path.home() / "Videos",
    Path.home() / "Music",
    Path("C:/Users/Public/Videos"),
    Path("C:/Users/Public/Music"),
]

# ─────────────────────────────────────────────────────────────────────────────
#  TAG READING   (zero external dependencies)
# ─────────────────────────────────────────────────────────────────────────────

def _decode_str(enc: int, raw: bytes) -> str:
    """Decode ID3 string field according to encoding byte."""
    try:
        if enc in (1, 2):
            return raw.replace(b"\xff\xfe","").replace(b"\xfe\xff","") \
                      .decode("utf-16", "replace").strip("\x00 ")
        if enc == 3:
            return raw.decode("utf-8", "replace").strip("\x00 ")
        return raw.decode("latin-1", "replace").strip("\x00 ")
    except Exception:
        return ""


def read_id3v2(path: Path) -> dict:
    """
    Parse ID3v2.3/2.4 tags from an MP3/audio file.
    Returns dict: title, artist, album, genre, track, year, duration_secs, art_png_bytes
    """
    r: dict = {"title":"","artist":"","album":"","genre":"","track":"",
               "year":"","duration_secs":0,"art_png_bytes":None}
    try:
        data = path.read_bytes()
        if data[:3] != b"ID3":
            return r

        # syncsafe integer
        def _ss(b4): return ((b4[0]&0x7f)<<21)|((b4[1]&0x7f)<<14)|((b4[2]&0x7f)<<7)|(b4[3]&0x7f)

        ver   = data[3]
        flags = data[5]
        tag_sz = _ss(data[6:10])
        pos = 10

        # extended header
        if flags & 0x40:
            ext_sz = _ss(data[10:14]) if ver >= 4 else struct.unpack(">I", data[10:14])[0]
            pos += ext_sz

        while pos + 10 < min(tag_sz + 10, len(data)):
            fid = data[pos:pos+4]
            if fid == b"\x00\x00\x00\x00":
                break
            if ver <= 3:
                fsz = struct.unpack(">I", data[pos+4:pos+8])[0]
            else:
                fsz = _ss(data[pos+4:pos+8])
            if fsz <= 0 or pos + 10 + fsz > len(data):
                break
            body = data[pos+10:pos+10+fsz]
            enc  = body[0] if body else 0
            raw  = body[1:]

            if   fid == b"TIT2": r["title"]  = _decode_str(enc, raw)
            elif fid == b"TPE1": r["artist"] = _decode_str(enc, raw)
            elif fid == b"TALB": r["album"]  = _decode_str(enc, raw)
            elif fid in (b"TCON",):
                g = _decode_str(enc, raw).strip("()")
                # map numeric genre codes e.g. "(0)" → "Blues"
                if g.isdigit():
                    g = _ID3_GENRES.get(int(g), g)
                r["genre"] = g
            elif fid == b"TRCK": r["track"]  = _decode_str(enc, raw).split("/")[0].strip()
            elif fid == b"TDRC": r["year"]   = _decode_str(enc, raw)[:4]
            elif fid == b"TYER": r["year"]   = _decode_str(enc, raw)[:4]
            elif fid == b"APIC":
                # APIC: enc(1) + mime(null-term) + pictype(1) + desc(null-term) + data
                try:
                    null = raw.index(b"\x00", 1)
                    mime = raw[1:null].decode("latin-1","replace").lower()
                    after_mime = raw[null+1:]
                    # skip pictype + description
                    null2 = after_mime.index(b"\x00", 1)
                    img_data = after_mime[null2+1:]
                    if mime in ("image/jpeg","image/jpg","image/png") and img_data:
                        r["art_png_bytes"] = img_data
                except Exception:
                    pass
            pos += 10 + fsz
    except Exception:
        pass
    return r


def read_mp4_tags(path: Path) -> dict:
    """Very minimal MP4/M4A iTunes tag reader."""
    r: dict = {"title":"","artist":"","album":"","genre":"","track":"",
               "year":"","duration_secs":0,"art_png_bytes":None}
    try:
        data = path.read_bytes()
        def _find(tag: bytes, blob: bytes, start: int=0) -> Tuple[int,int]:
            """Return (offset_of_data, data_length) for first iTunes tag atom."""
            pos = start
            while pos + 8 <= len(blob):
                sz = struct.unpack(">I", blob[pos:pos+4])[0]
                name = blob[pos+4:pos+8]
                if sz < 8: break
                if name == tag:
                    return pos+8, sz-8
                pos += sz
            return -1, 0

        # Walk to moov/udta/meta/ilst
        def walk(needle: bytes, blob: bytes) -> Optional[bytes]:
            pos = 0
            while pos + 8 <= len(blob):
                sz = struct.unpack(">I", blob[pos:pos+4])[0]
                name = blob[pos+4:pos+8]
                if sz < 8: break
                if name == needle:
                    return blob[pos+8:pos+sz]
                pos += sz
            return None

        moov = walk(b"moov", data)
        if not moov: return r
        udta = walk(b"udta", moov)
        if not udta: return r
        meta = walk(b"meta", udta)
        if not meta: return r
        # meta has a 4-byte version/flags prefix before ilst
        ilst = walk(b"ilst", meta[4:])
        if not ilst: return r

        MAP = {
            b"\xa9nam": "title",
            b"\xa9ART": "artist",
            b"\xa9alb": "album",
            b"\xa9gen": "genre",
            b"\xa9day": "year",
        }
        pos = 0
        while pos + 8 <= len(ilst):
            sz = struct.unpack(">I", ilst[pos:pos+4])[0]
            name = ilst[pos+4:pos+8]
            if sz < 8: break
            atom = ilst[pos+8:pos+sz]
            if name in MAP:
                # data child: 8-byte header + 4-byte version/flags + 4-byte locale + text
                if atom[:4] == b"data" or len(atom) > 16:
                    inner_pos = 0
                    while inner_pos + 8 <= len(atom):
                        isz = struct.unpack(">I", atom[inner_pos:inner_pos+4])[0]
                        iname = atom[inner_pos+4:inner_pos+8]
                        if isz < 8: break
                        if iname == b"data":
                            text_raw = atom[inner_pos+16:inner_pos+isz]
                            try:
                                r[MAP[name]] = text_raw.decode("utf-8","replace").strip()
                            except Exception:
                                pass
                        inner_pos += isz
            elif name == b"covr":
                inner_pos = 0
                while inner_pos + 8 <= len(atom):
                    isz = struct.unpack(">I", atom[inner_pos:inner_pos+4])[0]
                    iname = atom[inner_pos+4:inner_pos+8]
                    if isz < 8: break
                    if iname == b"data" and isz > 16:
                        r["art_png_bytes"] = atom[inner_pos+16:inner_pos+isz]
                    inner_pos += isz
            pos += sz
    except Exception:
        pass
    return r


def read_vorbis_tags(path: Path) -> dict:
    """OGG/FLAC Vorbis comment reader."""
    r: dict = {"title":"","artist":"","album":"","genre":"","track":"",
               "year":"","duration_secs":0,"art_png_bytes":None}
    try:
        data = path.read_bytes()
        sfx = path.suffix.lower()

        if sfx == ".ogg":
            # OGG page: capture_pattern OggS + page_header; comment packet is page 2
            pos = 0
            page_count = 0
            comment_data = b""
            while pos + 27 <= len(data):
                if data[pos:pos+4] != b"OggS":
                    pos += 1
                    continue
                segs = data[pos+26]
                seg_table = data[pos+27:pos+27+segs]
                pkt_size = sum(seg_table)
                pkt_start = pos + 27 + segs
                if page_count == 1:
                    comment_data = data[pkt_start:pkt_start+pkt_size]
                    break
                page_count += 1
                pos = pkt_start + pkt_size
            if comment_data and comment_data[1:7] == b"vorbis":
                comment_data = comment_data[7:]
            raw = comment_data

        elif sfx == ".flac":
            # FLAC stream marker
            if data[:4] != b"fLaC": return r
            pos = 4
            raw = b""
            while pos + 4 <= len(data):
                btype = data[pos] & 0x7f
                last  = bool(data[pos] & 0x80)
                blen  = struct.unpack(">I", b"\x00"+data[pos+1:pos+4])[0]
                pos += 4
                if btype == 4:   # VORBIS_COMMENT
                    raw = data[pos:pos+blen]
                    break
                if btype == 6:   # PICTURE
                    pass         # TODO: album art from FLAC
                pos += blen
                if last: break
        else:
            return r

        if not raw: return r

        # parse vorbis comment vector
        idx = 0
        vendor_len = struct.unpack("<I", raw[idx:idx+4])[0]; idx += 4 + vendor_len
        if idx + 4 > len(raw): return r
        count = struct.unpack("<I", raw[idx:idx+4])[0]; idx += 4
        MAP = {"title":"title","artist":"artist","album":"album",
               "genre":"genre","tracknumber":"track","date":"year"}
        for _ in range(count):
            if idx + 4 > len(raw): break
            clen = struct.unpack("<I", raw[idx:idx+4])[0]; idx += 4
            comment = raw[idx:idx+clen].decode("utf-8","replace"); idx += clen
            if "=" in comment:
                k, v = comment.split("=", 1)
                k = k.lower()
                if k in MAP:
                    r[MAP[k]] = v.strip()
    except Exception:
        pass
    return r


def read_tags(path: Path) -> dict:
    """Dispatch to the right tag reader based on file extension."""
    sfx = path.suffix.lower()
    if sfx in (".mp3",".wav",".aiff"):
        return read_id3v2(path)
    if sfx in (".m4a",".aac",".mp4",".m4v"):
        return read_mp4_tags(path)
    if sfx in (".ogg",".flac",".opus",".oga"):
        return read_vorbis_tags(path)
    # fallback: try ID3
    return read_id3v2(path)


# ─────────────────────────────────────────────────────────────────────────────
#  ALBUM ART CACHE
# ─────────────────────────────────────────────────────────────────────────────

def save_art(path: Path, png_bytes: bytes) -> Optional[Path]:
    """Save raw image bytes (JPEG or PNG) to the art cache. Returns path."""
    try:
        _ART_DIR.mkdir(parents=True, exist_ok=True)
        art_path = _ART_DIR / (path.stem[:60] + "_" + str(hash(str(path)) % 99999) + ".img")
        art_path.write_bytes(png_bytes)
        return art_path
    except Exception:
        return None

def get_art_path(path: Path) -> Optional[Path]:
    """Return cached art file path if it exists."""
    art_path = _ART_DIR / (path.stem[:60] + "_" + str(hash(str(path)) % 99999) + ".img")
    return art_path if art_path.is_file() else None


# ─────────────────────────────────────────────────────────────────────────────
#  PERSISTENT META CACHE
# ─────────────────────────────────────────────────────────────────────────────

class MetaCache:
    """
    Stores per-file metadata keyed by absolute path string.
    Each entry: {title, artist, album, genre, track, year, mtime, has_art}
    """
    def __init__(self):
        self._data: Dict[str, dict] = {}
        self._dirty = False
        self._lock  = threading.Lock()
        self._load()

    def _load(self):
        try:
            if _CACHE_FILE.is_file():
                raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                with self._lock:
                    self._data = raw
        except Exception:
            pass

    def save(self):
        if not self._dirty:
            return
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with self._lock:
                snap = dict(self._data)
            _CACHE_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
            self._dirty = False
        except Exception:
            pass

    def get(self, path: Path) -> Optional[dict]:
        key = str(path)
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None
        # stale if file was modified since cache
        try:
            if path.stat().st_mtime != entry.get("mtime", 0):
                return None
        except Exception:
            return None
        return entry

    def put(self, path: Path, meta: dict):
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0
        entry = {**meta, "mtime": mtime}
        entry.pop("art_png_bytes", None)   # don't persist raw art bytes
        with self._lock:
            self._data[key] = entry
        self._dirty = True

    def remove(self, path: Path):
        key = str(path)
        with self._lock:
            self._data.pop(key, None)
        self._dirty = True

    def prune(self, known_paths: set):
        """Remove cache entries for files that no longer exist."""
        with self._lock:
            stale = [k for k in self._data if k not in known_paths]
            for k in stale:
                del self._data[k]
        if stale:
            self._dirty = True


# ─────────────────────────────────────────────────────────────────────────────
#  MEDIA LIBRARY
# ─────────────────────────────────────────────────────────────────────────────

class MediaLibrary:
    """
    Thread-safe media library with persistent cache and folder watching.

    Usage
    -----
    lib = MediaLibrary(config_paths=[...])
    lib.on_update(my_callback)       # called with reason str on any change
    lib.scan()                       # start async background scan
    lib.start_watcher()              # poll for new/removed files
    lib.add_folder(path)             # add a watch root at runtime
    """

    def __init__(self, config_paths: Optional[List[str]] = None):
        self._roots: List[Path] = list(DEFAULT_ROOTS)
        for p in (config_paths or []):
            cp = Path(p)
            if cp not in self._roots:
                self._roots.append(cp)

        self.videos:    List[Path] = []
        self.audio:     List[Path] = []
        self.playlists: List[Path] = []

        self._lock     = threading.Lock()
        self._cache    = MetaCache()
        self._watchers: Dict[Path, float] = {}   # root → last scan time
        self._callbacks: List[Callable[[str], None]] = []
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_watch   = threading.Event()

        self.ready    = False
        self.scanning = False
        self.scan_msg = ""
        self.total_scanned = 0

    # ── observers ─────────────────────────────────────────────────────────────
    def on_update(self, cb: Callable[[str], None]):
        self._callbacks.append(cb)

    def _notify(self, reason: str):
        for cb in self._callbacks:
            try:
                cb(reason)
            except Exception:
                pass

    # ── folder management ─────────────────────────────────────────────────────
    def set_roots(self, paths: List[str]):
        """Replace the scan root list (call before scan())."""
        with self._lock:
            self._roots = list(DEFAULT_ROOTS)
            for p in paths:
                cp = Path(p)
                if cp not in self._roots:
                    self._roots.append(cp)

    def add_folder(self, path: str):
        """Add a new folder and trigger an incremental scan of just that folder."""
        cp = Path(path)
        with self._lock:
            if cp not in self._roots:
                self._roots.append(cp)
        self._notify("folder_added")
        threading.Thread(target=self._scan_root, args=(cp, True), daemon=True).start()

    def remove_folder(self, path: str):
        """Remove a folder and drop all its media from the library."""
        cp = Path(path)
        with self._lock:
            if cp in self._roots:
                self._roots.remove(cp)
            self.videos    = [p for p in self.videos    if not str(p).startswith(str(cp))]
            self.audio     = [p for p in self.audio     if not str(p).startswith(str(cp))]
            self.playlists = [p for p in self.playlists if not str(p).startswith(str(cp))]
        self._notify("folder_removed")

    def get_roots(self) -> List[str]:
        with self._lock:
            return [str(r) for r in self._roots]

    # ── scan ──────────────────────────────────────────────────────────────────
    def scan(self):
        """Start a full async scan of all roots."""
        if self.scanning:
            return
        threading.Thread(target=self._scan_all, daemon=True).start()

    def _scan_all(self):
        self.scanning  = True
        self.scan_msg  = "Starting scan…"
        self._notify("scan_start")

        with self._lock:
            roots = list(self._roots)

        # Skip dirs that obviously don't exist or are system dirs
        SKIP_DIRS = {
            "windows", "program files", "program files (x86)",
            "programdata", "$recycle.bin", "system volume information",
            "appdata", "node_modules", ".git", "__pycache__",
        }

        vids, auds, pls = [], [], []
        total = 0
        last_notify = 0.0

        def _walk(root: Path):
            """Iterative os.scandir walk — faster than rglob, skippable."""
            stack = [root]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() not in SKIP_DIRS:
                                    stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                yield Path(entry.path)
                except PermissionError:
                    continue
                except Exception:
                    continue

        for root in roots:
            # Quick existence check using os.path (no stat on network drives)
            try:
                if not os.path.isdir(str(root)):
                    continue
            except Exception:
                continue

            deadline = time.time() + 30.0   # max 30 s per root
            try:
                for p in _walk(root):
                    if time.time() > deadline:
                        self.scan_msg = f"Scan timeout on {root.name} — moving on"
                        self._notify("scan_progress")
                        break
                    sfx = p.suffix.lower()
                    if sfx in VIDEO_EXTS:      vids.append(p)
                    elif sfx in AUDIO_EXTS:    auds.append(p)
                    elif sfx in PLAYLIST_EXTS: pls.append(p)
                    else:
                        continue
                    total += 1
                    now = time.time()
                    if now - last_notify >= 1.0:   # notify at most once per second
                        last_notify = now
                        self.scan_msg = (f"Scanning {root.name}… "
                                         f"{len(vids)}v · {len(auds)}t")
                        self._notify("scan_progress")
            except Exception:
                continue

        # Sort — use key=str.lower to avoid stat calls
        vids.sort(key=lambda p: p.name.lower())
        auds.sort(key=lambda p: p.name.lower())
        pls.sort(key=lambda p: p.name.lower())

        with self._lock:
            self.videos    = vids
            self.audio     = auds
            self.playlists = pls
            self.total_scanned = total

        self.ready    = True
        self.scanning = False
        self.scan_msg = (f"Library ready — {len(vids)} videos · "
                         f"{len(auds)} tracks · {len(pls)} playlists")
        self._notify("scan_done")

        # Lightweight background metadata pre-fetch (cached only, no reads)
        threading.Thread(target=self._prefetch_meta, args=(list(auds[:500]),),
                         daemon=True).start()

        # Prune stale cache entries
        all_paths = {str(p) for p in vids + auds + pls}
        self._cache.prune(all_paths)
        threading.Thread(target=self._cache.save, daemon=True).start()

    def _scan_root(self, root: Path, incremental: bool = False):
        """Scan a single root and merge results."""
        if not root.exists():
            return
        new_v, new_a, new_p = [], [], []
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                sfx = p.suffix.lower()
                if sfx in VIDEO_EXTS:    new_v.append(p)
                elif sfx in AUDIO_EXTS:  new_a.append(p)
                elif sfx in PLAYLIST_EXTS: new_p.append(p)
        except Exception:
            return

        with self._lock:
            existing_v = set(self.videos)
            existing_a = set(self.audio)
            existing_p = set(self.playlists)
            added_v = [p for p in new_v if p not in existing_v]
            added_a = [p for p in new_a if p not in existing_a]
            added_p = [p for p in new_p if p not in existing_p]
            self.videos    = sorted(self.videos + added_v,
                                    key=lambda p: p.stat().st_mtime if p.exists() else 0,
                                    reverse=True)
            self.audio     = sorted(self.audio + added_a,
                                    key=lambda p: p.name.lower())
            self.playlists = sorted(self.playlists + added_p,
                                    key=lambda p: p.name.lower())

        if added_v or added_a or added_p:
            self._notify("incremental_add")
            if added_a:
                threading.Thread(target=self._prefetch_meta,
                                 args=(added_a,), daemon=True).start()

    # ── metadata ──────────────────────────────────────────────────────────────
    def meta(self, path: Path) -> dict:
        """
        Return metadata dict for path.
        Tries cache first; falls back to tag reader; stores result.
        """
        cached = self._cache.get(path)
        if cached:
            return cached

        sfx = path.suffix.lower()
        m: dict = {
            "title":  path.stem,
            "artist": "",
            "album":  "",
            "genre":  "",
            "track":  "",
            "year":   "",
            "duration_secs": 0,
            "has_art": False,
            "type": "audio" if sfx in AUDIO_EXTS else "video",
            "path": str(path),
        }

        if sfx in AUDIO_EXTS:
            tags = read_tags(path)
            if tags["title"]:   m["title"]  = tags["title"]
            if tags["artist"]:  m["artist"] = tags["artist"]
            if tags["album"]:   m["album"]  = tags["album"]
            if tags["genre"]:   m["genre"]  = tags["genre"]
            if tags["track"]:   m["track"]  = tags["track"]
            if tags["year"]:    m["year"]   = tags["year"]

            # save album art
            art = tags.get("art_png_bytes")
            if art:
                saved = save_art(path, art)
                m["has_art"] = bool(saved)

        self._cache.put(path, m)
        return m

    def _prefetch_meta(self, paths: List[Path]):
        """Background pre-fetch metadata for a list of audio files."""
        for p in paths:
            if not self._cache.get(p):
                try:
                    self.meta(p)
                except Exception:
                    pass
        threading.Thread(target=self._cache.save, daemon=True).start()
        self._notify("meta_prefetch_done")

    def art_path(self, path: Path) -> Optional[Path]:
        """Return the path to cached album art for this file, or None."""
        return get_art_path(path)

    # ── add dropped / CLI files ───────────────────────────────────────────────
    def add_files(self, paths):
        changed = False
        with self._lock:
            vset = set(self.videos)
            aset = set(self.audio)
            pset = set(self.playlists)
            for raw in paths:
                p = Path(raw)
                sfx = p.suffix.lower()
                if sfx in VIDEO_EXTS and p not in vset:
                    self.videos.insert(0, p); vset.add(p); changed = True
                elif sfx in AUDIO_EXTS and p not in aset:
                    self.audio.insert(0, p); aset.add(p); changed = True
                elif sfx in PLAYLIST_EXTS and p not in pset:
                    self.playlists.insert(0, p); pset.add(p); changed = True
        if changed:
            self._notify("files_added")

    # ── grouped views ─────────────────────────────────────────────────────────
    def by_artist(self) -> Dict[str, List[Path]]:
        d: Dict[str, List[Path]] = {}
        with self._lock:
            audio = list(self.audio)
        for p in audio:
            a = self.meta(p).get("artist","") or "Unknown Artist"
            d.setdefault(a, []).append(p)
        return dict(sorted(d.items(), key=lambda x: x[0].lower()))

    def by_album(self) -> Dict[str, dict]:
        d: Dict[str, dict] = {}
        with self._lock:
            audio = list(self.audio)
        for p in audio:
            m  = self.meta(p)
            al = m.get("album","") or "Unknown Album"
            if al not in d:
                d[al] = {"artist": m.get("artist",""), "files": []}
            d[al]["files"].append(p)
        return dict(sorted(d.items(), key=lambda x: x[0].lower()))

    def by_genre(self) -> Dict[str, List[Path]]:
        d: Dict[str, List[Path]] = {}
        with self._lock:
            audio = list(self.audio)
        for p in audio:
            g = self.meta(p).get("genre","") or "Unknown"
            d.setdefault(g, []).append(p)
        return dict(sorted(d.items(), key=lambda x: x[0].lower()))

    def by_year(self) -> Dict[str, List[Path]]:
        d: Dict[str, List[Path]] = {}
        with self._lock:
            audio = list(self.audio)
        for p in audio:
            y = self.meta(p).get("year","") or "Unknown"
            d.setdefault(y, []).append(p)
        return dict(sorted(d.items(), reverse=True))

    def video_folders(self) -> Dict[str, List[Path]]:
        d: Dict[str, List[Path]] = {}
        with self._lock:
            vids = list(self.videos)
        for p in vids:
            d.setdefault(str(p.parent), []).append(p)
        return d

    def all_media(self) -> List[Path]:
        with self._lock:
            return self.videos + self.audio

    def recent(self, n: int = 40) -> List[Path]:
        with self._lock:
            mixed = self.videos[:n//2] + self.audio[:n//2]
        mixed.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return mixed[:n]

    def recently_added_videos(self, n: int = 50) -> List[Path]:
        with self._lock:
            return list(self.videos[:n])

    def recently_added_audio(self, n: int = 50) -> List[Path]:
        with self._lock:
            return list(self.audio[:n])

    def search(self, query: str) -> List[Path]:
        """Case-insensitive search across title/artist/album/filename."""
        q = query.lower().strip()
        if not q:
            return []
        results: List[Path] = []
        with self._lock:
            candidates = list(self.videos) + list(self.audio)
        for p in candidates:
            if q in p.name.lower():
                results.append(p)
                continue
            if p.suffix.lower() in AUDIO_EXTS:
                m = self.meta(p)
                if (q in m.get("title","").lower()  or
                    q in m.get("artist","").lower() or
                    q in m.get("album","").lower()):
                    results.append(p)
        return results[:200]

    def sort_all(self, key: str = "name") -> List[Path]:
        """Return all media sorted by key: 'name' | 'date' | 'artist' | 'album'"""
        with self._lock:
            combined = list(self.videos) + list(self.audio)
        if key == "date":
            combined.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        elif key == "artist":
            combined.sort(key=lambda p: self.meta(p).get("artist","").lower() or p.name.lower())
        elif key == "album":
            combined.sort(key=lambda p: self.meta(p).get("album","").lower() or p.name.lower())
        else:
            combined.sort(key=lambda p: p.name.lower())
        return combined

    # ── file-system watcher ───────────────────────────────────────────────────
    def start_watcher(self, interval: int = 30):
        """
        Start a polling watcher thread that checks for new/removed files
        every `interval` seconds.
        """
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._stop_watch.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), daemon=True)
        self._watch_thread.start()

    def stop_watcher(self):
        self._stop_watch.set()

    def _watch_loop(self, interval: int):
        # Seed initial snapshot
        def _snap():
            s: set = set()
            with self._lock:
                roots = list(self._roots)
            for root in roots:
                if not root.exists():
                    continue
                try:
                    for p in root.rglob("*"):
                        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
                            s.add(p)
                except Exception:
                    pass
            return s

        prev = _snap()
        while not self._stop_watch.wait(interval):
            curr = _snap()
            added   = curr - prev
            removed = prev - curr
            if added:
                self.add_files(list(added))
            if removed:
                with self._lock:
                    self.videos    = [p for p in self.videos    if p not in removed]
                    self.audio     = [p for p in self.audio     if p not in removed]
                    self.playlists = [p for p in self.playlists if p not in removed]
                for p in removed:
                    self._cache.remove(p)
                self._notify("files_removed")
            prev = curr
        # final cache flush
        self._cache.save()


# ─────────────────────────────────────────────────────────────────────────────
#  LIBRARY SETTINGS PANEL  (Tkinter widget — drop into any Frame)
# ─────────────────────────────────────────────────────────────────────────────

def make_library_settings_panel(parent_frame, lib: MediaLibrary,
                                 on_change: Optional[Callable] = None,
                                 theme: Optional[dict] = None):
    """
    Creates and returns a Tkinter Frame containing the library-settings UI.
    Embed it inside a popup or settings window.

    parent_frame : tk.Frame or Toplevel to place the widget in
    lib          : MediaLibrary instance
    on_change    : optional callback() after folders are added/removed
    theme        : dict with keys bg, panel, highlight, text, subtext, border, red
    """
    import tkinter as tk
    import tkinter.ttk as ttk
    import tkinter.filedialog as fd

    T = theme or {
        "bg": "#0d0d0d", "panel": "#181818", "panel2": "#1e1e1e",
        "highlight": "#e8672a", "text": "#f0f0f0", "subtext": "#888888",
        "border": "#2a2a2a", "red": "#e84040", "green": "#3dba6f",
    }

    outer = tk.Frame(parent_frame, bg=T["bg"])
    outer.pack(fill=tk.BOTH, expand=True)

    # ── header ────────────────────────────────────────────────────────────────
    tk.Label(outer, text="LIBRARY FOLDERS", bg=T["bg"], fg=T["subtext"],
             font=("Segoe UI", 8, "bold"), anchor="w").pack(fill=tk.X, padx=10, pady=(10, 2))

    # ── folder list ───────────────────────────────────────────────────────────
    list_frame = tk.Frame(outer, bg=T["panel"], bd=0)
    list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

    lb = tk.Listbox(list_frame, bg=T["panel"], fg=T["text"],
                    selectbackground=T["highlight"], selectforeground=T["bg"],
                    font=("Segoe UI", 10), borderwidth=0, highlightthickness=0,
                    activestyle="none", height=8)
    lb_sb = ttk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=lb_sb.set)
    lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
    lb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    def _refresh_lb():
        lb.delete(0, tk.END)
        for r in lib.get_roots():
            exists = Path(r).exists()
            prefix = "  ✓  " if exists else "  ✗  "
            lb.insert(tk.END, prefix + r)
            lb.itemconfig(tk.END, fg=T["text"] if exists else T["red"])

    _refresh_lb()

    # ── buttons ───────────────────────────────────────────────────────────────
    btn_row = tk.Frame(outer, bg=T["bg"])
    btn_row.pack(fill=tk.X, padx=10, pady=6)

    def _add():
        folder = fd.askdirectory(title="Add media folder")
        if folder:
            lib.add_folder(folder)
            _refresh_lb()
            if on_change: on_change()

    def _remove():
        sel = lb.curselection()
        if not sel: return
        raw = lb.get(sel[0]).strip().lstrip("✓✗").strip()
        lib.remove_folder(raw)
        _refresh_lb()
        if on_change: on_change()

    def _rescan():
        status_lbl.config(text="Scanning…", fg=T["subtext"])
        lib.scan()

    for text, cmd in [("＋ Add Folder", _add), ("－ Remove", _remove),
                      ("⟳ Rescan Now", _rescan)]:
        tk.Button(btn_row, text=text, command=cmd,
                  bg=T["border"], fg=T["text"], relief="flat",
                  font=("Segoe UI", 10), padx=10, pady=6,
                  activebackground=T["highlight"], activeforeground=T["bg"]
                  ).pack(side=tk.LEFT, padx=4)

    # ── status line ───────────────────────────────────────────────────────────
    status_lbl = tk.Label(outer, text="", bg=T["bg"], fg=T["subtext"],
                          font=("Segoe UI", 9), anchor="w")
    status_lbl.pack(fill=tk.X, padx=10, pady=(0, 4))

    def _update_status(reason: str):
        if reason in ("scan_done", "meta_prefetch_done"):
            msg = (f"  {len(lib.videos)} videos  ·  {len(lib.audio)} tracks  "
                   f"·  {len(lib.playlists)} playlists")
            status_lbl.config(text=msg, fg=T["green"])
        elif reason == "scan_progress":
            status_lbl.config(text=f"  {lib.scan_msg}", fg=T["subtext"])
        elif reason in ("files_added","incremental_add"):
            status_lbl.config(
                text=f"  Library updated — {len(lib.videos)} videos · {len(lib.audio)} tracks",
                fg=T["green"])
            _refresh_lb()

    lib.on_update(_update_status)

    # Initial status
    if lib.ready:
        _update_status("scan_done")
    else:
        status_lbl.config(text="  Scanning library…", fg=T["subtext"])

    return outer


# ─────────────────────────────────────────────────────────────────────────────
#  ID3 GENRE TABLE (ID3v1 codes)
# ─────────────────────────────────────────────────────────────────────────────
_ID3_GENRES = {
    0:"Blues",1:"Classic Rock",2:"Country",3:"Dance",4:"Disco",5:"Funk",
    6:"Grunge",7:"Hip-Hop",8:"Jazz",9:"Metal",10:"New Age",11:"Oldies",
    12:"Other",13:"Pop",14:"R&B",15:"Rap",16:"Reggae",17:"Rock",18:"Techno",
    19:"Industrial",20:"Alternative",21:"Ska",22:"Death Metal",23:"Pranks",
    24:"Soundtrack",25:"Euro-Techno",26:"Ambient",27:"Trip-Hop",28:"Vocal",
    29:"Jazz+Funk",30:"Fusion",31:"Trance",32:"Classical",33:"Instrumental",
    34:"Acid",35:"House",36:"Game",37:"Sound Clip",38:"Gospel",39:"Noise",
    40:"Alt. Rock",41:"Bass",42:"Soul",43:"Punk",44:"Space",45:"Meditative",
    46:"Instrumental Pop",47:"Instrumental Rock",48:"Ethnic",49:"Gothic",
    50:"Darkwave",51:"Techno-Industrial",52:"Electronic",53:"Pop-Folk",
    54:"Eurodance",55:"Dream",56:"Southern Rock",57:"Comedy",58:"Cult",
    59:"Gangsta Rap",60:"Top 40",61:"Christian Rap",62:"Pop/Funk",63:"Jungle",
    64:"Native American",65:"Cabaret",66:"New Wave",67:"Psychedelic",
    68:"Rave",69:"Showtunes",70:"Trailer",71:"Lo-Fi",72:"Tribal",73:"Acid Punk",
    74:"Acid Jazz",75:"Polka",76:"Retro",77:"Musical",78:"Rock & Roll",
    79:"Hard Rock",80:"Folk",81:"Folk-Rock",82:"National Folk",83:"Swing",
    84:"Fast-Fusion",85:"Bebop",86:"Latin",87:"Revival",88:"Celtic",89:"Bluegrass",
    90:"Avantgarde",91:"Gothic Rock",92:"Progressive Rock",93:"Psychedelic Rock",
    94:"Symphonic Rock",95:"Slow Rock",96:"Big Band",97:"Chorus",98:"Easy Listening",
    99:"Acoustic",100:"Humour",101:"Speech",102:"Chanson",103:"Opera",
    104:"Chamber Music",105:"Sonata",106:"Symphony",107:"Booty Bass",108:"Primus",
    109:"Porn Groove",110:"Satire",111:"Slow Jam",112:"Club",113:"Tango",
    114:"Samba",115:"Folklore",116:"Ballad",117:"Power Ballad",118:"Rhythmic Soul",
    119:"Freestyle",120:"Duet",121:"Punk Rock",122:"Drum Solo",123:"A Cappella",
    124:"Euro-House",125:"Dance Hall",126:"Goa",127:"Drum & Bass",
    128:"Club-House",129:"Hardcore",130:"Terror",131:"Indie",132:"BritPop",
    133:"Afropunk",134:"Polsk Punk",135:"Beat",136:"Christian Gangsta Rap",
    137:"Heavy Metal",138:"Black Metal",139:"Crossover",140:"Contemporary Christian",
    141:"Christian Rock",142:"Merengue",143:"Salsa",144:"Trash Metal",
    145:"Anime",146:"JPop",147:"Synthpop",148:"Abstract",149:"Art Rock",
    150:"Baroque",151:"Bhangra",152:"Big Beat",153:"Breakbeat",154:"Chillout",
    155:"Downtempo",156:"Dub",157:"EBM",158:"Eclectic",159:"Electro",
    160:"Electroclash",161:"Emo",162:"Experimental",163:"Garage",164:"Global",
    165:"IDM",166:"Illbient",167:"Industro-Goth",168:"Jam Band",169:"Krautrock",
    170:"Leftfield",171:"Lounge",172:"Math Rock",173:"New Romantic",174:"Nu-Breakz",
    175:"Post-Punk",176:"Post-Rock",177:"Psytrance",178:"Shoegaze",179:"Space Rock",
    180:"Trop Rock",181:"World Music",182:"Neoclassical",183:"Audiobook",
    184:"Audio Theatre",185:"Neue Deutsche Welle",186:"Podcast",187:"Indie Rock",
    188:"G-Funk",189:"Dubstep",190:"Garage Rock",191:"Psybient",
}
