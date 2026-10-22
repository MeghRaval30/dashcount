# Registers a Windows scheduled task that runs DashCount every US trading weekday.
# Run once, yourself, in PowerShell from the project folder:
#     powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
# Remove it with:
#     Unregister-ScheduledTask -TaskName "DashCount trading day" -Confirm:$false
#
# Timing: the task starts at 18:25 India time (= 08:55 New York in summer, 07:55 in
# winter). The bot itself waits for 09:20 New York time to build the level map,
# skips US holidays, and trades until the close.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -ErrorAction Stop).Source

$action = New-ScheduledTaskAction -Execute $uv -Argument "run dashcount run-day" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "18:25"
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "DashCount trading day" -Action $action -Trigger $trigger -Settings $settings `
    -Description "DashCount: level map at 09:20 ET, trade the US session on Alpaca paper, Guardian kill switches." -Force

Write-Host "Scheduled. Keep the laptop plugged in, lid open, GPU in Standard/Ultimate mode."
