"""Live ranked MMR straight from Rocket League's servers (PsyNet), the way https://github.com/dank/rlapi does it.

Sign-in happens once, in the user's own browser (Epic's device sign-in, epicgames.com/activate): the app shows
a code, the user signs in on Epic's site and approves it, and the app, polling, gets an Epic token. That token
creates an Epic "device auth" (account id, device id, secret) for this PC, a permanent login that lasts until
the user changes their password or removes it on epicgames.com. Only the device auth is kept, encrypted with
Windows DPAPI (only this Windows user on this PC can decrypt it) in the data folder's epic_login.dat. The
password is typed into Epic's site and never reaches the app.

Each fetch:
    device auth -> Epic token                                              account-public-service-prod03.ol.epicgames.com
    Epic token -> exchange code -> EOS token for Rocket League             api.epicgames.dev
    EOS token -> Auth/AuthPlayer/v2 -> PsyToken + WebSocket URL              api.rlpp.psynet.gg
    WebSocket: Skills/GetPlayerSkill v1 for the player's own id, then close  ws.rlpp.psynet.gg

Nothing is sent anywhere else. Requests are signed with PsySig (HMAC-SHA256 with a key from the game); the
game version / feature set / build secret change with game updates: when PsyNet refuses them, the current
values are read from rlapi's psynet.go on GitHub (data only, parsed with a regex) and kept in psynet_version.json.

A fetch signs into PsyNet as the player, like the game does, so it is never done while Rocket League runs
(the game's own session could be dropped): the caller checks that.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EGS_UA = "UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit"
EGS_CLIENT = ("34a02cf8f4414e29b15921876da36f9a", "daafbccc737745039dffe53d94fc76cf")  # Epic launcher
DEVICE_CODE_CLIENT = ("98f7e42c2e3a4f86a74eb43fbb41ed39", "0a2449a2-001a-451e-afec-3e812901c4d7")  # allows device codes
DEVICE_AUTH_CLIENT = ("3f69e56c7649492c8cc29f1af08a8a12", "b51ee9cb12234f50a69efa67ef53812e")  # allows device auths
EGS_HOST = "https://account-public-service-prod03.ol.epicgames.com"
EOS_CLIENT = ("xyza7891p5D7s9R6Gm6moTHWGloerp7B", "Knh18du4NVlFs+3uQ+ZPpDCVto0WYf4yXP8+OcwVt1o")  # Rocket League
EOS_DEPLOYMENT = "da32ae9c12ae40e8a112c52e1f17f3ba"
PSY_HTTP = "https://api.rlpp.psynet.gg/rpc"
PSY_SIG_KEY = b"c338bd36fb8c42b1a431d30add939fc7"
RLAPI_SOURCE = "https://raw.githubusercontent.com/dank/rlapi/main/psynet.go"

# From rlapi (psynet.go); replaced by psynet_version.json once a newer set was fetched.
DEFAULT_VERSION = {
    "gameVersion": "261013.63066.530866",
    "featureSet": "PrimeUpdate60_FFE7A62B41040BB67F8829FD67F2EE8A",
    "buildSecret": "76656a5b51ec57a13549eef732c8a8367638aa3a7989bade1f7e4e5f93d57625",
}

TIMEOUT = 20


class LoginExpired(Exception):
    """The saved sign-in no longer works (password changed, or removed on epicgames.com): sign in again."""


class PsyNetError(Exception):
    pass


# ====================================================================================================
# The saved sign-in: DPAPI-encrypted, this Windows user only.
# ====================================================================================================


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_ENTROPY = b"RLUtils epic login v1"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    ent = _Blob(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY)),
                                            ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = (fn(ctypes.byref(src), "RLUtils", ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)) if encrypt
          else fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)))
    if not ok:
        raise OSError("Windows couldn't " + ("encrypt" if encrypt else "decrypt") + " the saved Epic sign-in")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


class LoginStore:
    """{account id (32 hex): {"name", "device_id", "secret"}} in one encrypted file. Sign-ins from 0.11.1 hold
    {"name", "refresh"} instead and are turned into a device auth on their next use."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        try:
            return json.loads(_dpapi(raw, False).decode("utf-8")).get("accounts", {})
        except (OSError, ValueError):
            return {}

    def _write(self, accounts: dict) -> None:
        blob = _dpapi(json.dumps({"accounts": accounts}).encode("utf-8"), True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(self.path)

    def put(self, account: str, name: str, **creds: str) -> None:
        with self.lock:
            accounts = self.load()
            accounts[account.lower()] = {"name": name, **creds}
            self._write(accounts)

    def remove(self, account: str) -> None:
        with self.lock:
            accounts = self.load()
            if accounts.pop(account.lower(), None) is not None:
                if accounts:
                    self._write(accounts)
                else:
                    self.path.unlink(missing_ok=True)

    def accounts(self) -> list[tuple[str, str]]:
        """(account id, Epic name) of everyone signed in."""
        return [(k, v.get("name", "")) for k, v in self.load().items()]


# ====================================================================================================
# HTTP
# ====================================================================================================


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _basic(client: tuple[str, str]) -> str:
    return "Basic " + base64.b64encode(f"{client[0]}:{client[1]}".encode()).decode()


def _form(url: str, client: tuple[str, str], fields: dict) -> dict:
    status, body = _request(url, data=urllib.parse.urlencode(fields).encode(), headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": EGS_UA, "Authorization": _basic(client)})
    try:
        js = json.loads(body or b"{}")
    except ValueError:
        js = {}
    if status != 200:
        code = js.get("errorCode") or ""
        if code.endswith("authorization_pending") or code.endswith("slow_down"):
            raise Pending(code)
        msg = js.get("errorMessage") or code or body[:200].decode("utf-8", "replace")
        saved_login = fields.get("grant_type") in ("refresh_token", "device_auth")
        raise (LoginExpired if saved_login and status in (400, 401) else PsyNetError)(f"Epic said {status}: {msg}")
    return js


class Pending(Exception):
    """The user hasn't approved the device sign-in in their browser yet."""


def _token(client: tuple[str, str], fields: dict) -> dict:
    return _form(f"{EGS_HOST}/account/api/oauth/token", client, {**fields, "token_type": "eg1"})


@dataclass
class DeviceLogin:
    user_code: str
    device_code: str
    url: str            # epicgames.com/activate with the code filled in: opened in the user's browser
    interval: int
    expires_at: float   # time.monotonic()


def start_device_login() -> DeviceLogin:
    """Ask Epic for a sign-in code the user approves in their browser."""
    client = _token(DEVICE_CODE_CLIENT, {"grant_type": "client_credentials"})
    status, body = _request(f"{EGS_HOST}/account/api/oauth/deviceAuthorization", data=b"prompt=login", headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": EGS_UA,
        "Authorization": "bearer " + client["access_token"]})
    if status != 200:
        raise PsyNetError(f"Epic sign-in couldn't start: {status}")
    js = json.loads(body)
    return DeviceLogin(js["user_code"], js["device_code"],
                       js.get("verification_uri_complete") or js["verification_uri"],
                       max(2, int(js.get("interval") or 10)), time.monotonic() + int(js.get("expires_in") or 600))


def poll_device_login(login: DeviceLogin) -> tuple[str, str, dict] | None:
    """None while the user hasn't approved it yet; once they have: (account id, Epic name, credentials to save).
    The credentials are a device auth made for this PC."""
    try:
        tok = _token(DEVICE_CODE_CLIENT, {"grant_type": "device_code", "device_code": login.device_code})
    except Pending:
        return None
    try:
        return _make_device_auth(tok)
    except (PsyNetError, KeyError, ValueError):  # Epic refused a device auth: keep its refresh token instead
        return tok["account_id"].lower(), tok.get("displayName", ""), {"refresh": tok["refresh_token"],
                                                                         "client": "device_code"}


def _make_device_auth(tok: dict) -> tuple[str, str, dict]:
    """Any Epic token -> a device auth on the client that may use one (via an exchange code)."""
    dev_tok = _token(DEVICE_AUTH_CLIENT, {"grant_type": "exchange_code", "exchange_code": _exchange_code(tok["access_token"])})
    account = dev_tok["account_id"]
    status, body = _request(f"{EGS_HOST}/account/api/public/account/{account}/deviceAuth", data=b"{}", headers={
        "Content-Type": "application/json", "User-Agent": EGS_UA, "Authorization": "bearer " + dev_tok["access_token"]})
    if status != 200:
        raise PsyNetError(f"Epic couldn't save the sign-in for this PC: {status}")
    js = json.loads(body)
    name = dev_tok.get("displayName") or tok.get("displayName") or ""
    return account.lower(), name, {"device_id": js["deviceId"], "secret": js["secret"]}


def _signed_in_token(store: LoginStore, account: str) -> dict:
    """An Epic token for a saved account. A 0.11.1 sign-in (refresh token) becomes a device auth here."""
    saved = store.load().get(account.lower())
    if not saved:
        raise LoginExpired("not signed in")
    if saved.get("secret"):
        return _token(DEVICE_AUTH_CLIENT, {"grant_type": "device_auth", "account_id": account,
                                           "device_id": saved["device_id"], "secret": saved["secret"]})
    client = DEVICE_CODE_CLIENT if saved.get("client") == "device_code" else EGS_CLIENT
    tok = _token(client, {"grant_type": "refresh_token", "refresh_token": saved["refresh"]})
    try:
        acc, name, creds = _make_device_auth(tok)
    except (PsyNetError, KeyError, ValueError):
        acc, name = tok["account_id"].lower(), tok.get("displayName", "")
        creds = {"refresh": tok["refresh_token"], **({"client": saved["client"]} if saved.get("client") else {})}
    store.put(acc, name or saved.get("name", ""), **creds)
    return tok


def sign_out(store: LoginStore, account: str) -> None:
    """Delete the saved sign-in here and, best effort, the device auth on Epic's side too."""
    saved = store.load().get(account.lower()) or {}
    store.remove(account)
    if not saved.get("secret"):
        return
    try:
        tok = _token(DEVICE_AUTH_CLIENT, {"grant_type": "device_auth", "account_id": account,
                                          "device_id": saved["device_id"], "secret": saved["secret"]})
        _request(f"{EGS_HOST}/account/api/public/account/{account}/deviceAuth/{saved['device_id']}", method="DELETE",
                 headers={"User-Agent": EGS_UA, "Authorization": "bearer " + tok["access_token"]})
    except Exception:
        pass  # offline: the local copy is gone either way; it can also be removed on epicgames.com


def _exchange_code(access_token: str) -> str:
    status, body = _request(f"{EGS_HOST}/account/api/oauth/exchange",
                            headers={"Authorization": "bearer " + access_token, "User-Agent": EGS_UA})
    if status != 200:
        raise PsyNetError(f"Epic exchange code: {status}")
    return json.loads(body)["code"]


def _eos_token(exchange_code: str) -> dict:
    return _form("https://api.epicgames.dev/epic/oauth/v2/token", EOS_CLIENT,
                 {"grant_type": "exchange_code", "exchange_code": exchange_code,
                  "deployment_id": EOS_DEPLOYMENT, "scope": "basic_profile"})




# ====================================================================================================
# PsyNet
# ====================================================================================================


def build_id(game_version: str) -> int:
    """PsyBuildID: big-endian CRC-32 of the version as UTF-16LE, as a signed int (rlapi buildid.go)."""
    crc = 0xFFFFFFFF
    for b in game_version.encode("utf-16-le"):
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    crc ^= 0xFFFFFFFF
    return crc - (1 << 32) if crc & 0x80000000 else crc


def psy_sig(body: bytes) -> str:
    return base64.b64encode(hmac.new(PSY_SIG_KEY, b"-" + body, hashlib.sha256).digest()).decode()


class Version:
    """gameVersion / featureSet / buildSecret, kept in psynet_version.json after an update from rlapi."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.v = dict(DEFAULT_VERSION)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if all(isinstance(saved.get(k), str) and saved[k] for k in DEFAULT_VERSION):
                self.v = {k: saved[k] for k in DEFAULT_VERSION}
        except (OSError, ValueError):
            pass

    def update_from_rlapi(self) -> bool:
        """Read the current values from rlapi's psynet.go. True when they differ from the ones in use."""
        status, body = _request(RLAPI_SOURCE, headers={"User-Agent": "RLUtils"})
        if status != 200:
            return False
        text = body.decode("utf-8", "replace")
        new = {}
        for key, const in (("gameVersion", "gameVersion"), ("featureSet", "featureSet"), ("buildSecret", "buildSecret")):
            m = re.search(rf'^\s*{const}\s*=\s*"([A-Za-z0-9_.]+)"', text, re.M)
            if not m:
                return False
            new[key] = m.group(1)
        if new == self.v:
            return False
        self.v = new
        self.path.write_text(json.dumps(new, indent=1), encoding="utf-8")
        return True


def _auth_player(eos: dict, name: str, version: dict) -> dict:
    account = eos["account_id"]
    body = json.dumps({
        "Platform": "Epic", "PlayerName": name, "PlayerID": account, "Language": "INT",
        "AuthTicket": eos["access_token"], "BuildRegion": "", "FeatureSet": version["featureSet"], "Device": "PC",
        "LocalFirstPlayerID": f"Epic|{account}|0", "bSkipAuth": False, "bSetAsPrimaryAccount": True,
        "EpicAuthTicket": eos["access_token"], "EpicAccountID": account,
    }, separators=(",", ":")).encode()
    status, resp = _request(f"{PSY_HTTP}/Auth/AuthPlayer/v2", data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": f"RL Win/{version['gameVersion']} gzip (x86_64-pc-win32) curl-7.67.0 Schannel",
        "PsyBuildID": str(build_id(version["gameVersion"])), "PsyEnvironment": "Prod",
        "PsyRequestID": "PsyNetMessage_X_0", "PsySig": psy_sig(body), "PsyBuildSecret": version["buildSecret"]})
    if status != 200:
        raise PsyNetError(f"PsyNet sign-in: HTTP {status}")
    js = json.loads(resp)
    if js.get("Error"):
        raise PsyNetError(f"PsyNet sign-in: {js['Error'].get('Type')}: {js['Error'].get('Message')}")
    return js["Result"]


