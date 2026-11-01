[CmdletBinding()]
param([switch]$ReviewOnly)
$ErrorActionPreference = 'Stop'
$installRoot = 'C:\Program Files\CodexDurableRelay'
$hostsPath = 'C:\Windows\System32\drivers\etc\hosts'
$taskNames = @('Codex Durable Relay - User','Codex Durable Relay - Hosts')

# This entry only removes a partial directory before system registration. It
# must never substitute for normal uninstall of a running/registered relay.
if (Get-ScheduledTask -TaskName $taskNames -ErrorAction SilentlyContinue) { throw 'Recovery refused: relay tasks exist; use the protected uninstaller.' }
if (Get-Service -Name 'CodexDurableRelayLifecycle' -ErrorAction SilentlyContinue) { throw 'Recovery refused: lifecycle service exists; use the protected uninstaller.' }
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalAddress -eq '127.0.0.2' -and $_.LocalPort -eq 443 })
if ($listeners.Count) { throw 'Recovery refused: relay listener still exists.' }
if ([IO.File]::ReadAllText($hostsPath).IndexOf('CODEX-DURABLE-RELAY-V2',[StringComparison]::OrdinalIgnoreCase) -ge 0) { throw 'Recovery refused: owned hosts mapping requires protected cleanup.' }
if (-not (Test-Path -LiteralPath $installRoot)) { Write-Output 'No partial installation directory exists.'; return }
$resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $installRoot).Path).TrimEnd('\')
if (-not [String]::Equals($resolved,'C:\Program Files\CodexDurableRelay',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected recovery path.' }

# Check every directory before descending; never follow junctions/symlinks.
$pending = New-Object 'System.Collections.Generic.Stack[string]'
$pending.Push($resolved)
while ($pending.Count) {
    $directory = $pending.Pop()
    $item = Get-Item -LiteralPath $directory -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Recovery refused: redirected directory.' }
    foreach ($child in Get-ChildItem -LiteralPath $directory -Force) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Recovery refused: redirected child.' }
        if ($child.PSIsContainer) { $pending.Push($child.FullName) }
    }
}
if ($ReviewOnly) { Write-Output 'Early-install recovery eligible: only the fixed partial installation directory would be removed.'; return }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Actual early-install recovery requires an administrator; review it before elevation.' }
Remove-Item -LiteralPath $resolved -Recurse -Force
if (Test-Path -LiteralPath $resolved) { throw 'Partial installation directory removal is unconfirmed.' }
Write-Output 'Removed only the unregistered partial installation directory; no hosts, task or service was changed.'
