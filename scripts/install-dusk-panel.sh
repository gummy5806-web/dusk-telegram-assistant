#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
package_dir="$repo_dir/config/dusk-panel"
prebuilt_binary="$package_dir/Prebuilt/DuskPanel"
destination="${DUSK_APP_PATH:-$HOME/Applications/Dusk.app}"
install_root="$(dirname "$destination")"
lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

log() {
  printf '[dusk-panel] %s\n' "$*"
}

die() {
  printf '[dusk-panel] ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -f "$package_dir/Package.swift" ]] || die "Missing Swift package: $package_dir"
[[ -f "$package_dir/Resources/Info.plist" ]] || die "Missing Dusk Info.plist"

source_binary="$prebuilt_binary"
if [[ "${DUSK_BUILD_FROM_SOURCE:-0}" == 1 ]]; then
  swift_bin="$(xcrun --find swift 2>/dev/null || command -v swift || true)"
  [[ -n "$swift_bin" ]] || die "Swift is missing. Install Apple Command Line Tools with: xcode-select --install"
  log "Running DuskCore tests."
  "$swift_bin" run --package-path "$package_dir" DuskCoreTests
  log "Building release application."
  "$swift_bin" build --package-path "$package_dir" --configuration release --product DuskPanel
  bin_path="$("$swift_bin" build --package-path "$package_dir" --configuration release --show-bin-path)"
  source_binary="$bin_path/DuskPanel"
fi

[[ -x "$source_binary" ]] || die "Missing DuskPanel executable: $source_binary"

mkdir -p "$install_root"
staging_root="$(mktemp -d "$install_root/.dusk-install.XXXXXX")"
backup=""
destination_replaced=false
install_succeeded=false
existing_dusk_was_running=false

cleanup() {
  rm -rf "$staging_root"
  if [[ "$install_succeeded" == true ]]; then
    [[ -z "$backup" ]] || rm -rf "$backup"
    return
  fi

  if [[ "$destination_replaced" == true ]]; then
    /usr/bin/pkill -TERM -f -x "$destination/Contents/MacOS/DuskPanel" 2>/dev/null || true
    sleep 0.1
    /usr/bin/pkill -KILL -f -x "$destination/Contents/MacOS/DuskPanel" 2>/dev/null || true
    rm -rf "$destination"
  fi
  if [[ -n "$backup" && -d "$backup" ]]; then
    mv "$backup" "$destination"
    if [[ "$existing_dusk_was_running" == true ]]; then
      [[ ! -x "$lsregister" ]] || "$lsregister" -f "$destination"
      /usr/bin/open -gj "$destination" || true
    fi
  fi
}
trap cleanup EXIT

bundle="$staging_root/Dusk.app"
mkdir -p "$bundle/Contents/MacOS"
install -m 0755 "$source_binary" "$bundle/Contents/MacOS/DuskPanel"
install -m 0644 "$package_dir/Resources/Info.plist" "$bundle/Contents/Info.plist"

/usr/bin/plutil -lint "$bundle/Contents/Info.plist" >/dev/null
/usr/bin/codesign --force --deep --sign - "$bundle" >/dev/null
/usr/bin/codesign --verify --deep --strict "$bundle"

if /usr/bin/pgrep -x DuskPanel >/dev/null 2>&1; then
  existing_dusk_was_running=true
fi
/usr/bin/pkill -TERM -x DuskPanel 2>/dev/null || true
for _ in {1..20}; do
  /usr/bin/pgrep -x DuskPanel >/dev/null 2>&1 || break
  sleep 0.1
done
if /usr/bin/pgrep -x DuskPanel >/dev/null 2>&1; then
  /usr/bin/pkill -KILL -x DuskPanel 2>/dev/null || true
  sleep 0.1
fi
/usr/bin/pgrep -x DuskPanel >/dev/null 2>&1 && die "Existing DuskPanel process did not stop"

if [[ -d "$destination" ]]; then
  backup="$install_root/.Dusk.backup.$$"
  mv "$destination" "$backup"
fi

if ! mv "$bundle" "$destination"; then
  die "Could not install $destination"
fi
destination_replaced=true

if [[ -x "$lsregister" ]]; then
  "$lsregister" -f "$destination"
fi

/usr/bin/open -gj "$destination"
executable="$destination/Contents/MacOS/DuskPanel"
for _ in {1..30}; do
  if /usr/bin/pgrep -f -x "$executable" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

/usr/bin/pgrep -f -x "$executable" >/dev/null 2>&1 \
  || die "DuskPanel was installed but did not stay running"

healthcheck_dir="$HOME/Library/Caches/com.asan.dusk-panel"
healthcheck_file="$healthcheck_dir/healthcheck"
healthcheck_token="$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')"
mkdir -p "$healthcheck_dir"
rm -f "$healthcheck_file"
/usr/bin/open "dusk://capture?healthcheck=$healthcheck_token"

for _ in {1..50}; do
  /usr/bin/pgrep -f -x "$executable" >/dev/null 2>&1 \
    || die "DuskPanel exited during URL healthcheck"
  if [[ -f "$healthcheck_file" ]] \
    && [[ "$(<"$healthcheck_file")" == "$healthcheck_token" ]]; then
    install_succeeded=true
    log "Installed, URL routing verified, and running: $destination"
    exit 0
  fi
  sleep 0.1
done

die "DuskPanel did not confirm URL routing, visible panel, and input focus"
