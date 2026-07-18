# Palworld Server Dashboard - collector.  Run on the machine hosting the Palworld dedicated server,
# every 1 minute (scheduled task). It polls the Palworld REST API, reads the exact in-game clock from the
# save, maintains a persistent store (roster / 7-day history / distance travelled), and writes the public
# palworld.json into your web directory.
#
# PRIVACY: the persistent store (in dataDir) keys on Steam userId and holds raw world coordinates - it is
# NEVER published. The published palworld.json carries only names / levels / playtime / etc. No ids, no IPs,
# no coordinates. Do not point a web server at dataDir; serve webDir only.
#
# Config is read from config.json next to this script (copy config.example.json -> config.json first).
$ErrorActionPreference = 'SilentlyContinue'
$here = $PSScriptRoot
$cfg  = Get-Content (Join-Path $here 'config.json') -Raw | ConvertFrom-Json

$saveRoot = $cfg.palworldSaveRoot
$cfgIni   = $cfg.palworldConfigIni
$dataDir  = $cfg.dataDir
$webDir   = $cfg.webDir
$restHost = if ($cfg.restHost) { $cfg.restHost } else { '127.0.0.1' }
$py       = Join-Path $here 'python\python.exe'
$gametime = Join-Path $here 'pal-gametime.py'

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
New-Item -ItemType Directory -Force -Path $webDir  | Out-Null

$out   = Join-Path $webDir  'palworld.json'
$store = Join-Path $dataDir 'palworld-store.json'
$saveF = Join-Path $dataDir 'palworld-save.json'   # written by pal-save-parse.py (slower task); merged if fresh
# Never hardcode the world GUID - a world wipe generates a new folder. Pick the newest Level.sav.
$world = (Get-ChildItem (Join-Path $saveRoot '*\Level.sav') -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName)

# --- REST auth: read AdminPassword + RESTAPIPort straight from the server's ini (nothing secret in config) ---
$l = (Get-Content $cfgIni | Where-Object { $_ -like 'OptionSettings=*' })
$pass = if ($l -match 'AdminPassword="([^"]*)"') { $Matches[1] } else { '' }
$rport = if ($l -match 'RESTAPIPort=(\d+)') { [int]$Matches[1] } else { 8212 }
$b = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes('admin:' + $pass))
$hdr = @{ Authorization = 'Basic ' + $b }
function Rest($ep) { try { return Invoke-RestMethod -Uri ("http://${restHost}:$rport/v1/api/$ep") -Headers $hdr -TimeoutSec 8 } catch { return $null } }

$info=Rest 'info'; $metrics=Rest 'metrics'; $playersR=Rest 'players'; $settingsR=Rest 'settings'
$up=[bool]$metrics
$now=Get-Date; $nowIso=$now.ToString('o')

# --- load persistent store ---
$P=@{}
$samples=New-Object System.Collections.Generic.List[object]
$peak=0; $lastPoll=$null; $lastDays=$null
$lastTicks=$null; $ticksRate=$null   # in-game clock: last GameDateTimeTicks + smoothed ticks-per-real-minute
if (Test-Path $store) {
  try {
    $s=Get-Content $store -Raw | ConvertFrom-Json
    if ($s.players) {
      foreach($pp in $s.players.PSObject.Properties){ $v=$pp.Value
        $P[$pp.Name]=@{ name=[string]$v.name; firstSeen=[string]$v.firstSeen; lastSeen=[string]$v.lastSeen
                        maxLevel=[int]$v.maxLevel; playMinutes=[double]$v.playMinutes; sessions=[int]$v.sessions
                        online=[bool]$v.online; lastX=[double]$v.lastX; lastY=[double]$v.lastY
                        meters=[double]$v.meters; lvl7d=[int]$v.lvl7d; lvl7dAt=[string]$v.lvl7dAt }
        if (-not $P[$pp.Name].lvl7dAt) { $P[$pp.Name].lvl7d=[int]$v.maxLevel; $P[$pp.Name].lvl7dAt=$nowIso }
      }
    }
    if ($s.samples)   { foreach($x in $s.samples){   $samples.Add([pscustomobject]@{ t=[string]$x.t; n=[int]$x.n; fps=[int]$x.fps; days=[int]$x.days; wkb=[int]$x.wkb; up=[bool]$x.up }) } }
    if ($s.peakOnline){ $peak=[int]$s.peakOnline }
    if ($s.lastPoll)  { $lastPoll=[datetime]$s.lastPoll }
    if ($null -ne $s.lastDays) { $lastDays=[int]$s.lastDays }
    if ($null -ne $s.lastTicks) { $lastTicks=[double]$s.lastTicks }
    if ($null -ne $s.ticksRate) { $ticksRate=[double]$s.ticksRate }
  } catch {}
}
$delta = if ($lastPoll) { [Math]::Min(($now-$lastPoll).TotalMinutes, 5) } else { 0 }

