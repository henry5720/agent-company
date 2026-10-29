# Shared helpers. Source this, do not execute it.

# Read KEY=value pairs from the repo's .env without executing it, expanding the
# literal ${HOME} that .env.example ships with. Values already present in the
# environment win.
load_env() {
  local root=$1 line key value
  [[ -f "$root/.env" ]] || return 0
  while IFS= read -r line; do
    [[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key=${line%%=*}
    value=${line#*=}
    value=${value#\"}; value=${value%\"}
    value=${value#\'}; value=${value%\'}
    value=${value//'${HOME}'/$HOME}
    [[ -n ${!key:-} ]] && continue
    printf -v "$key" '%s' "$value"
    export "${key?}"
  done < "$root/.env"
}

# The deployment host's two scheduled jobs. Both markers are grep targets, so a
# reinstall rewrites our own lines and leaves every other crontab entry alone.
CRON_MARKER_SYNC='# agent-company-snapshots'
CRON_MARKER_CLEANUP='# agent-company-artifact-cleanup'

# Print the crontab lines the host is supposed to run. install-sync-cron.sh pipes
# this into `crontab -`; tests/static.sh renders it to prove the artifact cleanup
# is actually scheduled rather than only described in the docs. Keeping one
# renderer means the tested text is the installed text.
#
# Cleanup runs at :17 so it does not land on the sync run at :00, which rebuilds
# the CodeGraph indexes with a docker run and is the heavy job of the hour.
render_crontab() {
  local root=$1 snapshot_root=$2
  local sync_schedule=${3:-0 * * * *}
  local cleanup_schedule=${4:-17 * * * *}
  printf '%s SNAPSHOT_ROOT=%q %q %s\n' \
    "$sync_schedule" "$snapshot_root" "$root/scripts/update-snapshots.sh" "$CRON_MARKER_SYNC"
  printf '%s %q %s\n' \
    "$cleanup_schedule" "$root/scripts/cleanup-artifacts.sh" "$CRON_MARKER_CLEANUP"
}

# Codex 的 provider 設定不讀 env，所以 deploy 時把 gateway 位址填進範本。Key 不寫進檔案：
# Codex 用 env_key 從 env 讀。只換這一個 placeholder，其他內容原樣保留。
render_codex_config() {
  local template=$1 env_file=$2 out=$3 base
  base=$(grep -E '^COMPANY_GATEWAY_BASE_URL=' "$env_file" | tail -n1 | cut -d= -f2-)
  [[ $base =~ ^https://[^[:space:]\"]+[^/]$ ]] || {
    printf 'FAIL: COMPANY_GATEWAY_BASE_URL in %s must be an https URL without a trailing slash\n' "$env_file" >&2
    return 1
  }
  mkdir -p "$(dirname "$out")"
  python3 - "$template" "$out" "$base" <<'PY'
import sys, tomllib
template, out, base = sys.argv[1:]
text = open(template, encoding="utf-8").read()
assert text.count("${COMPANY_GATEWAY_BASE_URL}") == 1, "template must contain the placeholder exactly once"
text = text.replace("${COMPANY_GATEWAY_BASE_URL}", base)
assert tomllib.loads(text)["model_providers"]["company"]["base_url"] == base
open(out, "w", encoding="utf-8").write(text)
PY
}