def _ws_call(auth: dict, version: dict, service: str, payload: dict) -> dict:
    import websocket  # websocket-client, already used for the Stats API

    ws = websocket.create_connection(auth["PerConURLv2"], timeout=TIMEOUT, header=[
        f"PsyBuildID: {build_id(version['gameVersion'])}", f"User-Agent: RL Win/{version['gameVersion']} gzip",
        "PsyEnvironment: Prod", f"PsyToken: {auth['PsyToken']}", f"PsySessionID: {auth['SessionID']}"])
    try:
        body = json.dumps(payload, separators=(",", ":"))
        rid = "PsyNetMessage_X_1"
        ws.send(f"PsyService: {service}\r\nPsyRequestID: {rid}\r\nPsySig: {psy_sig(body.encode())}\r\n\r\n{body}")
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            msg = ws.recv()
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "replace")
            head, sep, rest = msg.partition("\r\n\r\n")
            if not sep or f"PsyResponseID: {rid}" not in head:
                continue  # pongs and pushed messages
            js = json.loads(rest)
            if js.get("Error"):
                raise PsyNetError(f"{service}: {js['Error'].get('Type')}: {js['Error'].get('Message')}")
            return js.get("Result") or {}
        raise PsyNetError(f"{service}: no answer")
    finally:
        try:
            ws.close()
        except Exception:
            pass


