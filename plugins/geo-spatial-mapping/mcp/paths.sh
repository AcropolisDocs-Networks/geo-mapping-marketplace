# Sourced by launch.sh and setup.sh: where the private environment lives on macOS and Linux, and what the server must be able to import.
# macOS: ~/Library/Application Support/AcropolisDocs/geo-mapping; Linux: $XDG_DATA_HOME (or ~/.local/share)/acropolisdocs/geo-mapping.
case "$(uname -s 2>/dev/null)" in
  Darwin) BASE="$HOME/Library/Application Support/AcropolisDocs/geo-mapping" ;;
  *)      BASE="${XDG_DATA_HOME:-$HOME/.local/share}/acropolisdocs/geo-mapping" ;;
esac
[ -n "${GEO_MAPPING_ENV_HOME:-}" ] && BASE="$GEO_MAPPING_ENV_HOME"     # tests, and a machine whose home folder is read-only
VENV="$BASE/venv"
VENVPY="$VENV/bin/python"
STAMP="$BASE/requirements.installed"
FULLSTAMP="$BASE/full.installed"
IMPORTS="import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl"
