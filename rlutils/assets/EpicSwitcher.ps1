#requires -Version 5.1
# Epic Games account switcher.
# Saves each account's "Remember me" login from the Epic Games Launcher and swaps it back in,
# so switching accounts doesn't need a password or 2FA. Saved logins are encrypted with
# Windows DPAPI (only your Windows user on this PC can decrypt them).
# Command line (used by RLUtils):
#   -ListJson                       print the saved accounts as JSON: [{Id, Name, Active}]
#   -SwitchTo <id> [-WaitSignIn N]  switch the launcher to that account; with -WaitSignIn wait up to N seconds
#                                   for the launcher to sign in.
#   -AddNew                         restart the launcher on its sign-in screen (saved accounts stay saved)
#   -SaveCurrent                    save the account the launcher is signed into now
#   -StoreDir <folder>              where saved accounts live (default: "accounts" next to this script).
#                                   RLUtils ships this script inside the app and keeps them in its data folder.
#   Exit code 0 = done, anything else = failed; the last line printed says what happened.
param([switch]$NoGui, [switch]$ListJson, [string]$SwitchTo = '', [int]$WaitSignIn = 0, [switch]$AddNew,
      [switch]$SaveCurrent, [string]$StoreDir = '')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
# Print UTF-8: with no console (how RLUtils runs it) Windows PowerShell would print the OEM code page, turning
# names like 'ä' or '★' into '?'.
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

if (-not $StoreDir) { $StoreDir = Join-Path $PSScriptRoot 'accounts' }
$StatePath  = Join-Path $StoreDir 'state.json'
$EpicSaved  = Join-Path $env:LOCALAPPDATA 'EpicGamesLauncher\Saved'
$LogDir     = Join-Path $EpicSaved 'Logs'
$LogPath    = Join-Path $LogDir 'EpicGamesLauncher.log'
$LoginKeys  = @(@('RememberMe', 'Data'), @('Offline', 'Data'))
$Entropy    = [Text.Encoding]::UTF8.GetBytes('EpicSwitcher.v1')
$RocketLeagueUri = 'com.epicgames.launcher://apps/Sugar?action=launch&silent=true'

if (-not (Test-Path $StoreDir)) { New-Item -ItemType Directory -Path $StoreDir | Out-Null }

# ---------- encryption ----------

function Protect-Text([string]$Text) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $enc = [Security.Cryptography.ProtectedData]::Protect($bytes, $Entropy, 'CurrentUser')
    [Convert]::ToBase64String($enc)
}

function Unprotect-Text([string]$Blob) {
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($Blob), $Entropy, 'CurrentUser')
    [Text.Encoding]::UTF8.GetString($bytes)
}

# ---------- ini handling (line based, keeps everything else untouched) ----------

function Get-IniPath {
    $candidates = 'WindowsEditor', 'Windows' |
        ForEach-Object { Join-Path $EpicSaved "Config\$_\GameUserSettings.ini" } |
        Where-Object { Test-Path $_ }
    if (-not $candidates) { throw "Couldn't find the Epic Games Launcher settings file. Open the launcher and sign in once first." }
    $withLogin = $candidates | Where-Object { Select-String -Path $_ -Pattern '^\[RememberMe\]' -Quiet }
    if ($withLogin) { $candidates = $withLogin }
    $candidates | Sort-Object { (Get-Item $_).LastWriteTime } -Descending | Select-Object -First 1
}

function Read-Ini([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $reader = New-Object IO.StreamReader($Path, $true)
    try { $text = $reader.ReadToEnd(); $encoding = $reader.CurrentEncoding } finally { $reader.Close() }
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) -or
              ($bytes.Length -ge 2 -and (($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) -or ($bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF)))
    if (-not $hasBom -and $encoding -is [Text.UTF8Encoding]) { $encoding = New-Object Text.UTF8Encoding($false) }
    $newline = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $trailing = $text.EndsWith("`n")
    $lines = New-Object Collections.Generic.List[string]
    $body = if ($trailing) { $text.Substring(0, $text.Length - $newline.Length) } else { $text }
    if ($body.Length -gt 0) { $lines.AddRange([string[]]($body -split [regex]::Escape($newline))) }
    [pscustomobject]@{ Lines = $lines; Encoding = $encoding; Newline = $newline; Trailing = $trailing }
}

function Write-Ini([string]$Path, $Ini) {
    $text = [string]::Join($Ini.Newline, $Ini.Lines)
    if ($Ini.Trailing) { $text += $Ini.Newline }
    [IO.File]::WriteAllText($Path, $text, $Ini.Encoding)
}

