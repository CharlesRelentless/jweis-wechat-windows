param(
    [ValidatePattern('^tunnel_[a-f0-9]{32}$')]
    [string]$TunnelId,
    [switch]$ReplaceConfig
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$clientExe = Join-Path $projectRoot 'bin\tunnel-client.exe'
if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $clientExe)) {
    throw 'Project Python and verified official tunnel client are required.'
}
if (-not $TunnelId) {
    $TunnelId = Read-Host 'Dedicated private Tunnel ID (identifier only)'
    if ($TunnelId -notmatch '^tunnel_[a-f0-9]{32}$') { throw 'Invalid Tunnel ID.' }
}
Push-Location -LiteralPath $projectRoot
try {
    & $pythonExe -c "from probe.client_integrity import verify_bundle; verify_bundle('.')"
    if ($LASTEXITCODE -ne 0) { throw 'Official client bundle integrity check failed.' }
    $renderArgs = @('scripts/render-config.py','--tunnel-id',$TunnelId,'--discover-callback')
    if ($ReplaceConfig) { $renderArgs += '--replace' }
    & $pythonExe @renderArgs
    if ($LASTEXITCODE -ne 0) { throw 'Private configuration generation failed.' }
    if (-not (Test-Path -LiteralPath 'runtime\secrets\runtime.key')) {
        # The owner enters the key in a real terminal; no plaintext argument,
        # environment variable, transcript or assistant stdin is used.
        & $pythonExe 'scripts/set-runtime-key.py'
        if ($LASTEXITCODE -ne 0) { throw 'Owner key entry was not completed.' }
    }
    Write-Host 'Configuration prepared in callback discovery mode.'
    Write-Host 'Next: run the doctor check, then scripts/run-windows.py in this terminal.'
} finally { Pop-Location }
