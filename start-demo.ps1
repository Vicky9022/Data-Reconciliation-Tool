$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Get-Command python -ErrorAction SilentlyContinue
if ($taskPython) {
    & $taskPython.Source run.py
} else {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) { & $bundledPython run.py }
    else { Write-Error 'Install Python 3.12 or later, then run python run.py from this folder.' }
}
