# RLUtils

A small Windows app for Rocket League players.

- **MMR over time**: your ranked MMR (1v1, 2v2, 3v3), live from Rocket League's servers after a one-time Epic
  sign-in and from the game's own log every time you queue, with rank bands, a division gauge and history you
  can import once from tracker.gg.
- **Games**: every match you finish, with everyone's stats, your playstyle, your teammates, and what your
  wins and losses have in common. From the game's official Stats API.
- **Play**: starts Rocket League (Epic or Steam, or any shortcut you pick), optionally switching the Epic
  launcher to one of your saved accounts first.
- **Vibrance**: NVIDIA Digital Vibrance while Rocket League is in front, optionally mirrored into OBS.

## Download

Get `RLUtils-Setup-<version>.exe` (installer) or `RLUtils-<version>.zip` (portable) from
[Releases](../../releases). Windows 10/11, 64-bit. On first start a short setup asks for your Epic name,
offers the Epic sign-in for live MMR, finds the game, and offers to switch on Rocket League's Stats API.
The app tells you when a newer version is out (Settings > Check for updates); install it over the old one,
your data stays.

## What it reads and changes

- **Live MMR** (optional): you sign in once on Epic's own login page, shown in an app window, so RLUtils never
  sees your password. It keeps Epic's sign-in token, encrypted with Windows DPAPI so only your Windows user on
  this PC can read it, and sends it only to Epic and Rocket League's servers to read your MMR for every ranked
  playlist. This uses Rocket League's unofficial API the way [dank/rlapi](https://github.com/dank/rlapi) does
  (not made or endorsed by Psyonix or Epic). It is never used while the game runs. Settings > Live MMR signs
  you out and deletes the token.
- **MMR from the log** comes from `Documents\My Games\Rocket League\TAGame\Logs\Launch.log`: on every queue the
  game writes your skill (`Matchmaking: Pre-divide PartyLeaderMMR: …`), and MMR = skill × 20 + 100. The number
  is the party leader's, so a reading is saved only when you queue alone or lead the party.
- **Games** come from the [Stats API](https://www.rocketleague.com/developer/stats-api), which the game serves
  on 127.0.0.1 once `PacketSendRate` is above 0. RLUtils only listens.
- **Enable Stats API** (setup or Settings) sets `PacketSendRate=10` in the game folder's
  `TAGame\Config\DefaultStatsAPI.ini` (or `TAStatsAPI.ini`) and in the Documents copy, with backups, only
  while the game is closed. Nothing else in the game folder is touched.
- **Account switcher** (optional): saves the Epic launcher's "Remember me" sign-in per account, encrypted with
  Windows DPAPI for your Windows user, and swaps it in before starting the game. It never sees passwords.
- **Import history** (optional, once): you open tracker.gg pages in your own browser and paste them in. The app
  itself never contacts tracker.gg.
- **Update check**: once a day the app asks GitHub for the newest release's version and shows a link if it's
  newer. Nothing is downloaded or installed by the app.
- The game process is never touched (no injection, no memory reading), so Easy Anti-Cheat isn't involved.
  Your data stays in `%LOCALAPPDATA%\RLUtils`; RLUtils has no server of its own.

## Build from source

Windows, Python 3.10 or newer (built and tested with 3.14).

```
pip install -r requirements.txt pyinstaller
python rlutils.py                                                  # run from source
powershell -ExecutionPolicy Bypass -File packaging\build.ps1       # dist\RLUtils\RLUtils.exe
powershell -ExecutionPolicy Bypass -File packaging\release.ps1     # dist\RLUtils-<v>.zip (+ installer with Inno Setup 6)
```

`RLUTILS_DATA=<folder>` runs it on another data folder.

| Path | What |
|---|---|
| `rlutils/app.py` | the window (Qt WebEngine page) and its actions; log, live MMR, Stats API, vibrance and update watchers; setup, sign-in and import panels |
| `rlutils/web/` | the UI: `index.html` and `app.js` |
| `rlutils/psynet.py` | live MMR from Rocket League's servers: Epic sign-in, token store, PsyNet requests |
| `rlutils/rllog.py` | MMR from the game's log: parser (party/leader aware), tailer (rotation, partial lines, saved state) |
| `rlutils/stats.py` | Stats API feed, reader, playstyle and win/loss analysis; the Stats API setting |
| `rlutils/importer.py` | checks a pasted tracker.gg history or profile |
| `rlutils/game.py` | finding and starting the game; the account switcher |
| `rlutils/data.py` | settings and the SQLite database |
| `rlutils/vibrance.py` | NVIDIA vibrance (NVAPI) and the OBS filter |
| `rlutils/assets/` | the account switcher script, rank table, icons |
| `packaging/` | build, release, installer script, notices and third-party licenses |

## License

MIT, see [LICENSE.txt](LICENSE.txt). Third-party components (Qt/PySide6 under LGPL-3.0, Python, websocket-client)
are listed in [packaging/THIRD_PARTY_NOTICES.txt](packaging/THIRD_PARTY_NOTICES.txt).

Not affiliated with Psyonix, Epic Games or Tracker Network. Rocket League is a trademark of Psyonix LLC.
