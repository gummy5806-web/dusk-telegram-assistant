#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
package_dir="$repo_dir/config/dusk-panel"
prebuilt_dir="$package_dir/Prebuilt"
swift_bin="$(xcrun --find swift 2>/dev/null || command -v swift || true)"

[[ -n "$swift_bin" ]] || {
  printf '[dusk-build] ERROR: Swift is missing. Install Apple Command Line Tools.\n' >&2
  exit 1
}

"$swift_bin" run --package-path "$package_dir" DuskCoreTests
"$swift_bin" build --package-path "$package_dir" --configuration release --product DuskPanel \
  --triple arm64-apple-macosx13.0
"$swift_bin" build --package-path "$package_dir" --configuration release --product DuskPanel \
  --triple x86_64-apple-macosx13.0

arm_binary="$package_dir/.build/arm64-apple-macosx/release/DuskPanel"
intel_binary="$package_dir/.build/x86_64-apple-macosx/release/DuskPanel"
mkdir -p "$prebuilt_dir"
temporary="$(mktemp "$prebuilt_dir/.DuskPanel.XXXXXX")"
trap 'rm -f "$temporary"' EXIT

/usr/bin/lipo -create -output "$temporary" "$arm_binary" "$intel_binary"
chmod 0755 "$temporary"
/usr/bin/lipo "$temporary" -verify_arch arm64 x86_64
mv -f "$temporary" "$prebuilt_dir/DuskPanel"

printf '[dusk-build] Updated universal binary: %s\n' "$prebuilt_dir/DuskPanel"
