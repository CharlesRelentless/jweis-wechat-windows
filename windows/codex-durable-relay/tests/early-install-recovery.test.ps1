$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$source = [IO.File]::ReadAllText((Join-Path $root 'scripts\Recover-EarlyInstall.ps1'))
$testRoot = Join-Path $PSScriptRoot ('.early-recovery-test-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    foreach ($case in @('eligible','remove_success','task','service','listener','mapping')) {
        $dir = Join-Path $testRoot $case
        $install = Join-Path $dir 'CodexDurableRelay'
        New-Item -ItemType Directory -Path $install -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $install 'partial.txt'),'fixture')
        [IO.File]::WriteAllText((Join-Path $dir 'keep.txt'),'unrelated fixture')
        $hosts = Join-Path $dir 'hosts.fixture'
        [IO.File]::WriteAllText($hosts,$(if($case -eq 'mapping'){'# CODEX-DURABLE-RELAY-V2'}else{'# fixture'}))
        $prefix = @'
param([switch]$ReviewOnly)
$case='__CASE__'
function Get-ScheduledTask {param($TaskName);if($case -eq 'task'){[pscustomobject]@{State='Ready'}}}
function Get-Service {param($Name);if($case -eq 'service'){[pscustomobject]@{Status='Stopped'}}}
function Get-NetTCPConnection {param($State);if($case -eq 'listener'){[pscustomobject]@{LocalAddress='127.0.0.2';LocalPort=443}}}
'@
        if($case -ne 'remove_success') { $prefix += "`nfunction Remove-Item {throw 'ReviewOnly must never remove anything'}`n" }
        $body = $source.Substring($source.IndexOf("$"+"ErrorActionPreference = 'Stop'"))
        $body = $body.Replace('C:\Program Files\CodexDurableRelay',$install.Replace("'","''")).Replace('C:\Windows\System32\drivers\etc\hosts',$hosts.Replace("'","''"))
        if($case -eq 'remove_success') {
            $body = $body.Replace('$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())', '$principal=New-Object psobject; $principal|Add-Member ScriptMethod IsInRole {param($Role); return $true}')
        }
        $fixture = Join-Path $dir 'entry.ps1'
        [IO.File]::WriteAllText($fixture,($prefix.Replace('__CASE__',$case)+"`ntry {`n"+$body+"`n} catch { exit 1 }"),(New-Object Text.UTF8Encoding($true)))
        $arguments=@('-NoProfile','-NonInteractive','-File',$fixture)
        if($case -ne 'remove_success') {$arguments+='-ReviewOnly'}
        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') @arguments | Out-Null
        $expected = if($case -in @('eligible','remove_success')){0}else{1}
        if($LASTEXITCODE -ne $expected){throw ($case+': unsafe recovery eligibility')}
        if($case -eq 'remove_success') {
            if(Test-Path -LiteralPath $install){throw 'Eligible recovery did not remove the partial fixture'}
        } elseif(-not (Test-Path -LiteralPath (Join-Path $install 'partial.txt'))){throw 'Recovery review changed fixture files'}
        if(-not (Test-Path -LiteralPath (Join-Path $dir 'keep.txt'))){throw 'Recovery removed unrelated fixture data'}
    }
    Write-Output '6 early recovery refusal/preview/removal cases passed; only temporary fixture data was changed.'
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    if([IO.Path]::GetDirectoryName($resolved) -ne [IO.Path]::GetFullPath($PSScriptRoot) -or -not [IO.Path]::GetFileName($resolved).StartsWith('.early-recovery-test-')) {throw 'Unexpected cleanup path'}
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
