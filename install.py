"""
VLC Controller — Automated Installer
=====================================
Run this script with:  python install.py

What it does
------------
1. Checks Python version (3.9+)
2. Installs pip dependencies  (pyinstaller, requests)
3. Detects an existing VLC install  OR  downloads + installs VLC silently
4. Writes a launcher config  (vlc_controller_config.json)
5. Builds a standalone  vlc_controller.exe  with PyInstaller
6. Creates a Start-menu / Desktop shortcut  (Windows)
7. Optionally registers the exe to auto-start
"""

import sys
import os
import subprocess
import shutil
import urllib.request
import json
import platform
import tempfile
import time
from pathlib import Path

# ─── versioning ───
REQUIRED_PYTHON = (3, 9)
APP_NAME        = "PlayerOne"
APP_VERSION     = "1.0.0"
VLC_INSTALLER_URL = (
    "https://mirror.downloadvn.com/videolan/vlc/last/win64/"
    "vlc-{ver}-win64.exe"
)
VLC_VERSION = "3.0.21"   # pinned; change if newer stable is out

# ─── paths ───
SCRIPT_DIR   = Path(__file__).parent.resolve()
MAIN_SCRIPT  = SCRIPT_DIR / "vlc_controller.py"
DIST_DIR     = SCRIPT_DIR / "dist"
BUILD_DIR    = SCRIPT_DIR / "build"
ICON_PATH    = SCRIPT_DIR / "icon.ico"   # optional; bundled if present
CONFIG_FILE  = SCRIPT_DIR / "vlc_controller_config.json"
SHORTCUT_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"

VLC_PATH_CANDIDATES = [
    Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
    SCRIPT_DIR / "vlc" / "vlc.exe",
]

def banner(msg):
    print(f"\n{'─'*60}")
    print(f"  {msg}")
    print('─'*60)

def step(msg):
    print(f"  ▶  {msg}")

def ok(msg=""):
    print(f"  ✓  {msg}" if msg else "  ✓")

def warn(msg):
    print(f"  ⚠  {msg}")

def fail(msg):
    print(f"\n  ✗  ERROR: {msg}")
    sys.exit(1)

# ─────────────────────────────────────────────
# 1. PYTHON VERSION CHECK
# ─────────────────────────────────────────────
def check_python():
    banner("Checking Python version")
    v = sys.version_info[:2]
    if v < REQUIRED_PYTHON:
        fail(f"Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}+ required. "
             f"You have {v[0]}.{v[1]}.")
    ok(f"Python {v[0]}.{v[1]} — OK")

# ─────────────────────────────────────────────
# 2. PIP DEPENDENCIES
# ─────────────────────────────────────────────
def install_deps():
    banner("Installing Python dependencies")
    deps = ["pyinstaller", "requests", "pywin32"]
    for dep in deps:
        step(f"pip install {dep}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", dep],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            warn(f"Could not install {dep}: {result.stderr.strip()}")
        else:
            ok(f"{dep} installed/up-to-date")

# ─────────────────────────────────────────────
# 3. VLC DETECTION / INSTALL
# ─────────────────────────────────────────────
def find_vlc():
    for p in VLC_PATH_CANDIDATES:
        if p.is_file():
            return p
    return None

def download_vlc():
    """Download the VLC installer and run it silently."""
    url = VLC_INSTALLER_URL.format(ver=VLC_VERSION)
    tmp = Path(tempfile.gettempdir()) / f"vlc_{VLC_VERSION}_setup.exe"
    if not tmp.exists():
        step(f"Downloading VLC {VLC_VERSION} …")
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length", 0))
                downloaded = 0
                block = 65536
                while True:
                    data = r.read(block)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    if total:
                        pct = downloaded * 100 // total
                        print(f"\r  Downloading … {pct}%", end="", flush=True)
            print()
            ok("Download complete")
        except Exception as e:
            fail(f"Download failed: {e}\nPlease install VLC manually from https://www.videolan.org/")
    else:
        ok(f"Installer cached at {tmp}")

    step("Running VLC installer silently (may need UAC prompt) …")
    result = subprocess.run(
        [str(tmp), "/S", "/L=1033"],   # /S = silent, /L=1033 = English
        capture_output=True
    )
    time.sleep(3)
    vlc = find_vlc()
    if vlc:
        ok(f"VLC installed at {vlc}")
        return vlc
    else:
        warn("Could not find VLC after install — you may need to install it manually.")
        return None