# --- current online players (only trusted when the server is reachable) ---
$onlineNow=@{}
if ($up -and $playersR -and $playersR.players) {
  foreach ($pl in @($playersR.players)) {
    $key=[string]$pl.userId; if(-not $key){$key=[string]$pl.playerId}; if(-not $key){$key=[string]$pl.name}
    $onlineNow[$key]=@{ name=[string]$pl.name; level=[int]$pl.level; ping=[int][math]::Round([double]$pl.ping,0)
                        x=[double]$pl.location_x; y=[double]$pl.location_y }
  }
}

# --- merge (playtime, levels, distance) only when the server is up; if down, leave the store as-is ---
if ($up) {
  foreach ($k in $onlineNow.Keys) {
    $c=$onlineNow[$k]
    if (-not $P.ContainsKey($k)) {
      $P[$k]=@{ name=$c.name; firstSeen=$nowIso; lastSeen=$nowIso; maxLevel=$c.level; playMinutes=0.0; sessions=1
                online=$true; lastX=$c.x; lastY=$c.y; meters=0.0; lvl7d=$c.level; lvl7dAt=$nowIso }
    } else {
      $e=$P[$k]
      if (-not $e.online) { $e.sessions=[int]$e.sessions+1 }
      $e.online=$true; $e.lastSeen=$nowIso; $e.name=$c.name
      if ($c.level -gt $e.maxLevel) { $e.maxLevel=$c.level }
      $e.playMinutes=[double]$e.playMinutes + $delta
      # distance: ignore fast-travel/respawn teleports (>1 km between polls) and the first fix after a gap
      if ($e.lastX -ne 0 -or $e.lastY -ne 0) {
        $d=[math]::Sqrt([math]::Pow($c.x-$e.lastX,2)+[math]::Pow($c.y-$e.lastY,2))/100.0   # UE units (cm) -> m
        if ($d -lt 1000 -and $delta -le 2) { $e.meters=[double]$e.meters + $d }
      }
      $e.lastX=$c.x; $e.lastY=$c.y
      if (-not $e.lvl7dAt -or ((Get-Date $e.lvl7dAt) -lt $now.AddDays(-7))) { $e.lvl7d=[int]$e.maxLevel; $e.lvl7dAt=$nowIso }
    }
  }
  foreach ($k in @($P.Keys)) { if ($P[$k].online -and -not $onlineNow.ContainsKey($k)) { $P[$k].online=$false } }
  if ($onlineNow.Count -gt $peak) { $peak=$onlineNow.Count }
}

# --- world size ---
$wkb = 0
if ($world -and (Test-Path $world)) { $wkb=[int][math]::Round((Get-Item $world).Length/1KB,0) }

# --- day/night clock: EXACT in-game time read straight from the save (pal-gametime.py) ---
# GameDateTimeTicks -> day + time-of-day. Ground truth: reflects sleep skips instantly, no anchor/assumption.
# Night is a game constant: 20:00 -> 05:00. Real-time-to-next-transition uses the measured tick advance rate.
$TPD  = 864000000000.0            # .NET ticks per in-game day
$DAWN = 5.0/24.0                  # 05:00 - day begins
$DUSK = 20.0/24.0                 # 20:00 - night begins
$DAYLEN = $DUSK - $DAWN           # 0.625 of the day is daylight

$gt=$null
try { $gtOut = & $py $gametime 2>$null
      if ($gtOut) { $gt = ("$gtOut" | ConvertFrom-Json) } } catch {}

$ticksNow=$null
if ($gt) { $ticksNow=[double]$gt.ticks }
elseif ($null -ne $lastTicks -and $ticksRate -and $lastPoll) { $ticksNow=[double]$lastTicks + $ticksRate*($now-$lastPoll).TotalMinutes }

