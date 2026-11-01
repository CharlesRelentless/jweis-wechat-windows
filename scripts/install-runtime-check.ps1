param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [ValidateRange(1,60)][int]$IntervalMinutes = 5,
    [ValidatePattern('^[A-Za-z0-9_-]{1,64}$')][string]$TaskName = 'JweisBridgeHealth'
)
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$pythonPath = Join-Path $rootPath '.venv\Scripts\python.exe'
$checkPath = Join-Path $rootPath 'scripts\check-runtime.py'
$invokePath = Join-Path $rootPath 'scripts\invoke-runtime-check.ps1'
foreach ($required in @($pythonPath,$checkPath,$invokePath,(Join-Path $rootPath 'scripts\show-runtime-alert.ps1'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw 'Runtime check files or Python missing' }
}
& $pythonPath $checkPath --root $rootPath --initialize
if ($LASTEXITCODE -ne 0) { throw 'Endpoint pin initialization failed; no task installed' }
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Arguments -notlike "*$invokePath*")) {
    throw 'The task name belongs to another action; refusing to replace it'
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $invokePath + '" -Root "' + $rootPath + '"') -WorkingDirectory $rootPath
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 90)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger @($logon,$repeat) -Settings $settings -Principal $principal -Description 'Read-only Jweis gateway/poll/subscription/endpoint checks; change-only local alerts; never restart a temporary endpoint or send WeChat messages.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
