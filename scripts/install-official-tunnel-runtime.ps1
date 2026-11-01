param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [ValidatePattern('^[A-Za-z0-9_-]{1,64}$')][string]$TaskName = 'JweisBridgeOfficialTunnel',
    [switch]$Activate
)
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$pythonPath = Join-Path $rootPath '.venv\Scripts\python.exe'
$runnerPath = Join-Path $rootPath 'scripts\invoke-native-official.py'
$legacyRunnerPath = Join-Path $rootPath 'scripts\invoke-official-runtime.ps1'
$preflight = @'
import os,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
from probe.client_integrity import verify_bundle
from probe.platform_io import is_private,open_private
from probe.tunnel_runtime_env import official_environment
profile=root/'runtime/profile.yaml'
assert profile.is_file() and is_private(profile), 'Private profile required'
verify_bundle(root)
activate=sys.argv[2]=='True'
if activate:
    assert official_environment(root,profile) is not None, 'Authorized env-file profile required'
else:
    try:
        official_environment(root,profile)
    except ValueError as exc:
        if str(exc)!='Official tunnel activation is blocked until free transport is confirmed.': raise
    else:
        raise ValueError('Preparation installer requires activation to remain blocked')
os.close(open_private(root/'runtime/official-tunnel.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND))
print('Official runtime activation validated.' if activate else 'Official runtime prepared; online activation remains blocked.')
'@
& $pythonPath -c $preflight $rootPath $Activate.IsPresent.ToString()
if ($LASTEXITCODE -ne 0) { throw 'Offline preparation validation failed; no task installed' }
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Arguments -notlike "*$runnerPath*") -and ($existing.Actions.Arguments -notlike "*$legacyRunnerPath*")) {
    throw 'Task name belongs to another action; refusing to replace it'
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$basePython = & $pythonPath -c 'import pathlib,sys; p=pathlib.Path(sys._base_executable); w=p.with_name("pythonw.exe"); print(w if w.is_file() else p)'
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $basePython -PathType Leaf)) { throw 'Installed base interpreter unavailable' }
$action = New-ScheduledTaskAction -Execute $basePython -Argument ('"' + $runnerPath + '"') -WorkingDirectory $rootPath
# Owner's interactive proxy and private files become available at sign-in.
# Boot triggers require elevation and cannot run this interactive principal before sign-in.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
# A native retry trigger also covers hosts where RestartOnFailure is not fired.
# IgnoreNew prevents duplicates; the launcher backs off after three failures.
$retryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$trigger = @($logonTrigger,$retryTrigger)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$settings.Enabled = $Activate.IsPresent
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Owner-authorized official Jweis tunnel. Starts when the owner session is available. Original state, one worker, resource limits, charge-stop latch, and at most three native restart attempts.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
$registered = Get-ScheduledTask -TaskName $TaskName
if (-not $Activate -and $registered.State -ne 'Disabled') { throw 'Preparation task must remain disabled' }
if ($Activate -and -not $registered.Settings.Enabled) { throw 'Authorized task was not enabled' }
$registered | Select-Object TaskName,State
