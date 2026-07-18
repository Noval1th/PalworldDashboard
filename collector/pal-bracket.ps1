<#  Weekly nicknamed-Pal popularity bracket ("Palapalooza") - the brain.
    Drafts a field of 8, advances rounds off the vote tallies, keeps history, writes bracket.json.
    Idempotent: safe to run every collector cycle (the collector calls it).

    Requires a vote endpoint that serves running tallies as JSON:
        { "<matchId>": { "a": <int>, "b": <int> }, ... }
    and accepts votes from the dashboard. Set voteTalliesUrl in config.json. Without it the bracket
    still drafts and advances, but every match resolves by coin-toss (no votes to count).

    Params (all optional; defaults come from config.json):
      -Now <datetime>   override "now" (test a week's progression without waiting a week)
      -VotesFile <path> read tallies from a local JSON file instead of the endpoint (testing)
      -DryRun           don't persist state or write bracket.json; just print what would happen
#>
param(
  [datetime]$Now = (Get-Date),
  [string]$VotesFile,
  [string]$StateFile,
  [string]$OutFile,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

# ---- config (config.json next to this script; copy config.example.json -> config.json first) ----
$here = $PSScriptRoot
$cfg  = Get-Content (Join-Path $here 'config.json') -Raw | ConvertFrom-Json

$SAVEJSON = Join-Path $cfg.dataDir 'palworld-save.json'    # written by pal-save-parse.py
if(-not $StateFile){ $StateFile = Join-Path $cfg.dataDir 'pal-bracket-state.json' }  # private, never served
if(-not $OutFile)  { $OutFile   = Join-Path $cfg.webDir  'bracket.json' }            # published

$VOTESURL   = $cfg.voteTalliesUrl                          # "" disables vote fetching
$FIELD      = if($cfg.bracketFieldSize){ [int]$cfg.bracketFieldSize } else { 8 }
$REUSEWEEKS = if($cfg.bracketReuseWeeks){ [int]$cfg.bracketReuseWeeks } else { 3 }
$SPECIALEVR = if($cfg.bracketSpecialEvery){ [int]$cfg.bracketSpecialEvery } else { 4 }
$CROWDFACT  = if($cfg.bracketCrowdFactor){ [double]$cfg.bracketCrowdFactor } else { 2.0 }
$DRAFTHOUR  = if($null -ne $cfg.bracketDraftHour){ [int]$cfg.bracketDraftHour } else { 8 }

# ---- helpers ----
function To-Hashtable($o){
  if($null -eq $o){ return $null }
  if($o -is [System.Collections.IDictionary]){ return $o }
  if($o -is [pscustomobject]){
    $h=@{}; foreach($p in $o.PSObject.Properties){ $h[$p.Name]=To-Hashtable $p.Value }; return $h
  }
  if($o -is [object[]]){ return @($o | ForEach-Object { To-Hashtable $_ }) }
  return $o
}
function Load-Json($path){
  if(-not (Test-Path $path)){ return $null }
  try { return To-Hashtable ((Get-Content $path -Raw -Encoding UTF8) | ConvertFrom-Json) } catch { return $null }
}
function Save-JsonNoBom($path,$obj){
  $json = $obj | ConvertTo-Json -Depth 20
  [IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
}
function Week-Sunday([datetime]$dt){          # date of the Sunday on/before $dt (midnight)
  $d = $dt.Date
  return $d.AddDays(-[int]$d.DayOfWeek)         # DayOfWeek Sunday=0
}
function ISO([datetime]$dt){ return $dt.ToString('s') }
function Make-Schedule([datetime]$sunday){
  return @{
    draft     = ISO $sunday.AddHours($DRAFTHOUR)          # Sun 08:00
    round1End = ISO $sunday.AddDays(2)                    # Tue 00:00
    round2End = ISO $sunday.AddDays(4)                    # Thu 00:00
    round3End = ISO $sunday.AddDays(6)                    # Sat 00:00
    revealAt  = ISO $sunday.AddDays(6)                    # Sat 00:00
    nextDraft = ISO $sunday.AddDays(7).AddHours($DRAFTHOUR)
  }
}
function Coin-A($matchId){                     # deterministic tie-break: true => side a
  $b = [Text.Encoding]::UTF8.GetBytes([string]$matchId)
  $h = [Security.Cryptography.SHA1]::Create().ComputeHash($b)
  return (($h[0] -band 1) -eq 0)
}
function Seed-From($s){
  $h=[Security.Cryptography.SHA1]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$s))
  return [BitConverter]::ToInt32($h,0)
}
function Fetch-Votes{
  if($VotesFile){ $v=Load-Json $VotesFile; if($null -eq $v){ return @{} } else { return $v } }
  if(-not $VOTESURL){ return @{} }        # no endpoint configured -> every match ties -> coin-toss
  try {
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $c=(Invoke-WebRequest ($VOTESURL+'?cb='+[Guid]::NewGuid()) -UseBasicParsing -Headers @{'User-Agent'='Mozilla/5.0';'Cache-Control'='no-cache'} -TimeoutSec 20).Content
    $h=To-Hashtable ($c | ConvertFrom-Json); if($null -eq $h){ return @{} } else { return $h }
  } catch { Write-Warning "vote fetch failed: $($_.Exception.Message)"; return @{} }
}
function VoteCount($votes,$mid,$side){
  if($votes.ContainsKey($mid) -and $votes[$mid].ContainsKey($side)){ return [int]$votes[$mid][$side] }
  return 0
}
function Snapshot($p){                          # frozen entrant view from a pal record
  return @{ pid=$p.pid; nick=$p.nick; species=$p.species; level=$p.level; owner=$p.owner;
            gender=$p.gender; iv=$p.iv; lucky=[bool]$p.lucky; alpha=[bool]$p.alpha; favorite=[bool]$p.favorite }
}

# ---- load state ----
$state = Load-Json $STATEFILE
if($null -eq $state){ $state = @{ weekCounter=0; current=$null; usedHistory=@{}; performers=@{} } }
foreach($k in 'weekCounter','current','usedHistory','performers'){ if(-not $state.ContainsKey($k)){ $state[$k] = if($k -eq 'weekCounter'){0}elseif($k -eq 'current'){$null}else{@{}} } }
$used = $state.usedHistory; $perf = $state.performers
if(-not $state.ContainsKey('champions')){ $state['champions']=@() }
$state.champions=@($state.champions)   # durable Hall-of-Champions roll (one per finished week)
# re-array anything ConvertTo-Json collapsed on the last save (single-element matches/rounds become scalars)
if($null -ne $state.current -and $state.current.ContainsKey('rounds')){
  $state.current.entrants = @($state.current.entrants)
  $state.current.rounds   = @($state.current.rounds)
  foreach($rd in $state.current.rounds){ $rd.matches = @($rd.matches) }
}

$sunday   = Week-Sunday $Now
$weekId   = $sunday.ToString('yyyy-MM-dd')
$draftDue = $sunday.AddHours($DRAFTHOUR)

function Recently-Used($ppid){
  if(-not $used.ContainsKey($ppid)){ return $false }
  try { $last=[datetime]$used[$ppid] } catch { return $false }
  return (($sunday - $last).TotalDays -lt ($REUSEWEEKS*7))
}
function Owner-Key($p,$i){
  $o=$p.owner
  # unowned/base Pals aren't a tamer, so give each its own bucket - they should never count as clumping
  if([string]::IsNullOrWhiteSpace([string]$o)){ return "~unowned:$i" }
  return [string]$o
}
# Weighted draw that spreads the field across as many tamers as possible: take at most one Pal per
# owner, then (only once every owner is tapped) allow a second each, and so on. Within every pass the
# pick is still the recency-weighted random draw, so newer Pals stay favoured.
function Sample-Spread($items,$weights,$n){
  $rem=@()
  for($i=0;$i -lt $items.Count;$i++){
    $rem+=@{ id=$i; item=$items[$i]; w=[double]$weights[$i]; owner=(Owner-Key $items[$i] $i) }
  }
  $out=@(); $taken=@{}; $cap=1
  while($out.Count -lt $n -and $rem.Count -gt 0){
    $elig=@($rem | Where-Object { $(if($taken.ContainsKey($_.owner)){$taken[$_.owner]}else{0}) -lt $cap })
    if($elig.Count -eq 0){ $cap++; continue }        # every owner is at cap -> open up another slot each
    $tot=0.0; foreach($e in $elig){ $tot+=$e.w }
    $r=Get-Random -Minimum 0.0 -Maximum $tot; $acc=0.0; $pick=$elig[0]
    foreach($e in $elig){ $acc+=$e.w; if($r -le $acc){ $pick=$e; break } }
    $out+=$pick.item
    if($taken.ContainsKey($pick.owner)){ $taken[$pick.owner]++ } else { $taken[$pick.owner]=1 }
    $rem=@($rem | Where-Object { $_.id -ne $pick.id })
  }
  return $out
}

# ---- draft a new field for $weekId ----
function Draft-Field($special){
  $save=Load-Json $SAVEJSON
  $pals=@(); if($save -and $save.ContainsKey('pals')){ $pals=@($save.pals | Where-Object { $_.pid }) }
  $byPid=@{}; foreach($p in $pals){ $byPid[$p.pid]=$p }

  $cands=@(); $usedSpecial=$special
  if($special){
    $c=@()
    foreach($ppid in @($perf.Keys)){
      if($byPid.ContainsKey($ppid)){ $c+=$byPid[$ppid] }
      elseif($perf[$ppid].ContainsKey('snapshot') -and $perf[$ppid].snapshot){ $c+=$perf[$ppid].snapshot }
    }
    if($c.Count -ge $FIELD){ $cands=$c } else { $usedSpecial=$false }   # not enough champions -> normal draft
  }
  if(-not $usedSpecial){
    $nick=@($pals | Where-Object { $_.nick -and -not (Recently-Used $_.pid) })
    $cands=$nick
    if($cands.Count -lt $FIELD){
      $fav=@($pals | Where-Object { $_.favorite -and -not $_.nick -and -not (Recently-Used $_.pid) })
      $cands=@($cands)+@($fav)
    }
  }
  if($cands.Count -lt $FIELD){ return @{ ok=$false; special=$usedSpecial } }   # rest week

  Get-Random -SetSeed (Seed-From $weekId) | Out-Null
  # recency weight: newest 'owned' gets highest weight (rank-based, robust to nulls)
  $sorted=@($cands | Sort-Object { if($_.owned){[double]$_.owned}else{0} } -Descending)
  $weights=@(); for($i=0;$i -lt $sorted.Count;$i++){ $weights+=($sorted.Count - $i) }
  $field=@(Sample-Spread $sorted $weights $FIELD)
  $field=@($field | Sort-Object { Get-Random })   # shuffle seeding order
  return @{ ok=$true; special=$usedSpecial; field=$field }
}

function Build-Bracket($draft){
  $wk=$weekId; $f=$draft.field
  $entrants=@($f | ForEach-Object { Snapshot $_ })
  $rounds=@(
    @{ round=1; matches=@(
       @{matchId="$wk`:r1:m0"; a=$f[0].pid; b=$f[1].pid; winner=$null},
       @{matchId="$wk`:r1:m1"; a=$f[2].pid; b=$f[3].pid; winner=$null},
       @{matchId="$wk`:r1:m2"; a=$f[4].pid; b=$f[5].pid; winner=$null},
       @{matchId="$wk`:r1:m3"; a=$f[6].pid; b=$f[7].pid; winner=$null}) },
    @{ round=2; matches=@(
       @{matchId="$wk`:r2:m0"; a=$null; b=$null; winner=$null},
       @{matchId="$wk`:r2:m1"; a=$null; b=$null; winner=$null}) },
    @{ round=3; matches=@(
       @{matchId="$wk`:r3:m0"; a=$null; b=$null; winner=$null}) }
  )
  return @{ weekId=$wk; generatedAt=ISO $Now; status='round1'; round=1; special=[bool]$draft.special;
            schedule=(Make-Schedule $sunday); entrants=$entrants; rounds=$rounds;
            champion=$null; message=$null; finalized=$false }
}

function Resolve-Match($m,$votes){
  if($null -ne $m.winner -or $null -eq $m.a -or $null -eq $m.b){ return }
  $va=VoteCount $votes $m.matchId 'a'; $vb=VoteCount $votes $m.matchId 'b'
  if($va -gt $vb){ $m.winner=$m.a } elseif($vb -gt $va){ $m.winner=$m.b }
  else { $m.winner = if(Coin-A $m.matchId){$m.a}else{$m.b} }   # tie / no votes -> deterministic coin
}
function Advance($b,$votes){
  $sc=$b.schedule; $r1=@($b.rounds[0].matches); $r2=@($b.rounds[1].matches); $r3=@($b.rounds[2].matches)
  if($Now -ge [datetime]$sc.round1End){ foreach($m in $r1){ Resolve-Match $m $votes }
    if($null -eq $r2[0].a){ $r2[0].a=$r1[0].winner; $r2[0].b=$r1[1].winner }
    if($null -eq $r2[1].a){ $r2[1].a=$r1[2].winner; $r2[1].b=$r1[3].winner } }
  if($Now -ge [datetime]$sc.round2End){ foreach($m in $r2){ Resolve-Match $m $votes }
    if($null -eq $r3[0].a){ $r3[0].a=$r2[0].winner; $r3[0].b=$r2[1].winner } }
  if($Now -ge [datetime]$sc.round3End){ Resolve-Match $r3[0] $votes; $b.champion=$r3[0].winner }
  # status
  if($b.status -ne 'rest'){
    if($Now -lt [datetime]$sc.round1End){ $b.status='round1'; $b.round=1 }
    elseif($Now -lt [datetime]$sc.round2End){ $b.status='round2'; $b.round=2 }
    elseif($Now -lt [datetime]$sc.round3End){ $b.status='round3'; $b.round=3 }
    else { $b.status='reveal'; $b.round=0 }
  }
}
function Finalize($b,$votes){
  if($b.finalized -or $b.status -eq 'rest'){ return }
  # per-(pal,match) vote counts this week -> median -> crowd-favorite flag
  $counts=@()
  foreach($rd in @($b.rounds)){ foreach($m in @($rd.matches)){ if($m.a){ $counts+=(VoteCount $votes $m.matchId 'a') }; if($m.b){ $counts+=(VoteCount $votes $m.matchId 'b') } } }
  $counts=@($counts | Where-Object { $_ -ge 0 })
  $median=0.0
  if($counts.Count){ $s=@($counts | Sort-Object); $median=[double]$s[[int][math]::Floor($s.Count/2)] }
  function Perf($ppid){ if(-not $perf.ContainsKey($ppid)){ $perf[$ppid]=@{finals=0;wins=0;crowdFav=$false;bestScore=0.0;snapshot=$null} }; return $perf[$ppid] }
  # champion + finalists
  $final=@($b.rounds[2].matches)[0]
  if($final.a){ $p=Perf $final.a; $p.finals++; $p.snapshot=($b.entrants | Where-Object {$_.pid -eq $final.a} | Select-Object -First 1) }
  if($final.b){ $p=Perf $final.b; $p.finals++; $p.snapshot=($b.entrants | Where-Object {$_.pid -eq $final.b} | Select-Object -First 1) }
  if($b.champion){ (Perf $b.champion).wins++
    $cs=@($b.entrants | Where-Object { $_.pid -eq $b.champion } | Select-Object -First 1)[0]
    if($cs){ $entry=@{ weekId=$b.weekId; date=(ISO $Now); special=[bool]$b.special; pid=$b.champion;
      nick=$cs.nick; species=$cs.species; level=$cs.level; owner=$cs.owner; gender=$cs.gender;
      iv=$cs.iv; lucky=[bool]$cs.lucky; alpha=[bool]$cs.alpha; favorite=[bool]$cs.favorite }
      $state.champions=@(@($state.champions) + $entry) }
  }
  # crowd favorites (any match's votes a clear outlier for the week)
  if($median -gt 0){
    foreach($rd in @($b.rounds)){ foreach($m in @($rd.matches)){
      foreach($side in 'a','b'){ $ppid=$m[$side]; if(-not $ppid){ continue }
        $v=VoteCount $votes $m.matchId $side; $score=[double]$v/$median
        if($v -ge ($CROWDFACT*$median)){ $pp=Perf $ppid; $pp.crowdFav=$true; if($score -gt $pp.bestScore){$pp.bestScore=$score}
          if(-not $pp.snapshot){ $pp.snapshot=($b.entrants | Where-Object {$_.pid -eq $ppid} | Select-Object -First 1) } }
      } } }
  }
  $b.finalized=$true
}

# ---- main flow ----
$votes = Fetch-Votes
$did = @()
if($null -ne $state.current){
  $b=$state.current
  Advance $b $votes
  if($Now -ge [datetime]$b.schedule.revealAt){ Finalize $b $votes }
}
# time to draft a new week? only within the draft window (Sun 08:00 -> Tue 00:00) so a late first run
# doesn't kick off a broken half-week; if we miss the window, we just wait for next Sunday.
$r1End = $sunday.AddDays(2)
if($Now -ge $draftDue -and $Now -lt $r1End -and (($null -eq $state.current) -or ($state.current.weekId -ne $weekId))){
  if($null -ne $state.current -and -not $state.current.finalized){ Finalize $state.current $votes }
  $special = (($state.weekCounter + 1) % $SPECIALEVR) -eq 0
  $draft = Draft-Field $special
  $state.weekCounter++
  if($draft.ok){
    $state.current = Build-Bracket $draft
    foreach($e in $state.current.entrants){ $used[$e.pid] = $weekId }   # stamp used
    $did += "drafted $weekId ($([int]$state.current.entrants.Count) entrants, special=$($state.current.special))"
    Advance $state.current $votes   # set correct status if we drafted late
  } else {
    $state.current = @{ weekId=$weekId; generatedAt=ISO $Now; status='rest'; round=0; special=[bool]$draft.special;
                        schedule=(Make-Schedule $sunday); entrants=@(); rounds=@(); champion=$null;
                        message='Not enough eligible Pals this week — the arena is taking the week off.'; finalized=$true }
    $did += "rest week $weekId (not enough Pals)"
  }
}

# ---- publish bracket.json (strip internal 'finalized') ----
$out = $null
if($null -ne $state.current){
  $out = @{}; foreach($k in $state.current.Keys){ if($k -ne 'finalized'){ $out[$k]=$state.current[$k] } }
  $out['champions'] = @($state.champions)   # publish the Hall-of-Champions roll alongside the current bracket
}

if($DryRun){
  Write-Host "=== DRY RUN @ $($Now.ToString('s')) (weekId $weekId) ==="
  if($did){ $did | ForEach-Object { Write-Host "  - $_" } }
  if($out){ Write-Host ($out | ConvertTo-Json -Depth 20) }
  Write-Host "weekCounter=$($state.weekCounter) used=$($used.Count) performers=$($perf.Count)"
} else {
  if($out){ Save-JsonNoBom $OUTFILE $out }
  Save-JsonNoBom $STATEFILE $state
  $st = if($null -ne $state.current){ $state.current.status } else { 'none' }
  $tag = if($out){ 'bracket.json written' } else { 'state saved (no active bracket)' }
  Write-Host "${tag}: weekId=$weekId status=$st $([string]::Join('; ',$did))"
}
