r"""oslib.py - the few things that differ between Windows, macOS and Linux, in one place (stdlib only).

  NO_WINDOW                 creationflags for every subprocess: no console window on Windows, 0 elsewhere
  system()                  "windows" | "mac" | "linux"; anything else raises OSLibError (not supported)
  data_dir(app)             the app's private folder:
                              Windows %LOCALAPPDATA%\<app>, macOS ~/Library/Application Support/<app>,
                              Linux $XDG_DATA_HOME/<app> (default ~/.local/share/<app>)
  venv_python(env)          the interpreter inside a virtual environment (Scripts\python.exe or bin/python)
  exe_name(name)            name + ".exe" on Windows
  which(name)               shutil.which, or None
  open_path(path)           open a file or folder with the system's default app (startfile / open / xdg-open)
  open_url(url)             open a page (file:// or http) in the default browser
  home()                    the user's home folder
  pick_file(initial, title, types)   the operating system's own "Open" dialog (Tk); None when cancelled, NoDialog
                            when no dialog can open (no screen, no Tk). Blocks until the user answers.

Library only. Opening never raises: open_path/open_url return {"opened": bool} plus "open_error" when the launch
failed, so a caller that wrote a page can still tell the user where it is.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # child processes never open a console window


class OSLibError(Exception):
    pass


def system() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "linux"
    raise OSLibError(f"unsupported system {sys.platform!r} (Windows, macOS and Linux only)")


def home() -> Path:
    return Path.home()


def data_dir(app: str) -> Path:
    s = system()
    if s == "windows":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise OSLibError("LOCALAPPDATA is not set")
        return Path(base) / app
    if s == "mac":
        return home() / "Library" / "Application Support" / app
    return Path(os.environ.get("XDG_DATA_HOME") or home() / ".local" / "share") / app


def venv_python(env: Path) -> Path:
    return env / "Scripts" / "python.exe" if system() == "windows" else env / "bin" / "python"


def exe_name(name: str) -> str:
    return name + ".exe" if system() == "windows" else name


def which(name: str) -> Path | None:
    p = shutil.which(name)
    return Path(p) if p else None


def open_path(path: Path | str) -> dict:
    p = str(path)
    try:
        s = system()
        if s == "windows":
            os.startfile(p)   # noqa: S606 - the user's default app, by design
        else:
            subprocess.Popen(["open" if s == "mac" else "xdg-open", p], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, OSLibError) as exc:
        return {"opened": False, "open_error": f"could not open {p} ({exc}); open it by hand"}
    return {"opened": True}


def open_url(url: str) -> dict:
    try:
        ok = webbrowser.open(url, new=2)
    except webbrowser.Error as exc:
        return {"opened": False, "open_error": f"could not open the page ({exc}); open {url} by hand"}
    return {"opened": True} if ok else {"opened": False, "open_error": f"no browser found; open {url} by hand"}


class NoDialog(Exception):
    """The native dialog cannot open here (no screen, or Python without Tk)."""


def pick_file(initial: Path | None, title: str, types: list[tuple[str, str]]) -> Path | None:
    """The operating system's own "Open" dialog (Tk's askopenfilename), on top of other windows. Returns the chosen
    file, or None when the user cancels. Raises NoDialog when no dialog can be shown. A deliberate window for the
    user, like opening a gate page."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as e:
        raise NoDialog(f"this Python has no Tk ({e})") from e
    if os.name == "nt":   # sharp on high-resolution screens (not blurry, scaled bitmaps)
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    try:
        root = tk.Tk()
    except tk.TclError as e:
        raise NoDialog(f"no screen to show a dialog on ({e})") from e
    try:
        start = initial if initial and initial.is_dir() else home()
        if os.name == "nt":
            # a tiny, invisible owner window brought to the front, so the dialog opens on top and with focus
            root.geometry("1x1+200+200")
            root.attributes("-alpha", 0.0)
            root.attributes("-topmost", True)
            root.lift()
            root.focus_force()
            root.update()
            chosen = filedialog.askopenfilename(parent=root, initialdir=str(start), title=title, filetypes=types)
        else:   # macOS attaches a dialog with a parent to that window as a sheet: keep the owner hidden, no parent
            root.withdraw()
            chosen = filedialog.askopenfilename(initialdir=str(start), title=title, filetypes=types)
    except tk.TclError as e:
        raise NoDialog(f"the file dialog failed to open ({e})") from e
    finally:
        root.destroy()
    return Path(chosen) if chosen else None
