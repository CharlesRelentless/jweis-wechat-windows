param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$pythonPath = Join-Path $rootPath '.venv\Scripts\python.exe'
$checkPath = Join-Path $rootPath 'scripts\check-runtime.py'
$logPath = Join-Path $rootPath 'runtime\monitor\last-run.log'
& $pythonPath $checkPath --root $rootPath --public-check --notify 2>&1 | Out-File -LiteralPath $logPath -Encoding utf8
exit $LASTEXITCODE
