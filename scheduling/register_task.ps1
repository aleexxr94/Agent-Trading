<#
.SYNOPSIS
    Registers (or updates) the Agent-Trading orchestrator + monitor scheduled
    tasks from the bundled XML definitions.

.DESCRIPTION
    Reads scheduling\orchestrator_task.xml and scheduling\monitor_task.xml,
    substitutes the {{REPO_ROOT}}, {{USER_SID}}, and {{USER_UPN}} placeholders
    with values from the current environment, and registers the tasks under
    the \Agent-Trading\ folder. Re-running this script updates an existing
    registration in place.

    Validates that .venv exists before registering (the task wrappers fail
    closed if the venv is missing, but it's friendlier to catch it here).

    Run from an elevated or non-elevated PowerShell session — the tasks run
    under the current user's interactive token at LeastPrivilege.

.EXAMPLE
    PS> .\scheduling\register_task.ps1
    PS> .\scheduling\register_task.ps1 -SkipMonitor       # orchestrator only
    PS> .\scheduling\register_task.ps1 -WhatIf            # dry-run
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$SkipMonitor,
    [switch]$SkipOrchestrator
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvScript = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$TaskFolder = "\Agent-Trading\"

if (-not (Test-Path -LiteralPath $VenvScript)) {
    Write-Error @"
No virtualenv found at $VenvScript.
Create it first from the repo root:
    py -3.11 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
"@
}

$User    = [Security.Principal.WindowsIdentity]::GetCurrent()
$UserSid = $User.User.Value
$UserUpn = $User.Name

function Register-FromXml {
    param(
        [Parameter(Mandatory)] [string] $TaskName,
        [Parameter(Mandatory)] [string] $XmlPath
    )

    if (-not (Test-Path -LiteralPath $XmlPath)) {
        Write-Error "Task XML not found: $XmlPath"
    }

    $Xml = Get-Content -LiteralPath $XmlPath -Raw
    $Xml = $Xml.Replace("{{REPO_ROOT}}", $RepoRoot).Replace("{{USER_SID}}", $UserSid).Replace("{{USER_UPN}}", $UserUpn)

    if ($PSCmdlet.ShouldProcess("$TaskFolder$TaskName", "Register-ScheduledTask")) {
        # Register-ScheduledTask -Xml takes a .NET string; on-disk encoding
        # is irrelevant once it's loaded into memory.
        Register-ScheduledTask `
            -TaskPath  $TaskFolder `
            -TaskName  $TaskName `
            -Xml       $Xml `
            -User      $UserUpn `
            -Force | Out-Null
        Write-Host "Registered $TaskFolder$TaskName"
    }
}

if (-not $SkipOrchestrator) {
    Register-FromXml -TaskName "Orchestrator" -XmlPath (Join-Path $RepoRoot "scheduling\orchestrator_task.xml")
}

if (-not $SkipMonitor) {
    Register-FromXml -TaskName "Monitor" -XmlPath (Join-Path $RepoRoot "scheduling\monitor_task.xml")
}

Write-Host ""
Write-Host "Done. Inspect with:"
Write-Host "  Get-ScheduledTask -TaskPath '$TaskFolder'"
Write-Host "  Get-ScheduledTaskInfo -TaskPath '$TaskFolder' -TaskName 'Orchestrator'"
Write-Host ""
Write-Host "Trigger the orchestrator on demand with:"
Write-Host "  Start-ScheduledTask -TaskPath '$TaskFolder' -TaskName 'Orchestrator'"