def ensure_vlc():
    banner("Checking for VLC")
    vlc = find_vlc()
    if vlc:
        ok(f"Found VLC at: {vlc}")
        return vlc
    warn("VLC not found in standard locations.")
    if platform.system() == "Windows":
        answer = input("  Download and install VLC automatically? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            return download_vlc()
        else:
            print("  Please install VLC from https://www.videolan.org/ and re-run this installer.")
            return None
    else:
        print("  Please install VLC via your package manager and re-run this installer.")
        return None

# ─────────────────────────────────────────────
# 4. WRITE CONFIG
# ─────────────────────────────────────────────
def write_config(vlc_path):
    banner("Writing config")
    config = {
        "vlc_path":      str(vlc_path) if vlc_path else "",
        "http_host":     "127.0.0.1",
        "http_port":     43210,
        "http_password": "vlccontroller",
        "volume_step":   5,
        "seek_step":     5,
        "cooldown_a":    0.5,
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    ok(f"Config written to {CONFIG_FILE}")

# ─────────────────────────────────────────────
# 5. BUILD EXE WITH PYINSTALLER
# ─────────────────────────────────────────────
def build_exe():
    banner("Building standalone EXE (PyInstaller)")
    step("This may take a minute …")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        f"--name={APP_NAME.replace(' ', '_')}",
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        "--noconfirm",
    ]
    if ICON_PATH.is_file():
        args += [f"--icon={ICON_PATH}"]
    # embed config next to exe at runtime
    args += [
        f"--add-data={CONFIG_FILE};.",
    ]
    args.append(str(MAIN_SCRIPT))

    result = subprocess.run(args, capture_output=False)
    if result.returncode != 0:
        fail("PyInstaller build failed. Check output above.")
    exe = DIST_DIR / f"{APP_NAME.replace(' ', '_')}.exe"
    if exe.is_file():
        ok(f"EXE built: {exe}")
        # Copy a loose icon.ico next to the exe — PlayerOne reads this at
        # runtime for its window icon and for file-association icons.
        if ICON_PATH.is_file():
            try:
                shutil.copy2(ICON_PATH, DIST_DIR / "icon.ico")
                ok("icon.ico copied next to exe")
            except Exception as e:
                warn(f"Could not copy icon.ico: {e}")
        return exe
    else:
        warn("EXE not found after build — check the dist/ folder manually.")
        return None

# ─────────────────────────────────────────────
# 6. CREATE SHORTCUTS (Windows only)
# ─────────────────────────────────────────────
def create_shortcuts(exe_path):
    if platform.system() != "Windows":
        return
    banner("Creating shortcuts")
    try:
        import win32com.client
    except ImportError:
        warn("pywin32 not available — skipping shortcut creation.")
        return

    def make_shortcut(dest_dir, name):
        dest = Path(dest_dir) / f"{name}.lnk"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortCut(str(dest))
        sc.Targetpath = str(exe_path)
        sc.WorkingDirectory = str(exe_path.parent)
        sc.Description = f"{APP_NAME} {APP_VERSION}"
        if ICON_PATH.is_file():
            sc.IconLocation = str(ICON_PATH)
        sc.save()
        ok(f"Shortcut → {dest}")

    # Start Menu
    make_shortcut(SHORTCUT_DIR, APP_NAME)
    # Desktop
    desktop = Path(os.environ.get("USERPROFILE", "~")).expanduser() / "Desktop"
    answer = input("  Create Desktop shortcut? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        make_shortcut(desktop, APP_NAME)

# ─────────────────────────────────────────────
# 7. OPTIONAL AUTO-START
# ─────────────────────────────────────────────
def optional_autostart(exe_path):
    if platform.system() != "Windows":
        return
    banner("Auto-start on login (optional)")
    answer = input("  Launch VLC Controller when Windows starts? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, str(exe_path))
        winreg.CloseKey(key)
        ok("Added to startup registry")
    except Exception as e:
        warn(f"Could not write registry: {e}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print(f"\n{'═'*60}")
    print(f"  {APP_NAME}  v{APP_VERSION}  — Installer")
    print(f"{'═'*60}")

    check_python()
    install_deps()
    vlc_path = ensure_vlc()
    write_config(vlc_path)

    # Ask before building exe (allows running the .py directly)
    banner("Build Options")
    print("  (a) Build standalone EXE  — requires PyInstaller, takes ~1 min")
    print("  (b) Run as Python script  — launch with: python vlc_controller.py")
    choice = input("  Choice [a/b]: ").strip().lower()

    exe_path = None
    if choice in ("", "a"):
        exe_path = build_exe()
        if exe_path:
            create_shortcuts(exe_path)
            optional_autostart(exe_path)
    else:
        ok("Skipping EXE build — run  python vlc_controller.py  to start.")

    banner("Installation complete")
    if exe_path:
        print(f"  Launch: {exe_path}")
    else:
        print(f"  Launch: python \"{MAIN_SCRIPT}\"")
    print()
    print("  Controller mapping summary")
    print("  ─────────────────────────────────────────────")
    print("  START          → Open / close main menu")
    print("  SELECT / BACK  → Open / close context menu")
    print("  X              → Open / close filter menu")
    print("  Y              → Open / close playlist queue")
    print("  A              → Confirm / Play·Pause  (0.5 s cooldown)")
    print("  B              → Cancel / Stop / Back")
    print("  LB / RB        → Previous / Next")
    print("  L3             → Extended settings menu")
    print("  R3             → Fullscreen toggle")
    print("  Left stick     → Navigate menus / lists")
    print("  Right stick ←→ → Seek")
    print("  Right stick ↑↓ → Volume")
    print()

if __name__ == "__main__":
    main()
