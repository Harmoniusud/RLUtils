RLUtils
=======

A small Windows app for Rocket League players:
  - your ranked MMR over time: live from Rocket League's servers after a one-time Epic sign-in, and from the
    game's own log each time you queue
  - every match you finish, with everyone's stats, from the game's official Stats API
  - Play: start Rocket League (and, if you like, switch between your Epic accounts first)

Start RLUtils.exe. A short setup asks for your Epic name, offers the Epic sign-in for live MMR, finds the game,
and offers to switch on the game's Stats API.

What it reads and changes
  - Reads Rocket League's log files (Documents\My Games\Rocket League\TAGame\Logs) for your MMR. Only plain
    text files the game writes; the game itself is never touched, so Easy Anti-Cheat isn't affected.
  - Listens to the game's Stats API on this PC (127.0.0.1). It never sends anything to the game.
  - Changes TAStatsAPI.ini only when you click "Enable Stats API" (a backup is kept).
  - The optional account switcher swaps the Epic launcher's saved login between accounts you saved. Saved
    logins are encrypted with Windows (DPAPI) so only your Windows user on this PC can read them.
  - Live MMR (optional): you sign in on Epic's own page, so RLUtils never sees your password. Epic's sign-in
    token is kept encrypted the same way and is only sent to Epic and Rocket League's servers to read your MMR.
    It uses the same unofficial API as the game itself (github.com/dank/rlapi), and never while the game runs.
  - Once a day it asks GitHub whether a newer RLUtils is out (Settings > Check for updates). It only shows a
    link; it never downloads or installs anything by itself.
  - Your data stays on this PC (in %LOCALAPPDATA%\RLUtils). RLUtils has no server of its own.

Not affiliated with Psyonix, Epic Games or Tracker Network. Rocket League is a trademark of Psyonix LLC.
Epic Games is a trademark of Epic Games, Inc. Use at your own risk.

Third-party software and licenses: see THIRD_PARTY_NOTICES.txt.