function Find-IniKey($Ini, [string]$Section, [string]$Key) {
    # Returns @{ SectionLine; KeyLine; LastLine } (indexes, -1 when missing)
    $sectionLine = -1; $keyLine = -1; $lastLine = -1; $inSection = $false
    for ($i = 0; $i -lt $Ini.Lines.Count; $i++) {
        $line = $Ini.Lines[$i]
        if ($line -match '^\s*\[(.+)\]\s*$') {
            if ($inSection) { break }
            if ($matches[1] -eq $Section) { $inSection = $true; $sectionLine = $i; $lastLine = $i }
            continue
        }
        if (-not $inSection) { continue }
        if ($line.Trim() -ne '') { $lastLine = $i }
        if ($keyLine -lt 0 -and $line -match "^\s*$([regex]::Escape($Key))\s*=") { $keyLine = $i }
    }
    @{ SectionLine = $sectionLine; KeyLine = $keyLine; LastLine = $lastLine }
}

function Get-IniValue($Ini, [string]$Section, [string]$Key) {
    $pos = Find-IniKey $Ini $Section $Key
    if ($pos.KeyLine -lt 0) { return $null }
    $line = $Ini.Lines[$pos.KeyLine]
    $line.Substring($line.IndexOf('=') + 1)
}

function Set-IniValue($Ini, [string]$Section, [string]$Key, [string]$Value) {
    $pos = Find-IniKey $Ini $Section $Key
    if ($pos.KeyLine -ge 0) {
        $Ini.Lines[$pos.KeyLine] = "$Key=$Value"
    } elseif ($pos.SectionLine -ge 0) {
        $Ini.Lines.Insert($pos.LastLine + 1, "$Key=$Value")
    } else {
        if ($Ini.Lines.Count -gt 0 -and $Ini.Lines[$Ini.Lines.Count - 1].Trim() -ne '') { $Ini.Lines.Add('') }
        $Ini.Lines.Add("[$Section]")
        $Ini.Lines.Add("$Key=$Value")
    }
}

# ---------- saved accounts ----------

function Get-Profiles {
    Get-ChildItem $StoreDir -Filter '*.json' | Where-Object { $_.Name -ne 'state.json' } | ForEach-Object {
        Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    } | Sort-Object Name
}

function Get-ProfilePath([string]$Id) { Join-Path $StoreDir "$Id.json" }

function Save-Profile($ProfileObj) {
    $ProfileObj | ConvertTo-Json -Depth 5 | Set-Content -Path (Get-ProfilePath $ProfileObj.Id) -Encoding UTF8
}

function Get-State {
    if (Test-Path $StatePath) { Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json } else { [pscustomobject]@{ Current = $null; SwitchedAt = $null } }
}

