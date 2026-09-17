param([string]$Output)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
if (-not $Output) { $Output = Join-Path $appRoot 'COWMATA.exe' }
if (Test-Path -LiteralPath $Output) { throw 'Launcher output exists; choose a fresh output or retain the previous build first.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Maintainer compiler missing: .NET Framework 4.x csc.exe' }
$buildArguments = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
    ('/out:"' + $Output + '"'), ('/win32icon:"' + $appRoot + '\assets\app-icon\cowmata.ico"'),
    ('/win32manifest:"' + $appRoot + '\packaging\app.manifest"'), ('"' + $appRoot + '\packaging\Launcher.cs"'))
$buildProcess = Start-Process -FilePath $compiler -ArgumentList $buildArguments -PassThru -Wait -WindowStyle Hidden
if ($buildProcess.ExitCode -ne 0) { throw ('Launcher compilation failed: ' + $buildProcess.ExitCode) }
Get-FileHash -LiteralPath $Output -Algorithm SHA256
