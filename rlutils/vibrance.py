"""Rocket League vibrance: NVIDIA Digital Vibrance (what you see) and an OBS saturation filter (what's recorded).

Digital Vibrance is set the way vibranceGUI does it (its embedded vibranceDLL.dll calls NVAPI through
nvapi_QueryInterface: NvAPI_Initialize, EnumNvidiaDisplayHandle, GetAssociatedNvidiaDisplayHandle,
GetDVCInfo, SetDVCLevel). The GPU applies it on the way to the monitor, after the frame is drawn, so
recorders (OBS, Medal, NVIDIA's own) never see it. For recordings, the same boost is applied in OBS as a
Color Correction filter on the Rocket League capture, through obs-websocket. Nothing touches the game.
"""
from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes

# NVAPI function ids (nvapi_QueryInterface), as used by vibranceGUI's vibranceDLL.dll
_NVAPI_IDS = {
    "Initialize": 0x0150E828,
    "EnumNvidiaDisplayHandle": 0x9ABDD40D,
    "GetAssociatedNvidiaDisplayHandle": 0x35C29134,
    "GetDVCInfo": 0x4085DE45,
    "SetDVCLevel": 0x172409B4,
}
NVAPI_OK = 0


class NV_DISPLAY_DVC_INFO(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("currentLevel", ctypes.c_uint32),
                ("minLevel", ctypes.c_uint32), ("maxLevel", ctypes.c_uint32)]


class VibranceError(Exception):
    pass


class Nvapi:
    """Digital Vibrance per NVIDIA display. Levels are NVAPI's (usually 0..63; 0 = the 50% default)."""

    def __init__(self):
        if sys.platform != "win32":
            raise VibranceError("NVIDIA vibrance needs Windows")
        try:
            dll = ctypes.WinDLL("nvapi64.dll" if ctypes.sizeof(ctypes.c_void_p) == 8 else "nvapi.dll")
        except OSError as exc:
            raise VibranceError("needs an NVIDIA graphics card (no NVIDIA driver found), so it stays off") from exc
        qi = dll.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        qi.argtypes = [ctypes.c_uint32]
        handle = ctypes.c_void_p

        def fn(name, *args):
            addr = qi(_NVAPI_IDS[name])
            if not addr:
                raise VibranceError(f"NVAPI has no {name}")
            return ctypes.CFUNCTYPE(ctypes.c_int, *args)(addr)

        self._init = fn("Initialize")
        self._enum = fn("EnumNvidiaDisplayHandle", ctypes.c_uint32, ctypes.POINTER(handle))
        self._assoc = fn("GetAssociatedNvidiaDisplayHandle", ctypes.c_char_p, ctypes.POINTER(handle))
        self._info = fn("GetDVCInfo", handle, ctypes.c_uint32, ctypes.POINTER(NV_DISPLAY_DVC_INFO))
        self._set = fn("SetDVCLevel", handle, ctypes.c_uint32, ctypes.c_int)
        if self._init() != NVAPI_OK:
            raise VibranceError("NVAPI didn't initialise")

    def displays(self) -> list[int]:
        out, i = [], 0
        while True:
            h = ctypes.c_void_p()
            if self._enum(i, ctypes.byref(h)) != NVAPI_OK:
                return out
            out.append(h.value)
            i += 1

    def display_for(self, device_name: str) -> int | None:
        """NVIDIA display handle for a GDI device name such as \\\\.\\DISPLAY1."""
        h = ctypes.c_void_p()
        return h.value if self._assoc(device_name.encode(), ctypes.byref(h)) == NVAPI_OK else None

    def info(self, display: int) -> tuple[int, int, int]:
        """(current, min, max) level."""
        dvc = NV_DISPLAY_DVC_INFO(version=ctypes.sizeof(NV_DISPLAY_DVC_INFO) | (1 << 16))
        status = self._info(display, 0, ctypes.byref(dvc))
        if status != NVAPI_OK:
            raise VibranceError(f"GetDVCInfo failed ({status})")
        return dvc.currentLevel, dvc.minLevel, dvc.maxLevel

    def set_level(self, display: int, level: int) -> None:
        status = self._set(display, 0, int(level))
        if status != NVAPI_OK:
            raise VibranceError(f"SetDVCLevel failed ({status})")


