# install-mail-task.ps1 - register/query/remove the per-instance hq mail tick task.
#
# One Windows scheduled task per hq instance (task name embeds a short hash of the
# canonical absolute hq.toml path - the instance identity). The task runs `mail tick`
# every minute + at logon as the current user (Interactive token, hidden window).
# Time policy (Beijing 19:00-10:00 window) is evaluated inside Python, so the OS
# time zone does not matter.
#
# Passwords never appear in the task action: the sender reads the password from the
# environment variable named by hq.toml [mail] password_env.
#
# Usage:
#   powershell -File scripts\install-mail-task.ps1 -DryRun     # show, do not register
#   powershell -File scripts\install-mail-task.ps1             # register/overwrite own task
#   powershell -File scripts\install-mail-task.ps1 -Status
#   powershell -File scripts\install-mail-task.ps1 -Status -Json  # structured, for dashboard
#   powershell -File scripts\install-mail-task.ps1 -Remove
#
# Status/Remove only need the instance identity (canonical config path): they work
# even after hq.toml or the Python install has been deleted - config presence and a
# usable Python are INSTALL preconditions, not query/remove preconditions.
#
# NOTE: the machine must be on (user logged in) for ticks to run; sleep/shutdown
# cannot guarantee on-time delivery. Digests are back-filled only for the most
# recent cutoff after recovery.
param(
    [switch]$DryRun,
    [switch]$Status,
    [switch]$Json,
    [switch]$Remove,
    [string]$Python = "",
    [string]$HqRoot = "",
    [string]$HqConfig = ""
)
$ErrorActionPreference = "Stop"

if (-not $HqRoot) {
    $HqRoot = Split-Path -Parent $PSScriptRoot
}
if (-not $HqConfig) {
    $HqConfig = Join-Path $HqRoot "hq.toml"
}

# Canonical absolute path = instance identity. Works for existing files (on-disk
# casing) and for deleted ones (path still resolvable without the file).
$item = Get-Item -LiteralPath $HqConfig -ErrorAction SilentlyContinue
if ($item) {
    $HqConfig = $item.FullName
}
else {
    $HqConfig = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($HqConfig)
}

$sha = [System.Security.Cryptography.SHA256]::Create()
$hashBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($HqConfig))
$hash = ([System.BitConverter]::ToString($hashBytes) -replace '-', '').Substring(0, 8).ToLower()
$taskName = "hq-mail-tick-$hash"

if ($Status) {
    if ($Json) {
        # Structured output for the local dashboard. A missing task is a
        # fact, not a query failure: exit 0 with installed=false. Only an
        # unexpected query error exits non-zero (ok=false + stderr detail).
        $base = [ordered]@{ ok = $true; task = $taskName; instance_config = $HqConfig }
        try {
            # Enumerate + filter: a missing task yields no error here, so
            # "not installed" stays a fact (exit 0), distinct from a real
            # query failure (-ErrorAction Stop would throw on missing).
            $found = @(Get-ScheduledTask | Where-Object { $_.TaskName -eq $taskName })
            if ($found.Count -gt 0) {
                $t = $found[0]
                $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
            }
            else {
                $t = $null
            }
        }
        catch {
            $msg = $_.Exception.Message
            $base.ok = $false
            $base.error = $msg
            $base | ConvertTo-Json -Compress
            Write-Error $msg
            exit 1
        }
        if ($t) {
            $lastRun = $null
            $lastResult = $null
            if ($info) {
                if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) {
                    $lastRun = $info.LastRunTime.ToString("o")
                }
                $lastResult = [long]$info.LastTaskResult
            }
            $base.installed = $true
            $base.state = [string]$t.State
            $base.execute = [string]$t.Actions[0].Execute
            $base.arguments = [string]$t.Actions[0].Arguments
            $base.working_directory = [string]$t.Actions[0].WorkingDirectory
            $base.last_run_time = $lastRun
            $base.last_task_result = $lastResult
        }
        else {
            $base.installed = $false
            $base.state = $null
            $base.last_run_time = $null
            $base.last_task_result = $null
        }
        $base | ConvertTo-Json -Compress
        exit 0
    }
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Output "NOT INSTALLED: $taskName"
        Write-Output "Instance config: $HqConfig"
        exit 1
    }
    Write-Output "INSTALLED: $taskName (state: $($t.State))"
    Write-Output "Instance config: $HqConfig"
    Write-Output "Execute: $($t.Actions[0].Execute)"
    Write-Output "Arguments: $($t.Actions[0].Arguments)"
    Write-Output "WorkingDirectory: $($t.Actions[0].WorkingDirectory)"
    exit 0
}

if ($Remove) {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $t) {
        Write-Output "NOT INSTALLED: $taskName (nothing to remove)"
        exit 0
    }
    if ($DryRun) {
        Write-Output "[DryRun] would unregister task: $taskName"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "REMOVED: $taskName"
    exit 0
}

# ---- Everything below is the install path: preconditions apply from here on. ----

if (-not (Test-Path $HqConfig)) {
    if ($DryRun) {
        Write-Output "WARNING: hq.toml not found: $HqConfig (copy hq.toml.example first) - shown for preview only."
    }
    else {
        Write-Error "hq.toml not found: $HqConfig (copy hq.toml.example first)."
    }
}
$HqRoot = (Get-Item $HqRoot).FullName
$Bootstrap = Join-Path $HqRoot "bin\hq.py"

if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Error "python not found on PATH; pass -Python <absolute python.exe> (3.11+)."
    }
    $Python = $cmd.Source
}
$Python = (Get-Item $Python).FullName
$ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0) {
    Write-Error "failed to query Python version via $Python"
}
$minor = [int]($ver.Split('.')[1])
if (([int]($ver.Split('.')[0]) -lt 3) -or (([int]($ver.Split('.')[0]) -eq 3) -and ($minor -lt 11))) {
    Write-Error "Python >= 3.11 required, found $ver ($Python)."
}

$actionArg = "-X utf8 `"$Bootstrap`" --hq-config `"$HqConfig`" mail tick"

Write-Output "Task name : $taskName"
Write-Output "Python    : $Python (v$ver)"
Write-Output "Bootstrap : $Bootstrap"
Write-Output "HqConfig  : $HqConfig"
Write-Output "Action    : `"$Python`" $actionArg"
Write-Output "Triggers  : every 1 minute (indefinite) + at logon"
Write-Output "Settings  : MultipleInstances=IgnoreNew, StartWhenAvailable, Hidden,"
Write-Output "            AllowStartIfOnBatteries, DontStopIfGoingOnBatteries, ExecutionTimeLimit=30min"
Write-Output "Principal : current user ($env:USERNAME), Interactive token, RunLevel=Limited"
Write-Output "Password  : never in task arguments (sender reads password_env at send time)"
Write-Output "Limits    : machine on + user session available required; no on-time promise"
Write-Output "            during sleep/shutdown. Install success != verified real trigger."

if ($DryRun) {
    Write-Output "[DryRun] nothing registered."
    exit 0
}

$action = New-ScheduledTaskAction -Execute $Python -Argument $actionArg -WorkingDirectory $HqRoot
$trigger1 = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ([TimeSpan]::MaxValue)
$trigger2 = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($trigger1, $trigger2) -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "REGISTERED: $taskName (overwrites only this instance's task)."
