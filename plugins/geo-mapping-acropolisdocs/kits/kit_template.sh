#!/usr/bin/env bash
# geo-mapping request kit @@KIT_ID@@ (WFS): READ ONLY, GET only. Needs bash and curl (jq optional).
#   bash kit.sh [OUTPUT_FOLDER]
# Credentials, if the service needs them, come from YOUR environment (never paste them into a chat):
#   GEO_USER and GEO_PASS (HTTP Basic, sent only over https)   GEO_CACERT = a CA file for a private certificate authority   GEO_MAX_TIME = seconds per request
# TLS verification stays on. Redirects are not followed. Send the output folder back; manifest.yaml lets the plugin check it.
set -u
@@VARS@@
OUT="${1:-$KIT_ID-out}"
mkdir -p "$OUT/pages" || exit 1
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
CFG=""
if [ -n "${GEO_USER:-}" ] && [ -n "${GEO_PASS:-}" ]; then
  case "$ENDPOINT" in https://*) ;; *) echo "credentials are only sent over https; this endpoint is not https" >&2; exit 2;; esac
  CFG=$(printf 'user = "%s:%s"' "$(esc "$GEO_USER")" "$(esc "$GEO_PASS")")
fi
get() {   # get OUTFILE curl-args...   prints the HTTP status
  local out="$1"; shift
  local tries=0 code=000
  while [ $tries -lt 4 ]; do
    code=$(printf '%s\n' "$CFG" | curl -sS -K - -G "$ENDPOINT" --max-time "${GEO_MAX_TIME:-60}" ${GEO_CACERT:+--cacert "$GEO_CACERT"} \
        -A "geo-mapping-request-kit/1 (read-only)" "$@" -o "$out" -w '%{http_code}' 2>/dev/null) || code=000
    case "$code" in
      200) if head -c 300 "$out" | grep -qiE '<!doctype html|<html'; then echo "the service returned a web page instead of data (an error page, or a login page)" >&2; exit 4; fi
           echo 200; return 0;;
      401|403) echo "the service answered $code: authentication failed (not retried, to avoid locking the account)" >&2; exit 5;;
      429|500|502|503|504) tries=$((tries+1)); sleep $((2 ** tries));;
      3??) echo "the service redirected the request; redirects are not followed. Check the address" >&2; exit 4;;
      *) echo "the service answered $code" >&2; exit 4;;
    esac
  done
  echo "the service kept failing after 4 tries" >&2; exit 4
}
params=( --data-urlencode "service=WFS" --data-urlencode "version=$VERSION" --data-urlencode "request=GetFeature" --data-urlencode "$TYPEPARAM=$LAYER" --data-urlencode "srsName=$SRS" )
[ -n "$CQL" ] && params+=( --data-urlencode "CQL_FILTER=$CQL" )
[ -n "$BBOXP" ] && params+=( --data-urlencode "bbox=$BBOXP" )
count_features() { if command -v jq >/dev/null 2>&1; then jq '.features|length' "$1" 2>/dev/null || echo 0; else grep -o '"type" *: *"Feature"' "$1" | wc -l | tr -d ' '; fi; }
hits=""
get "$OUT/hits.xml" "${params[@]}" --data-urlencode "resultType=hits" >/dev/null && hits=$(grep -oE '(numberMatched|numberOfFeatures)="[0-9]+"' "$OUT/hits.xml" | head -1 | grep -oE '[0-9]+')
rm -f "$OUT/hits.xml"
cap="$MAXF"; [ -z "$cap" ] && cap="$hits"
if [ -z "$cap" ]; then echo "the service does not say how many features match; set max_features in the plan, or narrow the filter" >&2; exit 4; fi
echo "expected features: ${hits:-unknown (capped at $cap)}"
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
page="$PAGE"; start=0; n=0; received=0
while [ "$received" -lt "$cap" ] && [ $n -lt 5000 ]; do
  n=$((n+1)); want=$page; [ $((cap - received)) -lt "$want" ] && want=$((cap - received))
  f=$(printf '%s/pages/page_%04d.json' "$OUT" "$n")
  a=( "${params[@]}" --data-urlencode "outputFormat=${FORMAT:-application/json}" --data-urlencode "$COUNTPARAM=$want" --data-urlencode "startIndex=$start" )
  [ -n "$SORT" ] && a+=( --data-urlencode "sortBy=$SORT" )
  [ -n "$PROPS" ] && a+=( --data-urlencode "propertyName=$PROPS" )
  get "$f" "${a[@]}" >/dev/null
  c=$(count_features "$f"); received=$((received + c)); start=$((start + c))
  echo "page $n: $c features ($received so far)"
  [ "$c" -eq 0 ] && break
  [ "$c" -lt "$want" ] && [ -n "$hits" ] && [ "$received" -lt "$hits" ] && page="$c"
  [ "$c" -lt "$want" ] && [ -z "$hits" ] && break
done
trunc=false; note=""
if [ -n "$hits" ] && [ "$received" -lt "$hits" ] && [ -z "$MAXF" ]; then trunc=true; echo "WARNING: received $received of $hits; the manifest records this"; fi
if command -v jq >/dev/null 2>&1; then jq -s '{type:"FeatureCollection",features:(map(.features)|add)}' "$OUT"/pages/page_*.json > "$OUT/result.geojson"; fi
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
size() { wc -c < "$1" | tr -d ' '; }
q() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }
nn() { if [ -z "$1" ]; then printf 'null'; else q "$1"; fi; }
{
  echo "manifest_version: 1"; echo "kit_id: $(q "$KIT_ID")"; echo "created_by: plugin-kit"
  echo "service: {type: wfs, version: $(q "$VERSION"), host_class: $HOSTCLASS}"
  echo "request:"; echo "  operation: GetFeature"; echo "  layer: $(q "$LAYER")"; echo "  filter: $(nn "$RAWCQL")"
  echo "  bbox: ${BBOXJSON:-null}"; echo "  srs: $(q "$SRS")"; echo "  properties: ${PROPSJSON:-null}"; echo "  sort_by: $(nn "$SORT")"; echo "  page_size: $PAGE"
  echo "run:"; echo "  started_at: $(q "$STARTED")"; echo "  finished_at: $(q "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"; echo "  tool: {shell: $(q "bash $BASH_VERSION"), curl: $(q "$(curl --version | head -1 | cut -d' ' -f2)")}"
  echo "result:"; echo "  hits_expected: ${hits:-null}"; echo "  total_features: $received"; echo "  pages: $n"; echo "  truncated: $trunc"; echo "  shortfall_note: $(q "$note")"; echo "  files:"
  for p in "$OUT"/pages/page_*.json; do echo "    - {name: $(q "pages/$(basename "$p")"), bytes: $(size "$p"), sha256: $(q "$(sha "$p")"), feature_count: $(count_features "$p")}"; done
  if [ -f "$OUT/result.geojson" ]; then echo "    - {name: \"result.geojson\", bytes: $(size "$OUT/result.geojson"), sha256: $(q "$(sha "$OUT/result.geojson")"), feature_count: $(count_features "$OUT/result.geojson"), crs: $(q "$SRS")}"; fi
  echo 'attestation: {by: user, note: ""}'
} > "$OUT/manifest.yaml"
echo "done: $received features; send back the folder '$OUT'"
[ "$trunc" = true ] && exit 3
exit 0
