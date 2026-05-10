<#
.SYNOPSIS
    Wrapper invoked by Windows Task Scheduler for the Agent-Trading monitor.

.DESCRIPTION
    Activates the project .venv and runs `python monitor.py`. The monitor
    evaluates kill conditions and may flatten a position via the broker; it
    cannot open new positions.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

if (Test-Path -LiteralPath (Join-Path $RepoRoot "state\halt.flag")) {
    Write-Host "halt.flag present — monitor exiting cleanly."
    exit 0
}

$Venv = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path -LiteralPath $Venv)) {
    Write-Error "No .venv found at $Venv. Run 'py -3.11 -m venv .venv' from the repo root first."
}

. $Venv

python monitor.py
exit $LASTEXITCODE
