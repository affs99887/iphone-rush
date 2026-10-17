param([string]$ExeName = 'iPhoneDuo预约助手-1.5.exe')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskExe = Join-Path $taskRoot "release\$ExeName"
$taskResult = Join-Path $taskRoot 'release\artifacts\ui-smoke.json'
$taskStarted = Get-Date
$taskProcess = Start-Process -FilePath $taskExe -ArgumentList '--ui-smoke' -WorkingDirectory (Join-Path $taskRoot 'release') -WindowStyle Hidden -PassThru
if (-not $taskProcess.WaitForExit(60000)) {
    Stop-Process -Id $taskProcess.Id -ErrorAction SilentlyContinue
    throw 'Packaged executable startup timed out.'
}
if ($taskProcess.ExitCode -ne 0) { throw "Executable exited with $($taskProcess.ExitCode)." }
if ((Get-Item -LiteralPath $taskResult).LastWriteTime -lt $taskStarted) { throw 'No fresh smoke-test result was produced.' }
$taskReport = Get-Content -LiteralPath $taskResult -Raw | ConvertFrom-Json
if (-not $taskReport.visible) { throw 'The native window did not become visible.' }
if ($taskReport.console_window) { throw 'The executable has a console window.' }
$taskReport | ConvertTo-Json -Depth 5