if ($gt -and $null -ne $lastTicks -and $lastPoll) {
  $dR=($now-$lastPoll).TotalMinutes; $dT=[double]$gt.ticks-[double]$lastTicks
  if ($dR -gt 0.2 -and $dT -gt 0 -and $dT -lt $TPD*0.5) {
    $inst=$dT/$dR
    $ticksRate = if ($ticksRate) { 0.6*$ticksRate + 0.4*$inst } else { $inst }
  }
}

$days = if ($gt) { [int]$gt.day } elseif ($null -ne $ticksNow) { [int][math]::Floor($ticksNow/$TPD) } else { $lastDays }
$clock=$null
if ($null -ne $ticksNow) {
  $frac=(($ticksNow % $TPD)/$TPD); if ($frac -lt 0) { $frac+=1 }
  $isDay = ($frac -ge $DAWN -and $frac -lt $DUSK)
  $pos = $frac - $DAWN; if ($pos -lt 0) { $pos+=1 }   # position from dawn ($pos, NOT $p: $p==$P (player store) case-insensitively!)
  $toB = if ($isDay) { $DUSK-$frac } else { if ($frac -lt $DAWN) { $DAWN-$frac } else { (1.0-$frac)+$DAWN } }
  $cycleMin = if ($ticksRate) { $TPD/$ticksRate } else { 71.0 }
  $left = if ($ticksRate) { ($toB*$TPD)/$ticksRate } else { 0 }
  $hh=[int][math]::Floor($frac*24); $mm=[int][math]::Floor(($frac*24-$hh)*60)   # [int] alone ROUNDS in PS -> Floor
  $clock=[ordered]@{ day=[int]$days; phase=$(if($isDay){'day'}else{'night'})
                     hhmm=('{0:00}:{1:00}' -f $hh,$mm)
                     minutesLeft=[int][math]::Round($left,0); progress=[math]::Round($pos,3)
                     dayMinutes=[int][math]::Round($cycleMin*$DAYLEN,0); nightMinutes=[int][math]::Round($cycleMin*(1-$DAYLEN),0)
                     cycleMinutes=[int][math]::Round($cycleMin,0); source=$(if($gt){'save'}else{'extrapolated'}) }
}
if ($null -ne $ticksNow) { $lastTicks=$ticksNow }
if ($null -ne $days) { $lastDays=$days }

# --- append this poll to the 7d time-series ---
$samples.Add([pscustomobject]@{ t=$nowIso; n=[int]$onlineNow.Count; fps=$(if($up){[int]$metrics.serverfps}else{0})
                                days=$(if($null -ne $days){[int]$days}else{0}); wkb=$wkb; up=$up })
$cut=$now.AddDays(-7)
while ($samples.Count -gt 0 -and (Get-Date $samples[0].t) -lt $cut) { $samples.RemoveAt(0) }

# --- save persistent store --- .ToArray(); @($List[object]) throws "Argument types do not match" in ConvertTo-Json (PS 5.1)
$storeObj=@{ players=$P; samples=$samples.ToArray(); peakOnline=$peak
             lastPoll=$nowIso; lastDays=$lastDays; lastTicks=$lastTicks; ticksRate=$ticksRate }
$storeJson = $storeObj | ConvertTo-Json -Depth 6 -Compress
if ($storeJson) { [System.IO.File]::WriteAllText($store, $storeJson) }   # guard: never truncate the store

# --- downsample the series for publication ---
function Bucket($mins, $spanHours) {
  $from=$now.AddHours(-$spanHours); $res=New-Object System.Collections.Generic.List[object]
  $grp=@{}
  foreach($s in $samples){
    $t=Get-Date $s.t; if ($t -lt $from) { continue }
    $key=[int][math]::Floor(($t-$from).TotalMinutes/$mins)
    if (-not $grp.ContainsKey($key)) { $grp[$key]=@{ n=0; fps=0; c=0; up=0 } }
    $g=$grp[$key]; $g.c++; $g.up += [int][bool]$s.up
    if ($s.n   -gt $g.n)  { $g.n=[int]$s.n }
    if ($s.fps -gt $g.fps){ $g.fps=[int]$s.fps }
  }
  foreach($k in ($grp.Keys | Sort-Object)){
    $g=$grp[$k]
    $res.Add([pscustomobject]@{ t=$from.AddMinutes($k*$mins).ToString('o'); n=[int]$g.n
                                fps=[int]$g.fps; up=[math]::Round($g.up/[math]::Max($g.c,1),2) })
  }
  return $res.ToArray()
}
$hist24=Bucket 10 24
$hist7d=Bucket 60 168

