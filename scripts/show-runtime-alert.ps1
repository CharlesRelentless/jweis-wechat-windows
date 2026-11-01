param([Parameter(Mandatory)][string]$ReportPath)
$ErrorActionPreference = 'Stop'
$report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
$message = if ($report.notification_kind -eq 'recovered') {
    '微信桥接检查已恢复。此通知只表示运行检查通过，不代替手机收件验收。'
} else {
    '微信桥接检查发现异常：' + ($report.reasons -join ', ') + "`n请查看项目 runtime\monitor\status.json 和 docs\WEIXIN-RECOVERY.md。不要盲目重启临时入口。"
}
$shell = New-Object -ComObject WScript.Shell
[void]$shell.Popup($message, 10, 'Jweis 微信桥接运行检查', 48)
