# Palworld Server Dashboard - installer.
# Run this in an ADMIN PowerShell on the machine that hosts your Palworld dedicated server.
# It: installs a self-contained Python + the save-reading libraries into collector\python,
# copies the web page into your webDir, and registers two scheduled tasks (collector every 1 min,
# save parser every ~15 min). Nothing touches the system PATH or registry.
#
#   1. Copy collector\config.example.json -> collector\config.json and edit the paths.
#   2. Enable the Palworld REST API (see README).
#   3. Run:  powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo      = $PSScriptRoot
$collector = Join-Path $repo 'collector'
$cfgPath   = Join-Path $collector 'config.json'
$pyDir     = Join-Path $collector 'python'
$pyExe     = Join-Path $pyDir 'python.exe'

if (-not (Test-Path $cfgPath)) {
    throw "Missing $cfgPath - copy collector\config.example.json to collector\config.json and edit it first."
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
foreach ($k in 'palworldSaveRoot','palworldConfigIni','dataDir','webDir') {
    if (-not $cfg.$k) { throw "config.json is missing '$k'" }
}
Write-Host "Config OK." -ForegroundColor Green

# --- 1. self-contained embeddable Python + deps (no PATH / registry impact) ---
Write-Host "Installing embeddable Python + save libraries into $pyDir ..."
if (Test-Path $pyDir) { Remove-Item $pyDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pyDir | Out-Null
$zip = Join-Path $env:TEMP 'pal-py-embed.zip'
Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile $zip -UseBasicParsing
Expand-Archive $zip -DestinationPath $pyDir -Force
Remove-Item $zip -Force
# enable site-packages so pip-installed modules import
$pth = Get-ChildItem $pyDir -Filter '*._pth' | Select-Object -First 1
$c = Get-Content $pth.FullName
$c = $c -replace '^#\s*import site', 'import site'
if ($c -notcontains 'import site') { $c += 'import site' }
Set-Content $pth.FullName $c -Encoding ASCII
Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile (Join-Path $pyDir 'get-pip.py') -UseBasicParsing
& $pyExe (Join-Path $pyDir 'get-pip.py') --no-warn-script-location | Out-Null
& $pyExe -m pip install --no-warn-script-location palworld-save-tools pyooz | Out-Null
& $pyExe -c "import palworld_save_tools, ooz; print('Python + palworld-save-tools + ooz OK')"

# --- 2. web page into webDir ---
New-Item -ItemType Directory -Force -Path $cfg.webDir  | Out-Null
New-Item -ItemType Directory -Force -Path $cfg.dataDir | Out-Null
Copy-Item (Join-Path $repo 'web\index.html') (Join-Path $cfg.webDir 'index.html') -Force
Write-Host "Copied dashboard to $($cfg.webDir)\index.html" -ForegroundColor Green

# --- 3. scheduled tasks (run as SYSTEM so they survive logoff and can read the save) ---
function Register-Task($name, $argument, $everyMinutes) {
    try { Unregister-ScheduledTask -TaskName $name -Confirm:$false } catch {}
    $action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument
    $trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $everyMinutes)
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $name
    Write-Host "Registered + started task: $name (every $everyMinutes min)" -ForegroundColor Green
}
$collectorPs = Join-Path $collector 'pal-dashboard-collector.ps1'
$parserPy    = Join-Path $collector 'pal-save-parse.py'
Register-Task 'Palworld Dashboard Collector' "-NoProfile -ExecutionPolicy Bypass -File `"$collectorPs`"" 1
try { Unregister-ScheduledTask -TaskName 'Palworld Save Parser' -Confirm:$false } catch {}
$a = New-ScheduledTaskAction -Execute $pyExe -Argument "`"$parserPy`""
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
$pr = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$se = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'Palworld Save Parser' -Action $a -Trigger $t -Principal $pr -Settings $se | Out-Null
Start-ScheduledTask -TaskName 'Palworld Save Parser'
Write-Host "Registered + started task: Palworld Save Parser (every 15 min)" -ForegroundColor Green

Write-Host ""
Write-Host "Done. Within a minute, $($cfg.webDir)\palworld.json will appear." -ForegroundColor Cyan
Write-Host "Serve $($cfg.webDir) with any static web server and open index.html." -ForegroundColor Cyan
