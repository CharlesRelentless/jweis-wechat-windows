$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$source = [IO.File]::ReadAllText((Join-Path $root 'src\Manage-CodexRelay.ps1'))
$prefix = $source.Substring($source.IndexOf("$" + "ErrorActionPreference = 'Stop'"))
$prefix = $prefix.Substring(0,$prefix.IndexOf('function Read-State'))
foreach ($case in @('missing','malformed')) {
    foreach ($requestedAction in @('Uninstall','Status')) {
        $Action = $requestedAction
        $script:settingsReads = 0
        function Get-Content {
            param($LiteralPath,[switch]$Raw,$Encoding)
            $script:settingsReads++
            if ($case -eq 'missing') { throw 'fixture settings missing' }
            return '{broken-json'
        }
        $failed = $false
        try { & ([scriptblock]::Create($prefix)) } catch { $failed = $true }
        if ($requestedAction -eq 'Uninstall') {
            if ($failed -or $script:settingsReads -ne 0) { throw ($case+': uninstall must reach its fixed-path recovery without reading settings') }
        } elseif (-not $failed) { throw 'Normal status must still reject missing or malformed configuration' }
        Remove-Item Function:\Get-Content
    }
}
Write-Output '4 settings-entrypoint cases passed; no installation or system changes.'