function Set-State([string]$Current) {
    [pscustomobject]@{ Current = $Current; SwitchedAt = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8
}

# ---------- reading the launcher's current account ----------

function Get-LastLoginFromLog {
    # Last "OnLoginComplete" line in the current launcher log: account id + UTC timestamp.
    if (-not (Test-Path $LogPath)) { return $null }
    $hit = Select-String -Path $LogPath -Pattern '^\[(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d).*OnLoginComplete - 0 TRUE ([0-9a-f]{32})' |
        Select-Object -Last 1
    if (-not $hit) { return $null }
    $g = $hit.Matches[0].Groups
    $when = New-Object DateTime ([int]$g[1].Value), ([int]$g[2].Value), ([int]$g[3].Value), ([int]$g[4].Value), ([int]$g[5].Value), ([int]$g[6].Value), ([DateTimeKind]::Utc)
    [pscustomobject]@{ Id = $g[7].Value; When = $when }
}

function Find-DisplayName([string]$Id) {
    $files = Get-ChildItem $LogDir -Filter 'EpicGamesLauncher*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
    foreach ($f in $files) {
        $hit = Select-String -Path $f.FullName -Pattern "epicusername=`"([^`"]*)`" -epicuserid=$Id" | Select-Object -Last 1
        if ($hit) { return $hit.Matches[0].Groups[1].Value }
    }
    $null
}

function Get-CurrentLogin {
    $ini = Read-Ini (Get-IniPath)
    $values = @{}
    foreach ($k in $LoginKeys) { $values["$($k[0]).$($k[1])"] = [string](Get-IniValue $ini $k[0] $k[1]) }
    [pscustomobject]@{ Values = $values; HasLogin = ($values['RememberMe.Data'] -ne '') }
}

function Resolve-CurrentAccountId($Login) {
    # 1) The token is byte-for-byte one we saved -> it's that account.
    foreach ($p in Get-Profiles) {
        try { if ((Unprotect-Text $p.Data.'RememberMe.Data') -eq $Login.Values['RememberMe.Data']) { return $p.Id } } catch {}
    }
    # 2) Otherwise whichever is newer: the launcher log's last login, or our last switch.
    $log = Get-LastLoginFromLog
    $state = Get-State
    $switchedAt = if ($state.SwitchedAt) { [DateTime]::Parse($state.SwitchedAt).ToUniversalTime() } else { [DateTime]::MinValue }
    if ($log -and $log.When -ge $switchedAt) { return $log.Id }
    if ($state.SwitchedAt) { return $state.Current }
    if ($log) { return $log.Id }
    $null
}

# Saves whatever account the launcher is signed into right now (adds it, or refreshes its token).
function Save-CurrentAccount {
    $login = Get-CurrentLogin
    if (-not $login.HasLogin) { return $null }
    $id = Resolve-CurrentAccountId $login
    if (-not $id) { return $null }
    $path = Get-ProfilePath $id
    $existing = if (Test-Path $path) { Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
    $name = if ($existing) { $existing.Name } else { $n = Find-DisplayName $id; if ($n) { $n } else { "Account $($id.Substring(0, 6))" } }
    $data = [ordered]@{}
    foreach ($key in $login.Values.Keys) { $data[$key] = Protect-Text $login.Values[$key] }
    $profileObj = [pscustomobject]@{
        Id = $id; Name = $name; Data = [pscustomobject]$data
        Saved = (Get-Date).ToString('yyyy-MM-dd HH:mm')
        LastUsed = if ($existing) { $existing.LastUsed } else { $null }
    }
    Save-Profile $profileObj
    $profileObj
}

# ---------- launcher control ----------

function Get-LauncherExe {
    $paths = @(
        "$env:ProgramFiles\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe",
        "${env:ProgramFiles(x86)}\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe",
        "${env:ProgramFiles(x86)}\Epic Games\Launcher\Portal\Binaries\Win32\EpicGamesLauncher.exe"
    )
    $paths | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Stop-Launcher {
    $names = 'EpicGamesLauncher', 'EpicWebHelper'
    Get-Process -Name $names -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Process -Name $names -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
    if (Get-Process -Name 'EpicGamesLauncher' -ErrorAction SilentlyContinue) { throw "The Epic Games Launcher didn't close. Close it from the system tray and try again." }
    Start-Sleep -Milliseconds 500  # let it release the settings file
}

# The launcher asks Windows whether the PC is online when it starts; if Windows says no
# (e.g. a VPN or tunnel adapter is the main network), it opens on "Connection error".
function Test-WindowsInternet {
    try {
        $nlm = [Activator]::CreateInstance([Type]::GetTypeFromCLSID([Guid]'DCB00C01-570F-4A9B-8D69-199FDBA5723B'))
        [bool]$nlm.IsConnectedToInternet
    } catch { $true }
}

function Start-Launcher([switch]$PlayRocketLeague) {
    if ($PlayRocketLeague) { Start-Process $RocketLeagueUri; return }
    $exe = Get-LauncherExe
    if ($exe) { Start-Process $exe } else { Start-Process 'com.epicgames.launcher://store' }
}

function Write-LoginToIni([hashtable]$Values) {
    $iniPath = Get-IniPath
    Copy-Item $iniPath (Join-Path $StoreDir 'GameUserSettings.ini.bak') -Force
    $ini = Read-Ini $iniPath
    Set-IniValue $ini 'RememberMe' 'Enable' 'True'
    foreach ($k in $LoginKeys) { Set-IniValue $ini $k[0] $k[1] $Values["$($k[0]).$($k[1])"] }
    Write-Ini $iniPath $ini
}

function Switch-Account([string]$Id, [switch]$PlayRocketLeague) {
    $target = Get-Content (Get-ProfilePath $Id) -Raw -Encoding UTF8 | ConvertFrom-Json
    Stop-Launcher
    Save-CurrentAccount | Out-Null       # keep the outgoing account's newest token
    $target = Get-Content (Get-ProfilePath $Id) -Raw -Encoding UTF8 | ConvertFrom-Json
    $values = @{}
    foreach ($p in $target.Data.PSObject.Properties) { $values[$p.Name] = Unprotect-Text $p.Value }
    Write-LoginToIni $values
    $target.LastUsed = (Get-Date).ToString('yyyy-MM-dd HH:mm')
    Save-Profile $target
    Set-State $Id
    Start-Launcher -PlayRocketLeague:$PlayRocketLeague
}

function Start-AddAccount {
    Stop-Launcher
    Save-CurrentAccount | Out-Null
    $values = @{}
    foreach ($k in $LoginKeys) { $values["$($k[0]).$($k[1])"] = '' }
    Write-LoginToIni $values
    Set-State $null
    Start-Launcher
}

if ($ListJson) {
    $current = (Get-State).Current
    $rows = @(Get-Profiles | ForEach-Object { [pscustomobject]@{ Id = $_.Id; Name = $_.Name; Active = ($_.Id -eq $current) } })
    ConvertTo-Json -InputObject $rows -Compress
    exit 0
}

if ($SwitchTo) {
    try {
        $path = Get-ProfilePath $SwitchTo
        if (-not (Test-Path $path)) { "No saved account with id $SwitchTo."; exit 2 }
        $name = (Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json).Name
        $started = [DateTime]::UtcNow.AddSeconds(-1)
        Switch-Account $SwitchTo
        if ($WaitSignIn -gt 0) {
            $deadline = (Get-Date).AddSeconds($WaitSignIn)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Milliseconds 1000
                $login = Get-LastLoginFromLog
                if ($login -and $login.Id -eq $SwitchTo -and $login.When -ge $started) { "Signed in as $name."; exit 0 }
            }
            if (-not (Test-WindowsInternet)) {
                "The launcher didn't sign in as $name. Windows reports no internet (a VPN or tunnel adapter?), so Epic is probably on ""Connection error"": click Retry connection there."
            } else {
                "The launcher didn't sign in as $name within $WaitSignIn seconds."
            }
            exit 3
        }
        "Switched to $name."
        exit 0
    } catch {
        $_.Exception.Message
        exit 1
    }
}

if ($AddNew) {
    try {
        Start-AddAccount
        "The launcher is restarting on its sign-in screen. Sign in with ""Remember me"" ticked, then save the account."
        exit 0
    } catch {
        $_.Exception.Message
        exit 1
    }
}

if ($SaveCurrent) {
    try {
        $p = Save-CurrentAccount
        if (-not $p) { "The launcher isn't signed in with ""Remember me"" ticked, so there's nothing to save yet."; exit 4 }
        Set-State $p.Id
        "Saved $($p.Name)."
        exit 0
    } catch {
        $_.Exception.Message
        exit 1
    }
}

if ($NoGui) { return }

# ---------- GUI ----------

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object Windows.Forms.Form
$form.Text = 'Epic Account Switcher'
$form.Size = New-Object Drawing.Size(560, 430)
$form.MinimumSize = New-Object Drawing.Size(480, 360)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Segoe UI', 9.5)

$list = New-Object Windows.Forms.ListView
$list.View = 'Details'; $list.FullRowSelect = $true; $list.MultiSelect = $false; $list.HideSelection = $false
$list.Location = New-Object Drawing.Point(12, 12)
$list.Size = New-Object Drawing.Size(360, 330)
$list.Anchor = 'Top, Bottom, Left, Right'
[void]$list.Columns.Add('Account', 170)
[void]$list.Columns.Add('Last used', 120)
[void]$list.Columns.Add('', 60)
$form.Controls.Add($list)

$status = New-Object Windows.Forms.Label
$status.Location = New-Object Drawing.Point(12, 350)
$status.Size = New-Object Drawing.Size(520, 36)
$status.Anchor = 'Bottom, Left, Right'
$form.Controls.Add($status)

$script:buttonY = 12
function New-Button([string]$Text, [scriptblock]$OnClick) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $Text
    $b.Location = New-Object Drawing.Point(384, $script:buttonY)
    $b.Size = New-Object Drawing.Size(150, 34)
    $b.Anchor = 'Top, Right'
    $b.Add_Click($OnClick)
    $form.Controls.Add($b)
    $script:buttonY += 42
    $b
}

function Get-SelectedId { if ($list.SelectedItems.Count) { $list.SelectedItems[0].Tag } else { $null } }

function Refresh-List([string]$Message) {
    $selected = Get-SelectedId
    $state = Get-State
    $list.Items.Clear()
    foreach ($p in Get-Profiles) {
        $item = New-Object Windows.Forms.ListViewItem($p.Name)
        [void]$item.SubItems.Add($(if ($p.LastUsed) { $p.LastUsed } else { '-' }))
        [void]$item.SubItems.Add($(if ($p.Id -eq $state.Current) { 'active' } else { '' }))
        $item.Tag = $p.Id
        if ($p.Id -eq $state.Current) { $item.Font = New-Object Drawing.Font($list.Font, [Drawing.FontStyle]::Bold) }
        [void]$list.Items.Add($item)
        if ($p.Id -eq $selected) { $item.Selected = $true }
    }
    $status.Text = if ($Message) { $Message } elseif ($list.Items.Count -eq 0) {
        'No accounts saved yet. Sign into the launcher with "Remember me" ticked, then click "Save current account".'
    } else { 'Double-click an account to switch to it.' }
}

function Invoke-Safely([scriptblock]$Action) {
    $form.Cursor = 'WaitCursor'; $form.Enabled = $false
    try { & $Action } catch { [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Epic Account Switcher', 'OK', 'Error') }
    finally { $form.Enabled = $true; $form.Cursor = 'Default' }
}

function Confirm-GameClosed {
    if (Get-Process -Name 'RocketLeague' -ErrorAction SilentlyContinue) {
        $r = [Windows.Forms.MessageBox]::Show("Rocket League is still running. Switching restarts the Epic launcher, so close the game first.`n`nSwitch anyway?", 'Epic Account Switcher', 'YesNo', 'Warning')
        return $r -eq 'Yes'
    }
    $true
}

function Do-Switch([switch]$Play) {
    $id = Get-SelectedId
    if (-not $id) { $status.Text = 'Pick an account first.'; return }
    if (-not (Confirm-GameClosed)) { return }
    Invoke-Safely {
        $status.Text = 'Switching...'; $form.Refresh()
        Switch-Account $id -PlayRocketLeague:$Play
        $name = $list.SelectedItems[0].Text
        $msg = if ($Play) { "Switched to $name. Rocket League will start once the launcher signs in." } else { "Switched to $name. The launcher is starting." }
        if (-not (Test-WindowsInternet)) {
            $msg += ' Windows reports no internet (a VPN or tunnel adapter?), so Epic will open on "Connection error": click Retry connection.'
        }
        Refresh-List $msg
    }
}

New-Button 'Switch' { Do-Switch } | Out-Null
New-Button 'Switch + Rocket League' { Do-Switch -Play } | Out-Null
New-Button 'Save current account' {
    Invoke-Safely {
        $p = Save-CurrentAccount
        if ($p) {
            Set-State $p.Id
            Refresh-List "Saved $($p.Name)."
        } else {
            Refresh-List 'The launcher is not signed in with "Remember me" ticked, so there is nothing to save yet.'
        }
    }
} | Out-Null
New-Button 'Add new account' {
    if (-not (Confirm-GameClosed)) { return }
    $msg = "The launcher will restart on the sign-in screen.`n`n1. Sign in with the new account (tick ""Remember me"").`n2. Come back here and click ""Save current account"".`n`nYour other saved accounts stay saved. Continue?"
    if ([Windows.Forms.MessageBox]::Show($msg, 'Add new account', 'OKCancel', 'Information') -ne 'OK') { return }
    Invoke-Safely { Start-AddAccount; Refresh-List 'Sign in on the launcher, then click "Save current account".' }
} | Out-Null
New-Button 'Rename' {
    $id = Get-SelectedId
    if (-not $id) { $status.Text = 'Pick an account first.'; return }
    Add-Type -AssemblyName Microsoft.VisualBasic
    $p = Get-Content (Get-ProfilePath $id) -Raw -Encoding UTF8 | ConvertFrom-Json
    $new = [Microsoft.VisualBasic.Interaction]::InputBox('New name:', 'Rename account', $p.Name)
    if ($new.Trim()) { $p.Name = $new.Trim(); Save-Profile $p; Refresh-List "Renamed to $($p.Name)." }
} | Out-Null
New-Button 'Remove' {
    $id = Get-SelectedId
    if (-not $id) { $status.Text = 'Pick an account first.'; return }
    $name = $list.SelectedItems[0].Text
    if ([Windows.Forms.MessageBox]::Show("Remove $name from the switcher? You'd need to sign in with 2FA again to re-add it. (This doesn't touch the Epic account itself.)", 'Remove account', 'YesNo', 'Question') -eq 'Yes') {
        Remove-Item (Get-ProfilePath $id) -Force
        Refresh-List "Removed $name."
    }
} | Out-Null

$list.Add_DoubleClick({ Do-Switch })
$form.Add_Shown({ Refresh-List })
[void]$form.ShowDialog()
