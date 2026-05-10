<#
.SYNOPSIS
    Removes the Agent-Trading orchestrator + monitor scheduled tasks.

.EXAMPLE
    PS> .\scheduling\unregister_task.ps1
    PS> .\scheduling\unregister_task.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$TaskFolder = "\Agent-Trading\"

foreach ($TaskName in @("Orchestrator", "Monitor")) {
    $Existing = Get-ScheduledTask -TaskPath $TaskFolder -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Existing) {
        Write-Host "Skip $TaskFolder$TaskName (not registered)."
        continue
    }
    if ($PSCmdlet.ShouldProcess("$TaskFolder$TaskName", "Unregister-ScheduledTask")) {
        Unregister-ScheduledTask -TaskPath $TaskFolder -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered $TaskFolder$TaskName"
    }
}

# Clean up the now-empty folder if no other tasks live there.
$Remaining = Get-ScheduledTask -TaskPath $TaskFolder -ErrorAction SilentlyContinue
if ($null -eq $Remaining) {
    try {
        $Scheduler = New-Object -ComObject "Schedule.Service"
        $Scheduler.Connect()
        $Root = $Scheduler.GetFolder("\")
        $Root.DeleteFolder($TaskFolder.Trim('\'), 0)
        Write-Host "Removed task folder $TaskFolder"
    } catch {
        Write-Verbose "Folder removal skipped: $_"
    }
}