def pct_to_level(pct: int, lo: int, hi: int) -> int:
    """NVIDIA control panel percent (50 = default, 100 = max) -> NVAPI level."""
    pct = max(50, min(100, int(pct)))
    return lo + round((hi - lo) * (pct - 50) / 50)


def level_to_pct(level: int, lo: int, hi: int) -> int:
    return 50 + round(50 * (level - lo) / max(1, hi - lo))


# ----------------------------------------------------------------------------------------------------
# Which window is in front, and which monitor it's on
# ----------------------------------------------------------------------------------------------------


class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def foreground_window() -> int:
    return ctypes.windll.user32.GetForegroundWindow() or 0


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def monitor_device(hwnd: int) -> str:
    """GDI device name (\\\\.\\DISPLAYn) of the monitor showing `hwnd`."""
    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFOEX(cbSize=ctypes.sizeof(_MONITORINFOEX))
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return mi.szDevice if user32.GetMonitorInfoW(mon, ctypes.byref(mi)) else ""


# ----------------------------------------------------------------------------------------------------
# OBS: a Color Correction filter on the Rocket League capture, through obs-websocket (protocol v5)
# ----------------------------------------------------------------------------------------------------

OBS_FILTER = "RLUtils vibrance"
OBS_FILTER_KIND = "color_filter_v2"  # OBS's "Color Correction"


def pct_to_obs_saturation(pct: int, strength: float = 1.0) -> float:
    """Vibrance percent -> the filter's saturation (0 = unchanged, 1 = double). An approximation: Digital
    Vibrance is the driver's own curve, which recordings can't sample, so 100% maps to `strength`."""
    return round(max(0.0, (max(50, min(100, int(pct))) - 50) / 50 * strength), 3)


class Obs:
    """Minimal obs-websocket v5 client (no authentication, or a password)."""

    def __init__(self, url: str = "ws://127.0.0.1:4455", password: str = "", timeout: float = 3):
        import websocket  # websocket-client
        self.ws = websocket.create_connection(url, timeout=timeout)
        hello = json.loads(self.ws.recv())
        ident = {"rpcVersion": 1}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not password:
                raise VibranceError("OBS websocket wants a password")
            import base64, hashlib
            secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self.ws.send(json.dumps({"op": 1, "d": ident}))
        if json.loads(self.ws.recv()).get("op") != 2:
            raise VibranceError("OBS websocket refused the connection")
        self._n = 0

    def close(self) -> None:
        self.ws.close()

    def call(self, request: str, data: dict | None = None, ok_codes=(100,)) -> dict:
        self._n += 1
        rid = str(self._n)
        self.ws.send(json.dumps({"op": 6, "d": {"requestType": request, "requestId": rid, "requestData": data or {}}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                status = msg["d"]["requestStatus"]
                if status.get("code") not in ok_codes:
                    raise VibranceError(f"OBS {request}: {status.get('comment') or status.get('code')}")
                return msg["d"].get("responseData") or {}

    def rl_sources(self) -> list[str]:
        """Inputs capturing Rocket League (game/window capture whose window names RocketLeague.exe)."""
        names = []
        for inp in self.call("GetInputList")["inputs"]:
            if inp.get("inputKind") not in ("game_capture", "window_capture"):
                continue
            settings = self.call("GetInputSettings", {"inputName": inp["inputName"]})["inputSettings"]
            if "rocketleague.exe" in str(settings.get("window", "")).lower():
                names.append(inp["inputName"])
        return names

    def set_saturation(self, source: str, saturation: float) -> str:
        """Create or update the vibrance filter on `source`; 0 disables it. Returns what was done."""
        try:
            self.call("GetSourceFilter", {"sourceName": source, "filterName": OBS_FILTER})
            exists = True
        except VibranceError:
            exists = False
        settings = {"saturation": saturation}
        if not exists:
            if saturation <= 0:
                return "off"
            self.call("CreateSourceFilter", {"sourceName": source, "filterName": OBS_FILTER,
                                             "filterKind": OBS_FILTER_KIND, "filterSettings": settings})
            return "created"
        self.call("SetSourceFilterSettings", {"sourceName": source, "filterName": OBS_FILTER,
                                              "filterSettings": settings, "overlay": True})
        self.call("SetSourceFilterEnabled", {"sourceName": source, "filterName": OBS_FILTER,
                                             "filterEnabled": saturation > 0})
        return "updated" if saturation > 0 else "off"
