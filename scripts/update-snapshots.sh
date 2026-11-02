#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib.sh
source "$REPO_DIR/scripts/lib.sh"
load_env "$REPO_DIR"
ROOT=${SNAPSHOT_ROOT:-${HOME:?HOME must be set}/agent-company-snapshots}
INDEX_ROOT=$ROOT/.index
CONFIG=${REPOS_CONFIG:-$REPO_DIR/config/repos.conf}
SSH_KEY=${GITHUB_SSH_KEY:-${HOME:?HOME must be set}/.ssh/work-agent-github}
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/agent-company
LOCK=${SNAPSHOT_LOCK:-$STATE_DIR/snapshots.lock}
LOG=${SNAPSHOT_LOG:-$STATE_DIR/snapshots.log}
LOG_MAX_BYTES=${SNAPSHOT_LOG_MAX_BYTES:-1048576}

mkdir -p "$STATE_DIR"

# cron has no journald: keep one rotation of our own log, but stay on the
# terminal when a maintainer runs this by hand.
if [[ ! -t 1 ]]; then
  if [[ -f "$LOG" && $(stat -c '%s' "$LOG") -gt $LOG_MAX_BYTES ]]; then
    mv -f "$LOG" "$LOG.1"
  fi
  exec >>"$LOG" 2>&1
fi
printf '=== %s\n' "$(date -Is)"

if [[ ! -r "$SSH_KEY" ]]; then
  printf 'Missing readable GitHub SSH key: %s\n' "$SSH_KEY" >&2
  exit 1
fi
if [[ ! -r "$CONFIG" ]]; then
  printf 'Missing repo config: %s\n' "$CONFIG" >&2
  exit 1
fi

mkdir -p "$ROOT" "$INDEX_ROOT"
exec 9>"$LOCK"
flock -n 9 || exit 0

indexed=()
export GIT_SSH_COMMAND="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

while IFS='|' read -r name url branch; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  if [[ ! "$name" =~ ^[A-Za-z0-9._-]+$ || ! "$branch" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    printf 'Invalid repo entry: %s|%s|%s\n' "$name" "$url" "$branch" >&2
    exit 1
  fi

  target="$ROOT/$name"
  if [[ ! -d "$target/.git" ]]; then
    rm -rf "$target"
    git clone --no-checkout "$url" "$target"
  fi

  git -C "$target" remote set-url origin "$url"
  git -C "$target" fetch --prune --tags origin '+refs/heads/*:refs/remotes/origin/*'
  git -C "$target" checkout -B "$branch" "origin/$branch"
  git -C "$target" reset --hard "origin/$branch"
  git -C "$target" clean -ffd

  # Keep repo-owned .codegraph files intact. Both the indexer and the agent use
  # CODEGRAPH_DIR=.codegraph-company; only its external index target is writable.
  mkdir -p "$INDEX_ROOT/$name" "$target/.git/info"
  ln -sfnT "../.index/$name" "$target/.codegraph-company"
  grep -qxF '.codegraph-company' "$target/.git/info/exclude" 2>/dev/null \
    || printf '.codegraph-company\n' >> "$target/.git/info/exclude"

  indexed+=("$name")
  printf '%-20s %s %s\n' "$name" "$branch" "$(git -C "$target" rev-parse --short HEAD)"
done < "$CONFIG"

# Build the indexes with the runtime image so the host needs no Node toolchain.
# Before the first `deploy.sh` the image does not exist yet; that is not fatal.
if [[ ${#indexed[@]} -gt 0 ]]; then
  script="set -e; "
  for name in "${indexed[@]}"; do
    script+="if [ -s /home/node/snapshots/.index/$name/codegraph.db ];"
    script+=" then codegraph sync /home/node/snapshots/$name;"
    script+=" else codegraph init /home/node/snapshots/$name; fi; "
  done
  # Plain `docker run` with no network instead of `docker compose run`: indexing needs no
  # network or home volume, only the bot's snapshot and .index mounts mirrored here.
  # config --images agent also includes dependency images on some Compose versions.
  image=$(SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" config --format json |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["agent"]["image"])')
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    printf 'CodeGraph index skipped (runtime image not built yet)\n' >&2
    exit 0
  fi
  if docker run --rm --network none --read-only --tmpfs /tmp \
      --cap-drop ALL --security-opt no-new-privileges:true -e CODEGRAPH_TELEMETRY=0 -e CODEGRAPH_DIR=.codegraph-company \
      -v "$ROOT:/home/node/snapshots:ro,z" -v "$INDEX_ROOT:/home/node/snapshots/.index:z" \
      --entrypoint sh "$image" -c "$script"; then
    printf 'CodeGraph index updated for %d repos\n' "${#indexed[@]}"
  else
    printf 'FAILED: CodeGraph index update\n' >&2
    exit 1
  fi
fi

# agent-config 可能剛更新：把 skills、MCP 與規則重新對齊進正在跑的 bot（跟啟動時的 pre_boot
# 同一支）。bot 沒在跑就略過，下次啟動的 pre_boot 會做。
if [[ -n $(SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" ps --status running -q agent 2>/dev/null) ]]; then
  if ! SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" exec -T agent agent-sync; then
    printf 'FAILED: agent-sync in the running bot; skills/MCP/rules may be stale\n' >&2
    exit 1
  fi
fi
