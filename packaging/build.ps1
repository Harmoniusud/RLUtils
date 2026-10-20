# Build dist\RLUtils\RLUtils.exe with PyInstaller.  Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python packaging\make_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon build failed" }

python -m PyInstaller --noconfirm --clean --windowed --name RLUtils `
    --icon "$root\packaging\icon.ico" `
    --add-data "$root\rlutils\assets;rlutils\assets" `
    --add-data "$root\rlutils\web;rlutils\web" `
    --exclude-module PySide6.QtPdf --exclude-module PySide6.QtPdfWidgets `
    --exclude-module matplotlib --exclude-module tkinter `
    --distpath dist --workpath build\pyinstaller --specpath build `
    rlutils.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Trim what the app never uses (about 70 MB): the window is English, and it has no QML of its own (Qt WebEngine
# needs the Qt Quick DLLs, which stay, but not the QML modules).
$pyside = "$root\dist\RLUtils\_internal\PySide6"
Get-ChildItem "$pyside\translations\qtwebengine_locales" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne "en-US.pak" } | Remove-Item -Force
Get-ChildItem "$pyside\translations" -File -Filter *.qm -ErrorAction SilentlyContinue | Remove-Item -Force
if (Test-Path "$pyside\qml") { Remove-Item "$pyside\qml" -Recurse -Force }
Write-Host "Built $root\dist\RLUtils\RLUtils.exe"