@dataclass(frozen=True)
class Skill:
    playlist_id: int
    mmr: int
    tier_id: int | None
    division_id: int | None
    matches: int


def skill_to_mmr(skill: float) -> int:
    """PsyNet's "MMR" is the raw skill; the game shows skill x 20 + 100, rounded half up (45.65 -> 1013)."""
    return math.floor(skill * 20 + 100 + 0.5)


def parse_skills(result: dict) -> list[Skill]:
    out = []
    for s in result.get("Skills") or []:
        try:
            out.append(Skill(int(s["Playlist"]), skill_to_mmr(float(s["MMR"])), s.get("Tier"), s.get("Division"),
                             int(s.get("MatchesPlayed") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass
class Fetch:
    account: str
    name: str
    skills: list[Skill]
    at: datetime


def fetch_skills(store: LoginStore, account: str, version: Version) -> Fetch:
    """Sign in with the saved device auth, then read the player's skills from PsyNet. LoginExpired when the
    saved sign-in no longer works."""
    tok = _signed_in_token(store, account)
    name = tok.get("displayName") or store.load().get(account.lower(), {}).get("name", "")
    eos = _eos_token(_exchange_code(tok["access_token"]))
    try:
        auth = _auth_player(eos, name, version.v)
    except PsyNetError:
        if not version.update_from_rlapi():
            raise
        eos = _eos_token(_exchange_code(tok["access_token"]))  # the EOS token may be single use: get another
        auth = _auth_player(eos, name, version.v)
    result = _ws_call(auth, version.v, "Skills/GetPlayerSkill v1", {"PlayerID": f"Epic|{eos['account_id']}|0"})
    return Fetch(eos["account_id"].lower(), name, parse_skills(result), datetime.now(timezone.utc))


def save(fetch: Fetch, playlists, path: Path | None = None) -> tuple[str, int]:
    """Store the tracked playlists' MMR as source psynet. A reading equal to the newest stored one of that
    playlist (from any source) adds nothing. Returns (profile key, readings added)."""
    from . import data

    key = data.profile_for_epic_account(fetch.account, fetch.name, path)
    added = 0
    for s in fetch.skills:
        if s.playlist_id not in playlists or s.matches <= 0:
            continue
        prev = data.latest_snapshot(key, s.playlist_id, None, path)
        if prev and prev["rating"] == s.mmr:
            continue
        snap = data.Snapshot(s.playlist_id, s.mmr, "", "", s.tier_id, s.division_id, fetch.at, data.SOURCE_PSYNET)
        added += data.insert_snapshots(key, [snap], path)
    return key, added
