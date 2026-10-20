# Make the shareable zip of a built app. Run after build.ps1:
#   powershell -ExecutionPolicy Bypass -File packaging\release.ps1
# Writes dist\RLUtils-<version>.zip with only: the built app (dist\RLUtils), README.txt (packaging\README-user.txt),
# THIRD_PARTY_NOTICES.txt, the licenses folder, and LICENSE.txt if the project has one. Nothing from data\,
# tools\, HANDOFF*.md or other dev folders can end up in it. (Up to 0.10 this also wrote an update feed,
# latest.json; the app doesn't read one any more.)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (python -c "import sys; sys.path.insert(0, r'$root'); import rlutils; print(rlutils.__version__)").Trim()
$src = Join-Path $root "dist\RLUtils"
if (-not (Test-Path (Join-Path $src "RLUtils.exe"))) { throw "Run packaging\build.ps1 first." }
$built = (Get-Item (Join-Path $src "RLUtils.exe")).LastWriteTime
$newest = Get-ChildItem (Join-Path $root "rlutils") -Recurse -File | Where-Object { $_.Extension -ne ".pyc" } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newest.LastWriteTime -gt $built) { throw "$($newest.Name) changed after the last build. Run packaging\build.ps1 again." }

$stage = Join-Path $root "build\release\RLUtils-$version"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item $src (Join-Path $stage "RLUtils") -Recurse
Copy-Item (Join-Path $PSScriptRoot "README-user.txt") (Join-Path $stage "README.txt")
Copy-Item (Join-Path $PSScriptRoot "THIRD_PARTY_NOTICES.txt") $stage
Copy-Item (Join-Path $PSScriptRoot "licenses") (Join-Path $stage "licenses") -Recurse
if (Test-Path (Join-Path $root "LICENSE.txt")) { Copy-Item (Join-Path $root "LICENSE.txt") $stage }
else { Write-Warning "No LICENSE.txt in the project yet: the zip has none for RLUtils itself." }

# Last check: nothing personal (data folder files, the saved-accounts store, keys, logs).
$bad = Get-ChildItem $stage -Recurse -File | Where-Object {
    $_.Name -match '^(tracker\.db|config\.json|rllog_state\.json|trackergg_session\.json|.*key.*\.txt|Launch.*\.log|state\.json)$' -or
    $_.FullName -match '\\(epic-accounts|edge-trackergg|accounts)\\' }
if ($bad) { throw "Personal files in the release: $($bad.FullName -join ', ')" }

$zip = Join-Path $root "dist\RLUtils-$version.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip
Write-Host "Wrote $zip"
Write-Host "  SHA-256 $((Get-FileHash $zip -Algorithm SHA256).Hash.ToLower())"

# The installer (Inno Setup 6: winget install JRSoftware.InnoSetup)
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Warning "Inno Setup not found: no installer built (the zip is ready)."; return }
& $iscc /Q "/DAppVersion=$version" (Join-Path $PSScriptRoot "RLUtils.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$setup = Join-Path $root "dist\RLUtils-Setup-$version.exe"
Write-Host "Wrote $setup"
Write-Host "  SHA-256 $((Get-FileHash $setup -Algorithm SHA256).Hash.ToLower())"
