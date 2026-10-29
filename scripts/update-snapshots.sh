#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ROOT=${SNAPSHOT_ROOT:-${HOME:?HOME must be set}/work-agent-snapshots}
INDEX_ROOT=$ROOT/.index
CONFIG=${REPOS_CONFIG:-$REPO_DIR/config/repos.conf}
SSH_KEY=${GITHUB_SSH_KEY:-${HOME:?HOME must be set}/.ssh/work-agent-github}
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/work-agent
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

  # The CodeGraph index must be writable, but the snapshot is mounted read-only.
  # A relative symlink into $INDEX_ROOT keeps the index outside the read-only
  # tree while leaving it where CodeGraph expects it. Excluding it here (not via
  # a host-wide gitignore) keeps `clean -ffd` and `status --porcelain` correct
  # regardless of the host's dotfiles. The pattern has no trailing slash because
  # Git sees a symlink, not a directory.
  mkdir -p "$INDEX_ROOT/$name" "$target/.git/info"
  [[ -L "$target/.codegraph" ]] || ln -sfn "../.index/$name" "$target/.codegraph"
  grep -qxF '.codegraph' "$target/.git/info/exclude" 2>/dev/null \
    || printf '.codegraph\n' >> "$target/.git/info/exclude"

  indexed+=("$name")
  printf '%-20s %s %s\n' "$name" "$branch" "$(git -C "$target" rev-parse --short HEAD)"
done < "$CONFIG"

# Build the indexes with the runtime image so the host needs no Node toolchain.
# Before the first `deploy.sh` the image does not exist yet; that is not fatal.
if [[ ${#indexed[@]} -gt 0 ]]; then
  script=""
  for name in "${indexed[@]}"; do
    script+="if [ -s /home/node/code/.index/$name/codegraph.db ];"
    script+=" then codegraph sync /home/node/code/$name;"
    script+=" else codegraph init /home/node/code/$name; fi; "
  done
  # Plain `docker run` with no network instead of `docker compose run`: the bot service
  # pins ipv4_address 10.231.0.10 on work-agent-net, so a second container from the
  # same service would fail with "Address already in use" while the bot is up.
  # Indexing needs no network; the mounts mirror the bot's snapshot and .index mounts.
  image=$(SNAPSHOT_ROOT="$ROOT" docker compose -f "$REPO_DIR/compose.yaml" config --images backlog-agent)
  if docker run --rm --network none --read-only --tmpfs /tmp \
      --cap-drop ALL --security-opt no-new-privileges:true -e CODEGRAPH_TELEMETRY=0 \
      -v "$ROOT:/home/node/code:ro,z" -v "$INDEX_ROOT:/home/node/code/.index:z" \
      --entrypoint sh "$image" -c "$script"; then
    printf 'CodeGraph index updated for %d repos\n' "${#indexed[@]}"
  else
    printf 'CodeGraph index skipped (runtime image not built yet?)\n' >&2
  fi
fi
