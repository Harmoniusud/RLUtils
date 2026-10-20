"""Single entry point, from source or packaged.

  python rlutils.py           desktop app (RLUtils.exe); --tray starts it in the tray
"""
import sys


def _close_own_console() -> None:
    """Started by double-clicking with python.exe, Windows opens a console just for this app: hide it.
    A console that a terminal or shell also uses (you typed the command there) is left alone."""
    if sys.platform != "win32":
        return
    import ctypes
    import os
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    kernel32.OpenProcess.restype = ctypes.c_void_p
    hwnd = kernel32.GetConsoleWindow()
    if not hwnd:
        return
    pids = (ctypes.c_uint * 16)()
    count = kernel32.GetConsoleProcessList(pids, 16)
    for pid in pids[:min(count, 16)]:
        # Only Python itself on the console (the python.exe launcher may start a second Python on it).
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            if not kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)):
                return
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        if not os.path.basename(buf.value).lower().startswith(("python", "py.exe", "pyw")):
            return
    user32.ShowWindow(ctypes.c_void_p(hwnd), 0)  # SW_HIDE
    kernel32.FreeConsole()
    sys.stdout = sys.stderr = None  # no console any more; _ensure_streams logs to a file instead


def _ensure_streams() -> None:
    # A windowed build has no console, so print(..., file=sys.stderr) would fail. Log to a file instead.
    if sys.stderr is None or sys.stdout is None:
        from rlutils.data import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(DATA_DIR / "rlutils.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


if __name__ == "__main__":
    _close_own_console()
    _ensure_streams()
    from rlutils.app import main as app_main

    sys.exit(app_main())
