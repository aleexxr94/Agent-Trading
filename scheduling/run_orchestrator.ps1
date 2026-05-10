<#
.SYNOPSIS
    Wrapper invoked by Windows Task Scheduler for the Agent-Trading orchestrator.

.DESCRIPTION
    Activates the project .venv, runs `python orchestrator.py`, and rewrites
    the next-run trigger of the \Agent-Trading\Orchestrator task from
    state/next_run.json so the orchestrator controls its own cadence.

    Halt safety: if state/halt.flag is present, this wrapper exits 0 without
    invoking the Python orchestrator (which would also abort, but exiting
    early avoids an unnecessary process spawn).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

if (Test-Path -LiteralPath (Join-Path $RepoRoot "state\halt.flag")) {
    Write-Host "halt.flag present at $RepoRoot\state\halt.flag — refusing to run."
    exit 0
}

$Venv = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path -LiteralPath $Venv)) {
    Write-Error "No .venv found at $Venv. Run 'py -3.11 -m venv .venv' from the repo root first."
}

. $Venv

# Run the orchestrator. We intentionally do NOT pass --dry-run here.
python orchestrator.py
$ExitCode = $LASTEXITCODE

# Re-schedule from state/next_run.json regardless of exit code, so a transient
# failure does not lose the recurring trigger. If next_run.json is missing,
# the original daily fallback trigger remains in place.
$NextRunFile = Join-Path $RepoRoot "state\next_run.json"
if (Test-Path -LiteralPath $NextRunFile) {
    try {
        $NextRun = Get-Content -LiteralPath $NextRunFile -Raw | ConvertFrom-Json
        $NextAt = [DateTime]::Parse($NextRun.next_run_at).ToUniversalTime()
        $Trigger = New-ScheduledTaskTrigger -Once -At $NextAt
        Set-ScheduledTask -TaskPath "\Agent-Trading\" -TaskName "Orchestrator" -Trigger $Trigger | Out-Null
        Write-Host "Next run scheduled at $NextAt UTC (run_id=$($NextRun.run_id))."
    } catch {
        Write-Warning "Failed to update next-run trigger: $_"
    }
}

exit $ExitCode
