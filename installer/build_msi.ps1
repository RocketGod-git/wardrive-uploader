# Build the Wardrive Uploader MSI.
# 1) rebuilds the self-contained exe (PyInstaller), 2) wraps it in an MSI via WiX v3.
# Requires WiX v3 (candle.exe/light.exe) either on PATH, in %WIX%\bin, or passed via -WixDir.
param([string]$WixDir = "")

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent            # repo root (this script lives in installer/)

Write-Host "== building exe =="
& "$root\.venv\Scripts\python.exe" -m PyInstaller --noconfirm "$root\wdgwars_uploader.spec"
$exe = Join-Path $root "dist\WardriveUploader.exe"
if (-not (Test-Path $exe)) { throw "exe not found at $exe" }

# locate WiX v3
if (-not $WixDir) {
    $c = Get-Command candle.exe -ErrorAction SilentlyContinue
    if ($c) { $WixDir = Split-Path $c.Source }
    elseif ($env:WIX) { $WixDir = Join-Path $env:WIX "bin" }
}
$candle = Join-Path $WixDir "candle.exe"
$light  = Join-Path $WixDir "light.exe"
if (-not (Test-Path $candle)) { throw "WiX v3 candle.exe not found (pass -WixDir or install WiX v3)" }

# Version comes from wdgwars_uploader/__init__.py so the exe, the MSI
# ProductVersion and this file name can never disagree.
$verSrc = Get-Content "$root\wdgwars_uploader\__init__.py" -Raw
$ver = [regex]::Match($verSrc, '__version__\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $ver) { throw "could not read __version__ from wdgwars_uploader/__init__.py" }
Write-Host "== version $ver =="
$obj = Join-Path $root "dist\Product.wixobj"
$msi = Join-Path $root "dist\WardriveUploader-$ver.msi"
Write-Host "== candle =="
& $candle -nologo -arch x64 -ext WixFirewallExtension -dExeSrc="$exe" -dProductVersion="$ver" -out $obj (Join-Path $PSScriptRoot "Product.wxs")
Write-Host "== light =="
& $light -nologo -sval -ext WixFirewallExtension -out $msi $obj
Write-Host "Built: $msi"
