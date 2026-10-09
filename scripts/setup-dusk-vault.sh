#!/usr/bin/env bash
set -euo pipefail

vault_dir="${DUSK_VAULT_PATH:-$HOME/Documents/obsidian/my dusk}"
vault_repo="${DUSK_VAULT_REPO:-https://github.com/gummy5806-web/dusk-vault.git}"
dispatcher="$vault_dir/bot/dispatch.py"
lock_dir="${vault_dir}.restore.lock"
lock_acquired=false
staging=""

log() {
  printf '[dusk-vault] %s\n' "$*"
}

die() {
  printf '[dusk-vault] ERROR: %s\n' "$*" >&2
  exit 1
}

make_private_directory() {
  mkdir -p "$vault_dir/Private"
  chmod 0700 "$vault_dir/Private"
}

if [[ -f "$dispatcher" ]]; then
  make_private_directory
  log "Ready: $vault_dir"
  exit 0
fi

command -v git >/dev/null 2>&1 || die "git is required to restore dusk-vault"

if [[ -e "$vault_dir" ]]; then
  die "$vault_dir exists but bot/dispatch.py is missing; move it aside or restore the vault manually"
fi

parent="$(dirname "$vault_dir")"
mkdir -p "$parent"
cleanup() {
  [[ -z "$staging" ]] || rm -rf "$staging"
  if [[ "$lock_acquired" == true ]]; then
    rm -rf "$lock_dir"
  fi
}
trap cleanup EXIT

for _ in {1..120}; do
  if mkdir "$lock_dir" 2>/dev/null; then
    lock_acquired=true
    printf '%s\n' "$$" >"$lock_dir/pid"
    break
  fi

  lock_owner="$(cat "$lock_dir/pid" 2>/dev/null || true)"
  if [[ "$lock_owner" =~ ^[0-9]+$ ]] && ! kill -0 "$lock_owner" 2>/dev/null; then
    stale_lock="${lock_dir}.stale.$$"
    if mv "$lock_dir" "$stale_lock" 2>/dev/null; then
      rm -rf "$stale_lock"
    fi
    continue
  fi
  sleep 0.25
done

[[ "$lock_acquired" == true ]] || die "timed out waiting for vault restore lock: $lock_dir"

if [[ -f "$dispatcher" ]]; then
  make_private_directory
  log "Ready: $vault_dir"
  exit 0
fi

if [[ -e "$vault_dir" ]]; then
  die "$vault_dir exists but bot/dispatch.py is missing; move it aside or restore the vault manually"
fi

staging="$(mktemp -d "$parent/.dusk-vault-clone.XXXXXX")"

log "Cloning private vault from $vault_repo"
git clone --quiet "$vault_repo" "$staging" || die "clone failed; authenticate GitHub access and rerun"
[[ -f "$staging/bot/dispatch.py" ]] || die "cloned repository does not contain bot/dispatch.py"

mv "$staging" "$vault_dir"
staging=""
make_private_directory
log "Restored: $vault_dir"
