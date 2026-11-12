# geo-mapping request kit @@KIT_ID@@ (WFS): READ ONLY, GET only. Windows PowerShell 5.1 or PowerShell 7.
#   powershell -ExecutionPolicy Bypass -File kit.ps1 [OutputFolder]
# Credentials, if the service needs them, come from YOUR environment (never paste them into a chat):
#   $env:GEO_USER and $env:GEO_PASS (HTTP Basic, sent only over https)   $env:GEO_MAX_TIME = seconds per request
#   $env:GEO_MAX_SECONDS = stop after this many seconds in total and exit 4. Run the same command again with the same folder to carry on: a run
#   that stopped (the time budget, a service that kept failing, a closed laptop) resumes after its last whole page.
# TLS verification stays on (a private certificate authority must be trusted by Windows). Redirects are not followed.
param([string]$Out = "")
$ErrorActionPreference = "Stop"
@@VARS@@
if (-not $Out) { $Out = "$KitId-out" }
New-Item -ItemType Directory -Force -Path (Join-Path $Out "pages") | Out-Null
$headers = @{ "User-Agent" = "geo-mapping-request-kit/1 (read-only)" }
if ($env:GEO_USER -and $env:GEO_PASS) {
  if (-not $Endpoint.ToLower().StartsWith("https://")) { throw "credentials are only sent over https; this endpoint is not https" }
  $headers["Authorization"] = "Basic " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($env:GEO_USER):$($env:GEO_PASS)"))
}
$timeout = 60; if ($env:GEO_MAX_TIME) { $timeout = [int]$env:GEO_MAX_TIME }
function Get-Data([hashtable]$Params, [string]$File) {
  $qs = ($Params.GetEnumerator() | ForEach-Object { [Uri]::EscapeDataString($_.Key) + "=" + [Uri]::EscapeDataString([string]$_.Value) }) -join "&"
  $url = $Endpoint + $(if ($Endpoint.Contains("?")) { "&" } else { "?" }) + $qs
  for ($try = 1; $try -le 4; $try++) {
    try {
      $r = Invoke-WebRequest -Uri $url -Method Get -Headers $headers -UseBasicParsing -MaximumRedirection 0 -TimeoutSec $timeout
      $bytes = $r.RawContentStream.ToArray()
      $head = [Text.Encoding]::UTF8.GetString($bytes, 0, [Math]::Min(300, $bytes.Length))
      if ($head -match "(?i)<!doctype html|<html") { throw "the service returned a web page instead of data (an error page, or a login page)" }
      [IO.File]::WriteAllBytes($File, $bytes); return
    } catch [System.Net.WebException] {
      $code = 0; if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
      if ($code -eq 401 -or $code -eq 403) { throw "the service answered ${code}: authentication failed (not retried, to avoid locking the account)" }
      if ($code -ge 300 -and $code -lt 400) { throw "the service redirected the request; redirects are not followed. Check the address" }
      if ($try -eq 4) { throw "the service kept failing after 4 tries ($code)" }
      Start-Sleep -Seconds ([Math]::Pow(2, $try))
    }
  }
}
function Count-Features([string]$File) { try { return @((Get-Content -Raw -Encoding UTF8 $File | ConvertFrom-Json).features).Count } catch { return 0 } }
$base = [ordered]@{ service = "WFS"; version = $Version; request = "GetFeature"; $TypeParam = $Layer; srsName = $Srs }
if ($Cql) { $base["CQL_FILTER"] = $Cql }
if ($BboxParam) { $base["bbox"] = $BboxParam }
$hits = $null
$statePath = Join-Path $Out "state.json"; $t0 = Get-Date; $resumed = $false
function Save-State { [IO.File]::WriteAllText($statePath, (@{ kit_id = $KitId; started = $started; hits = $hits; page = $page; received = $received; start = $start; n = $n } | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false))) }
if (Test-Path $statePath) {
  $st = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
  if ($st.kit_id -ne $KitId) { throw "this folder holds the state of another kit; use a fresh folder" }
  $started = [string]$st.started; $hits = $st.hits; $page = [int]$st.page; $received = [int]$st.received; $start = [int]$st.start; $n = [int]$st.n
  $resumed = $true; Write-Host "resuming after page $n ($received features so far)"
} else {
  $hp = @{} + $base; $hp["resultType"] = "hits"
  $hf = Join-Path $Out "hits.xml"; Get-Data $hp $hf
  $m = [regex]::Match((Get-Content -Raw $hf), '(numberMatched|numberOfFeatures)="(\d+)"'); if ($m.Success) { $hits = [int]$m.Groups[2].Value }
  Remove-Item $hf
  $started = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $page = $Page; $start = 0; $n = 0; $received = 0
}
$cap = $MaxF; if (-not $cap) { $cap = $hits }
if (-not $cap) { throw "the service does not say how many features match; set max_features in the plan, or narrow the filter" }
if (-not $resumed) { Write-Host "expected features: $(if ($null -ne $hits) { $hits } else { "unknown (capped at $cap)" })" }
$first = $true
while ($received -lt $cap -and $n -lt 5000) {
  if ($env:GEO_MAX_SECONDS -and -not $first -and ((Get-Date) - $t0).TotalSeconds -ge [double]$env:GEO_MAX_SECONDS) {
    Write-Host "time budget reached after page $n ($received so far): run the same command again to carry on"; exit 4
  }
  $first = $false
  $n++; $want = [Math]::Min($page, $cap - $received)
  $p = @{} + $base; $p["outputFormat"] = $(if ($Format) { $Format } else { "application/json" }); $p[$CountParam] = $want; $p["startIndex"] = $start
  if ($Sort) { $p["sortBy"] = $Sort }
  if ($Props) { $p["propertyName"] = $Props }
  $f = Join-Path $Out ("pages/page_{0:D4}.json" -f $n)
  try { Get-Data $p "$f.part" } catch {
    if (Test-Path "$f.part") { Remove-Item "$f.part" }
    $n--; Save-State
    Write-Host "stopped at page $($n + 1); run the same command again to carry on from there"
    throw
  }
  Move-Item -Force "$f.part" $f
  $c = Count-Features $f; $received += $c; $start += $c
  Write-Host "page ${n}: $c features ($received so far)"
  if ($c -gt 0 -and $c -lt $want -and $null -ne $hits -and $received -lt $hits) { $page = $c }
  Save-State                                      # after every whole page: a run that stops resumes here
  if ($c -eq 0) { break }
  if ($c -lt $want -and $null -eq $hits) { break }
}
$trunc = $false
if ($null -ne $hits -and $received -lt $hits -and -not $MaxF) { $trunc = $true; Write-Host "WARNING: received $received of $hits; the manifest records this" }
function Q([string]$s) { '"' + ($s -replace '\\', '\\\\' -replace '"', '\"') + '"' }
function NN($s) { if ($s) { Q $s } else { "null" } }
$files = @()
Get-ChildItem (Join-Path $Out "pages") -Filter "page_*.json" | Sort-Object Name | ForEach-Object {
  $files += "    - {name: " + (Q ("pages/" + $_.Name)) + ", bytes: " + $_.Length + ", sha256: " + (Q (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower()) + ", feature_count: " + (Count-Features $_.FullName) + "}"
}
$lines = @("manifest_version: 1", "kit_id: $(Q $KitId)", "created_by: plugin-kit", "service: {type: wfs, version: $(Q $Version), host_class: $HostClass}",
  "request:", "  operation: GetFeature", "  layer: $(Q $Layer)", "  filter: $(NN $RawCql)", "  bbox: $(if ($BboxJson) { $BboxJson } else { 'null' })", "  srs: $(Q $Srs)",
  "  properties: $(if ($PropsJson) { $PropsJson } else { 'null' })", "  sort_by: $(NN $Sort)", "  page_size: $Page",
  "run:", "  started_at: $(Q $started)", "  finished_at: $(Q ((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')))", "  tool: {shell: $(Q ('PowerShell ' + $PSVersionTable.PSVersion))}",
  "result:", "  hits_expected: $(if ($null -ne $hits) { $hits } else { 'null' })", "  total_features: $received", "  pages: $n", "  truncated: $($trunc.ToString().ToLower())", '  shortfall_note: ""', "  files:") + $files + @('attestation: {by: user, note: ""}')
[IO.File]::WriteAllText((Join-Path $Out "manifest.yaml"), (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
if (Test-Path $statePath) { Remove-Item $statePath }
Write-Host "done: $received features; send back the folder '$Out'"
if ($trunc) { exit 3 }
