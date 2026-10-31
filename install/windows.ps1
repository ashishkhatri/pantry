<#
  Install or update Pantry on Windows. In PowerShell (no admin needed):

    irm https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/windows.ps1 | iex

  With options:

    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/windows.ps1))) -StartAtLogin -Lan

  -StartAtLogin  run Pantry in the background whenever you sign in to Windows
  -Lan           listen on your network (http://<this-pc>:8790), not only on this PC
  -Port 8790     port to use
  -Version v0.1.0  install a specific release (default: latest)
  -Uninstall     remove Pantry (your data folder is kept unless you add -RemoveData)

  Installs to %LOCALAPPDATA%\Pantry. Your data is in %LOCALAPPDATA%\Pantry\data and is kept on update.
#>
param(
  [switch]$StartAtLogin,
  [switch]$Lan,
  [int]$Port = 8790,
  [string]$Version = "",
  [switch]$Uninstall,
  [switch]$RemoveData
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "ashishkhatri/pantry"
$Dest = Join-Path $env:LOCALAPPDATA "Pantry"
$AppDir = Join-Path $Dest "app"
$DataDir = Join-Path $Dest "data"
$Venv = Join-Path $Dest ".venv"
$Launcher = Join-Path $Dest "run.pyw"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Pantry.lnk"
$StartupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Pantry.lnk"

function Say($m) { Write-Host "== $m" -ForegroundColor Green }
function Die($m) { Write-Host "!! $m" -ForegroundColor Red; throw $m }

function Stop-Pantry {
  Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Launcher*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
  Say "Removing Pantry"
  Stop-Pantry
  Remove-Item $StartMenu, $StartupLnk -ErrorAction SilentlyContinue
  Remove-Item $AppDir, $Venv, $Launcher -Recurse -Force -ErrorAction SilentlyContinue
  if ($RemoveData) { Remove-Item $Dest -Recurse -Force -ErrorAction SilentlyContinue; Say "Removed everything, including your data." }
  else { Say "Removed. Your data is still in $DataDir (delete it yourself, or run again with -RemoveData)." }
  return
}

# ---------- Python 3.10+ ----------
function Find-Python {
  $candidates = @(
    @("py", "-3"), @("python", ""),
    @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"), ""),
    @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"), "")
  )
  foreach ($c in $candidates) {
    try {
      $exeArgs = @(); if ($c[1]) { $exeArgs += $c[1] }
      $out = & $c[0] @exeArgs -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $out) {
        $ver = [version]($out[0]); $exe = $out[1]
        # Skip the Microsoft Store stub, which prints nothing useful.
        if ($ver -ge [version]"3.10" -and (Test-Path $exe)) { return $exe }
      }
    } catch { }
  }
  return $null
}

Say "Checking for Python"
$Py = Find-Python
if (-not $Py) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Die "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
  }
  Say "Installing Python 3.12 (winget)"
  winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
  $Py = Find-Python
  if (-not $Py) { Die "Python was installed but can't be found yet. Close this window, open a new PowerShell and run the command again." }
}
Say "Using $Py"

# ---------- download ----------
if (-not $Version) {
  try { $Version = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "pantry-installer" }).tag_name } catch { $Version = "" }
}
if ($Version) { $Url = "https://github.com/$Repo/archive/refs/tags/$Version.zip"; Say "Downloading Pantry $Version" }
else { $Url = "https://github.com/$Repo/archive/refs/heads/main.zip"; Say "No release yet; downloading the main branch" }

$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("pantry-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
  $Zip = Join-Path $Tmp "pantry.zip"
  Invoke-WebRequest $Url -OutFile $Zip -UseBasicParsing
  Expand-Archive $Zip -DestinationPath $Tmp
  $Src = Get-ChildItem $Tmp -Directory | Select-Object -First 1
  if (-not (Test-Path (Join-Path $Src.FullName "pantry\__main__.py"))) { Die "The download doesn't look like Pantry." }

  Stop-Pantry
  New-Item -ItemType Directory -Path $Dest, $DataDir -Force | Out-Null
  if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
  Move-Item $Src.FullName $AppDir
} finally {
  Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---------- Python environment ----------
Say "Setting up Python packages"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & $Py -m venv $Venv; if ($LASTEXITCODE) { Die "Couldn't create the Python environment." } }
& (Join-Path $Venv "Scripts\python.exe") -m pip install -q --disable-pip-version-check -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE) { Die "Installing packages failed. Check your internet connection and try again." }

# ---------- launcher (no console window; output goes to data\pantry.log) ----------
$HostAddr = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
@"
# Started by the Pantry shortcuts. Runs without a console window; see data\pantry.log.
import os, sys
os.environ.setdefault("PANTRY_DATA_DIR", r"$DataDir")
os.environ.setdefault("PANTRY_HOST", "$HostAddr")
os.environ.setdefault("PANTRY_PORT", "$Port")
log = open(os.path.join(os.environ["PANTRY_DATA_DIR"], "pantry.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log
sys.path.insert(0, r"$AppDir")
from pantry.__main__ import main
main()
"@ | Set-Content -Path $Launcher -Encoding UTF8

$PyW = Join-Path $Venv "Scripts\pythonw.exe"
$Icon = Join-Path $AppDir "pantry\static\icons\favicon.ico"
function New-Shortcut($Path, $Arguments) {
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
  $s.TargetPath = $PyW
  $s.Arguments = "`"$Launcher`" $Arguments"
  $s.WorkingDirectory = $Dest
  if (Test-Path $Icon) { $s.IconLocation = $Icon }
  $s.Description = "Pantry"
  $s.Save()
}
New-Shortcut $StartMenu "--browser"
if ($StartAtLogin) { New-Shortcut $StartupLnk ""; Say "Pantry will start when you sign in." }
elseif (Test-Path $StartupLnk) { New-Shortcut $StartupLnk "" }   # keep an existing start-at-login on update

Say "Starting Pantry"
Start-Process -FilePath $PyW -ArgumentList "`"$Launcher`" --browser" -WorkingDirectory $Dest

Write-Host ""
Say "Pantry $Version is installed. Open it any time from the Start menu (Pantry)."
Write-Host "   Address: http://127.0.0.1:$Port"
if ($Lan) {
  Write-Host "   Other devices: http://$($env:COMPUTERNAME):$Port. Windows Firewall may ask to allow Python; allow it on private networks."
}
Write-Host "   Phones need https for the camera and notifications; see the README (reverse proxy or Tailscale)."
Write-Host "   Data: $DataDir"
Write-Host "   Update: run the same install command again."
