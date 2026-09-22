#!/usr/bin/env bash
# Exercise the real index writer without GitHub access or signing credentials.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
output_dir="$work_dir/output"
mkdir -p "$output_dir" "$work_dir/package/DEBIAN"
: > "$output_dir/Packages"
declare -A indexed_binary_versions=()
declare -A indexed_binary_contents=()
# Load only this function; sourcing the publisher itself would build/sign a repo.
source <(sed -n '/^append_remote_binary_record() {/,/^}/p' "$root/scripts/build-repository.sh")

make_package() {
  local architecture="$1"
  cat > "$work_dir/package/DEBIAN/control" <<EOF
Package: carry-forward-test
Version: 1.0
Architecture: $architecture
Maintainer: Test <test@example.invalid>
Description: Test fixture
EOF
  dpkg-deb --build --root-owner-group "$work_dir/package" "$work_dir/$architecture.deb" >/dev/null
}
make_package amd64
make_package arm64
fixture="$work_dir/amd64.deb"
# Simulate the downloaded control portion with the small complete fixture.
curl() {
  local destination=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == --output ]]; then destination="$2"; break; fi
    shift
  done
  test -n "$destination"
  cp "$fixture" "$destination"
}
size="$(stat -c %s "$fixture")"
digest="sha256:$(sha256sum "$fixture" | cut -d ' ' -f 1)"
append_remote_binary_record test.deb https://github.com/example/new/test.deb "$size" "$digest" example/new
append_remote_binary_record test.deb https://github.com/example/old/test.deb "$size" "$digest" example/old
test "$(grep -c '^Package:' "$output_dir/Packages")" = 1
grep -Fxq 'Filename: example/new/test.deb' "$output_dir/Packages"

if (append_remote_binary_record test.deb https://github.com/example/conflict/test.deb "$size" "sha256:$(printf '%064d' 0)" example/conflict); then
  echo 'A conflicting SHA-256 digest was incorrectly accepted' >&2; exit 1
fi
if (append_remote_binary_record test.deb https://github.com/example/conflict/test.deb "$((size + 1))" "$digest" example/conflict); then
  echo 'A conflicting size was incorrectly accepted' >&2; exit 1
fi
fixture="$work_dir/arm64.deb"
size="$(stat -c %s "$fixture")"
digest="sha256:$(sha256sum "$fixture" | cut -d ' ' -f 1)"
append_remote_binary_record test-arm64.deb https://github.com/example/new/test-arm64.deb "$size" "$digest" example/new
test "$(grep -c '^Package:' "$output_dir/Packages")" = 2
grep -Fxq 'Architecture: arm64' "$output_dir/Packages"
echo 'Identical copies deduplicated, conflicting copies rejected, ARM64 preserved.'
