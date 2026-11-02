$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$taskPython = Get-Command python -ErrorAction SilentlyContinue
if ($taskPython) {
    & $taskPython.Source -m reconcile
} else {
    Write-Error 'Install Python 3.12 or later, run python -m pip install -e . --no-deps, then python -m reconcile.'
}
