$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$temp = Join-Path $PSScriptRoot ('.policy-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $fixture = Join-Path $temp 'fixed-script.ps1'
    [IO.File]::WriteAllText($fixture,'param([string]$Mode); exit 0',(New-Object Text.UTF8Encoding($true)))
    $assembly = [Reflection.Assembly]::LoadFile((Join-Path $root 'dist\CodexRelayLifecycle.exe'))
    $type = $assembly.GetType('CodexRelayLifecycle',$true)
    $flags = [Reflection.BindingFlags]'NonPublic,Static'
    $modeType = $type.GetNestedType('ScriptMode',[Reflection.BindingFlags]::NonPublic)
    $method = $type.GetMethod('BuildArguments',$flags)
    $serviceArgs = $method.Invoke($null,@([Enum]::Parse($modeType,'Initialize'),[uint32]0))
    $serviceArgs = $serviceArgs.Replace('C:\Program Files\CodexDurableRelay\Shutdown-CodexRelay.ps1',$fixture)
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'src\Install-CodexRelay.ps1'),[ref]$tokens,[ref]$errors)
    $assignment=$ast.Find({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$helperAction'},$true)
    if (-not $assignment) { throw 'Maintenance task entrypoint not found.' }
    function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory); return $Argument }
    $installRoot=$temp
    $powerShellPath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $helperArgs=& ([scriptblock]::Create($assignment.Right.Extent.Text))
    $helperArgs=$helperArgs.Replace((Join-Path $temp 'managed-relay-hosts.ps1'),$fixture)
    foreach($entry in @(@{name='service';arguments=$serviceArgs},@{name='maintenance';arguments=$helperArgs})) {
        $info=New-Object Diagnostics.ProcessStartInfo
        $info.FileName=$powerShellPath
        $info.Arguments=$entry.arguments
        $info.UseShellExecute=$false
        $info.CreateNoWindow=$true
        $info.RedirectStandardOutput=$true
        $info.RedirectStandardError=$true
        # Model SYSTEM without CurrentUser policy: no machine policy change.
        $info.EnvironmentVariables['PSExecutionPolicyPreference']='Restricted'
        $process=[Diagnostics.Process]::Start($info)
        try {
            $output=$process.StandardOutput.ReadToEnd()
            $errorOutput=$process.StandardError.ReadToEnd()
            if (-not $process.WaitForExit(10000)) { $process.Kill(); throw 'Policy fixture timed out.' }
            if ($process.ExitCode -ne 0) { throw ($entry.name+': fixed unsigned local script was blocked under the restricted inherited policy.') }
        } finally { $process.Dispose() }
    }
    Write-Output '2 fixed SYSTEM script entrypoints passed under a restricted inherited policy; no machine/user execution policy was changed.'
} finally {
    $resolved=[IO.Path]::GetFullPath($temp)
    $expected=[IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')+'\'
    if (-not $resolved.StartsWith($expected,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notlike '.policy-test-*') {throw 'Unexpected policy fixture cleanup path.'}
    if (Test-Path -LiteralPath $resolved) {Remove-Item -LiteralPath $resolved -Recurse -Force}
}
