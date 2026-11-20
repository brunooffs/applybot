# applybot installer for Windows (PowerShell 5.1 or newer).
#
#   irm https://raw.githubusercontent.com/brunooffs/applybot/main/install.ps1 | iex
#
# It installs uv (a small, widely used Python tool installer) if you don't have it, installs
# applybot with it (uv fetches a suitable Python by itself), creates your private folder
# (%USERPROFILE%\.applybot) and runs `applybot doctor`. It does NOT install Ollama or download
# any model: doctor tells you what fits your computer and the exact command to run.
# Read it first if you like; it is short. Re-running it updates applybot and keeps your files.
$ErrorActionPreference = 'Stop'

$Src = if ($env:APPLYBOT_SRC) { $env:APPLYBOT_SRC } else { 'git+https://github.com/brunooffs/applybot' }

function Say($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'uv is not installed. It is the tool that installs applybot and the right Python for it.'
    $ans = Read-Host 'Install uv now from https://astral.sh/uv ? [Y/n]'
    if ($ans -match '^[nN]') { Write-Host 'Stopped. Install uv (https://docs.astral.sh/uv/) and run this again.'; return }
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host 'uv was installed but is not on PATH yet. Open a new PowerShell window and run this again.'; return
    }
}

Say 'Installing applybot'
uv tool install --force --python '>=3.10' $Src
if ($LASTEXITCODE -ne 0) { Write-Host 'The install failed. See the message above.'; return }

$bin = (uv tool dir --bin).Trim()
$env:Path = "$bin;$env:Path"

Say 'Creating your applybot folder'
applybot init

Say 'Checking this computer'
applybot doctor

Write-Host "`nDone. If a new window says applybot is not recognised, run:  uv tool update-shell"
Write-Host 'Then open a new PowerShell window and start with:  applybot serve --open'
