param(
    [ValidatePattern('^[\w.\-]+$')][string]$OutputName = 'iPhoneDuo预约助手-2.0',
    [string]$PythonExecutable = 'python'
)
$ErrorActionPreference = 'Stop'
$taskPython = $PythonExecutable
$env:PYTHONPATH = Join-Path $PSScriptRoot '.vendor'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'build\pyinstaller-cache'
$env:DUO_EXE_NAME = $OutputName
Set-Location -LiteralPath $PSScriptRoot
$taskOriginalPath = $env:Path
try {
    # The document runtime adds old native DLLs to PATH. Never collect those as Qt dependencies.
    $env:Path = ((Join-Path $PSScriptRoot '.vendor\PySide6'), (Join-Path $PSScriptRoot '.vendor\shiboken6'), "$env:SystemRoot\System32", $env:SystemRoot) -join ';'
    & $taskPython -m PyInstaller --noconfirm 'duo_build.spec'
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
} finally {
    $env:Path = $taskOriginalPath
}
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'release') -Force | Out-Null
# PyInstaller reports success a moment before the OS finishes flushing the .exe to disk; wait for it.
$taskDistExe = Join-Path $PSScriptRoot "dist\$OutputName.exe"
for ($i = 0; $i -lt 30 -and -not (Test-Path -LiteralPath $taskDistExe); $i++) { Start-Sleep -Milliseconds 500 }
if (-not (Test-Path -LiteralPath $taskDistExe)) { throw "Built executable not found at $taskDistExe" }
Copy-Item -LiteralPath $taskDistExe -Destination (Join-Path $PSScriptRoot "release\$OutputName.exe") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '使用说明.txt') -Destination (Join-Path $PSScriptRoot 'release\使用说明.txt')
# Ship every dated verification record so the release folder is self-describing.
Get-ChildItem -LiteralPath $PSScriptRoot -File |
    Where-Object { $_.Name -match '^(官网|版本)验证记录-\d{8}\.txt$' } |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $PSScriptRoot "release\$($_.Name)") }
$taskExe = Join-Path $PSScriptRoot "release\$OutputName.exe"
$taskHash = (Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host ("Built {0}  {1} bytes  SHA-256 {2}" -f $OutputName, (Get-Item -LiteralPath $taskExe).Length, $taskHash)