function UptimePct($h) {
  $from=$now.AddHours(-$h); $tot=0; $ok=0
  foreach($s in $samples){ if ((Get-Date $s.t) -ge $from) { $tot++; if ($s.up) { $ok++ } } }
  if ($tot -eq 0) { return $null }
  return [math]::Round(100.0*$ok/$tot,1)
}

$wspark=New-Object System.Collections.Generic.List[object]
$seen=@{}
foreach($s in $samples){
  if ([int]$s.wkb -le 0) { continue }
  $t=Get-Date $s.t; $key=$t.ToString('yyyyMMddHH')
  if (-not $seen.ContainsKey($key)) { $seen[$key]=$true; $wspark.Add([pscustomobject]@{ t=$s.t; kb=[int]$s.wkb }) }
}

# --- published roster (no ids/ip/coords): online-first, then most-recently-seen ---
$roster = foreach ($k in $P.Keys) {
  $e=$P[$k]; $on=($up -and $e.online)
  [pscustomobject]@{ name=$e.name; level=[int]$e.maxLevel; playMinutes=[int][math]::Round([double]$e.playMinutes,0)
                     sessions=[int]$e.sessions; lastSeen=$e.lastSeen; online=$on
                     ping=$(if($on -and $onlineNow.ContainsKey($k)){$onlineNow[$k].ping}else{$null})
                     km=[math]::Round([double]$e.meters/1000.0,3)
                     levelGain=[math]::Max([int]$e.maxLevel-[int]$e.lvl7d,0) }
}
$roster = @($roster | Sort-Object @{e={$_.online};Descending=$true}, @{e={[datetime]$_.lastSeen};Descending=$true})

# --- settings: gameplay only (strip anything sensitive/identifying) ---
$drop=@('AdminPassword','ServerPassword','RCONEnabled','RCONPort','RESTAPIEnabled','RESTAPIPort','PublicPort','PublicIP','Region','BanListURL','bUseAuth','ServerName','ServerDescription')
$settings=[ordered]@{}; if ($settingsR){ foreach($prop in $settingsR.PSObject.Properties){ if ($drop -notcontains $prop.Name){ $settings[$prop.Name]=$prop.Value } } }

# --- world detail from the save parser (separate slower task); only if reasonably fresh ---
$save=$null
if (Test-Path $saveF) {
  try {
    $sv=Get-Content $saveF -Raw | ConvertFrom-Json
    if ($sv.parsedAt -and ((Get-Date) - (Get-Date $sv.parsedAt)).TotalHours -lt 6) { $save=$sv }
  } catch {}
}

$obj=[ordered]@{
  updated=$nowIso; up=$up
  info=$(if($info){[ordered]@{version=$info.version;servername=$info.servername;description=$info.description}}else{$null})
  metrics=$(if($metrics){[ordered]@{currentplayernum=[int]$metrics.currentplayernum;maxplayernum=[int]$metrics.maxplayernum;serverfps=[int]$metrics.serverfps;serverfpsaverage=[math]::Round([double]$metrics.serverfpsaverage,1);serverframetime=[math]::Round([double]$metrics.serverframetime,2);days=[int]$metrics.days;basecampnum=[int]$metrics.basecampnum;uptime=[int]$metrics.uptime}}else{$null})
  peakOnline=[int]$peak
  clock=$clock
  world=[ordered]@{ kb=$wkb; history=$wspark.ToArray() }
  history=[ordered]@{ day=$hist24; week=$hist7d }
  uptime=[ordered]@{ d1=(UptimePct 24); d7=(UptimePct 168) }
  roster=@($roster)
  settings=$settings
  save=$save
}
[System.IO.File]::WriteAllText($out, ($obj | ConvertTo-Json -Depth 8))

# --- weekly Pal bracket: draft / advance rounds -> bracket.json in $webDir (published with the rest) ---
# Idempotent and cheap; does nothing outside the draft/round boundaries. Skipped unless enabled in config.
# Failures here must never take the collector down, so it is best-effort and silent.
if ($cfg.bracketEnabled) {
  try { & (Join-Path $here 'pal-bracket.ps1') *> $null } catch { }
}

# --- optional publish: push the web dir to a remote host, run a copy command, etc. ---
# Leave publishCommand empty ("") to just serve $webDir with any local static web server.
# The command runs as-is in PowerShell; $out is the JSON path and $webDir is the folder to publish.
if ($cfg.publishCommand) { try { Invoke-Expression $cfg.publishCommand } catch {} }
