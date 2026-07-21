# Palworld broadcast poller — pulls queued messages from the VPS and shows them in-game.
#
# WHY A POLLER: the game server's REST API can kick, ban, wipe and shut down, so it is LAN-only and must
# never be exposed to the internet. Nothing reaches IN to this box. Instead the box reaches OUT, polling a
# queue on the VPS and calling its own local API. See "Palworld Broadcast - Endpoint Spec".
#
# SAFETY — this is the whole security argument, do not weaken it:
#   * The queue carries message TEXT only. This script calls exactly ONE game API - /v1/api/announce - and
#     never interprets a queued string as a command, path, or id. There is no code path here by which a
#     queued item can kick/ban/wipe/shutdown, so a leaked admin token can only ever send annoying messages.
#   * Messages are length-capped and rate-capped here, independently of the VPS's own limits, so a leaked
#     token cannot machine-gun players.
#
# Runs as a long-lived loop (Task Scheduler's minimum repetition is 1 minute, which is too slow for a chat-
# like feature), started at boot. Off unless configured: no URL or no token => exits quietly.
#
# Repo variant: config-driven. The box variant has hardcoded paths and reads its token from a file.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg = Get-Content (Join-Path $here 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$BaseUrl   = $cfg.broadcastPollUrl                              # e.g. https://example.com/palworld
$BoxToken  = $cfg.broadcastBoxToken                             # secret; config.json is gitignored
$DataDir   = $cfg.dataDir
$Ini       = $cfg.palworldConfigIni
$RestHost  = if ($cfg.restHost) { $cfg.restHost } else { '127.0.0.1' }
$PollSecs  = if ($cfg.broadcastPollSeconds)  { [int]$cfg.broadcastPollSeconds }  else { 10 }
$MaxPerMin = if ($cfg.broadcastMaxPerMinute) { [int]$cfg.broadcastMaxPerMinute } else { 10 }
$MaxChars  = 200

$CursorFile = Join-Path $DataDir 'broadcast-cursor.txt'
$LogFile    = Join-Path $DataDir 'broadcast.log'

function Write-Log($msg) {
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  Add-Content -Path $LogFile -Value $line -Encoding utf8
}

if ([string]::IsNullOrWhiteSpace($BaseUrl) -or [string]::IsNullOrWhiteSpace($BoxToken)) {
  # Dormant by design: the feature ships disabled and stays inert until both are configured.
  Write-Host 'broadcast poller not configured (broadcastPollUrl / broadcastBoxToken empty) - exiting'
  exit 0
}

# REST auth comes from the game's own ini, so no game password is duplicated into config.json.
$iniText  = Get-Content $Ini -Raw
$AdminPw  = ([regex]::Match($iniText, 'AdminPassword="([^"]*)"')).Groups[1].Value
$RestPort = ([regex]::Match($iniText, 'RESTAPIPort=(\d+)')).Groups[1].Value
if (-not $RestPort) { $RestPort = '8212' }
if (-not $AdminPw) { Write-Log 'FATAL: could not read AdminPassword from ini'; exit 1 }

$restHeaders = @{
  Authorization  = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:$AdminPw"))
  'Content-Type' = 'application/json'
}
$queueHeaders = @{ Authorization = "Bearer $BoxToken" }

function Get-Cursor {
  if (Test-Path $CursorFile) {
    $v = (Get-Content $CursorFile -Raw).Trim()
    if ($v -match '^\d+$') { return [int64]$v }
  }
  return [int64]0
}
function Set-Cursor($id) { Set-Content -Path $CursorFile -Value ([string]$id) -Encoding ascii }

function Send-Announce($text) {
  # UTF-8 encode explicitly: nicknames and messages contain non-ASCII and the default encoding mangles them.
  $json  = @{ message = $text } | ConvertTo-Json -Compress
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  # -UseBasicParsing is required: Windows PowerShell otherwise tries the IE engine to parse the response
  # and throws even on a successful 200.
  $r = Invoke-WebRequest -Uri "http://${RestHost}:${RestPort}/v1/api/announce" -Method POST `
        -Headers $restHeaders -Body $bytes -TimeoutSec 10 -UseBasicParsing
  return [int]$r.StatusCode
}

Write-Log "poller started (pid $PID) url=$BaseUrl poll=${PollSecs}s cap=${MaxPerMin}/min cursor=$(Get-Cursor)"
$sendTimes = New-Object System.Collections.ArrayList

while ($true) {
  try {
    $cursor = Get-Cursor
    $resp = Invoke-RestMethod -Uri "$BaseUrl/broadcast-pending?since=$cursor" -Headers $queueHeaders -TimeoutSec 15
    $msgs = @($resp.messages) | Where-Object { $_ -and $_.id -gt $cursor } | Sort-Object id

    foreach ($m in $msgs) {
      # Rolling one-minute cap. Anything over the cap simply waits for the next window - the cursor only
      # advances for messages actually sent, so nothing is lost, just paced.
      $cut = (Get-Date).AddMinutes(-1)
      while ($sendTimes.Count -gt 0 -and $sendTimes[0] -lt $cut) { $sendTimes.RemoveAt(0) }
      if ($sendTimes.Count -ge $MaxPerMin) {
        Write-Log "rate cap reached ($MaxPerMin/min) - deferring remaining messages"
        break
      }

      $text = [string]$m.message
      if ([string]::IsNullOrWhiteSpace($text)) { Set-Cursor $m.id; continue }   # skip junk, don't replay it
      if ($text.Length -gt $MaxChars) { $text = $text.Substring(0, $MaxChars) }

      try {
        $code = Send-Announce $text
        if ($code -ge 200 -and $code -lt 300) {
          [void]$sendTimes.Add((Get-Date))
          Set-Cursor $m.id          # advance ONLY after a confirmed send, so a crash never drops a message
          Write-Log ("sent id={0}: {1}" -f $m.id, $text)
        } else {
          Write-Log ("announce returned HTTP {0} for id={1} - will retry" -f $code, $m.id)
          break                     # leave cursor put; retry on the next poll
        }
      } catch {
        Write-Log ("announce FAILED for id={0}: {1} - will retry" -f $m.id, $_.Exception.Message)
        break
      }
    }
  } catch {
    # Network blips, VPS restarts and 401s all land here. Stay quiet-ish and keep polling; the cursor is
    # untouched so nothing is lost.
    Write-Log ("poll failed: {0}" -f $_.Exception.Message)
  }
  Start-Sleep -Seconds $PollSecs
}
