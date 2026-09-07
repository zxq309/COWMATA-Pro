param([string]$Output)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
if (-not $Output) { $Output = Join-Path $appRoot 'COWMATA.exe' }
if (Test-Path -LiteralPath $Output) { throw 'Launcher output exists; choose a fresh output or retain the previous build first.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Maintainer compiler missing: .NET Framework 4.x csc.exe' }
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 "/out:$Output" "/win32icon:$appRoot\assets\app-icon\cowmata.ico" "/win32manifest:$appRoot\packaging\app.manifest" "$appRoot\packaging\Launcher.cs"
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed' }
Get-FileHash -LiteralPath $Output -Algorithm SHA256
