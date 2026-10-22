RLUtils
=======

A small Windows app for Rocket League players:
  - your ranked MMR over time, straight from Rocket League's servers (needs a one-time Epic sign-in)
  - every match you finish, with everyone's stats, from the game's official Stats API
  - Play: start Rocket League (and, if you like, switch between your Epic accounts first)

Start RLUtils.exe. A short setup has you sign in to Epic in your browser, finds the game, and offers to switch
on the game's Stats API.

What it reads and changes
  - Your MMR: you sign in on Epic's own website in your browser, so RLUtils never sees your password. It keeps
    Epic's sign-in for this PC, encrypted with Windows (DPAPI) so only your Windows user on this PC can read it,
    and only sends it to Epic and Rocket League's servers to read your MMR. It uses the same unofficial API as
    the game itself (github.com/dank/rlapi), never while the game runs: a session's MMR shows when you close
    the game. Settings > Epic sign-in signs you out.
  - Listens to the game's Stats API on this PC (127.0.0.1). It never sends anything to the game.
  - Changes TAStatsAPI.ini only when you click "Enable Stats API" (a backup is kept).
  - The optional account switcher swaps the Epic launcher's saved login between accounts you saved. Saved
    logins are encrypted with Windows (DPAPI) so only your Windows user on this PC can read them.
  - Once a day it asks GitHub whether a newer RLUtils is out (Settings > Check for updates). It only shows a
    link; it never downloads or installs anything by itself.
  - The game itself is never touched, so Easy Anti-Cheat isn't affected.
  - Your data stays on this PC (in %LOCALAPPDATA%\RLUtils). RLUtils has no server of its own.

Not affiliated with Psyonix, Epic Games or Tracker Network. Rocket League is a trademark of Psyonix LLC.
Epic Games is a trademark of Epic Games, Inc. Use at your own risk.

Third-party software and licenses: see THIRD_PARTY_NOTICES.txt.
