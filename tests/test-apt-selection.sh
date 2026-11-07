#!/usr/bin/env bash
# Exercise the real manifest loop without GitHub access or signing credentials.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
repository_root="$work_dir"
github_organization=openresearchtools
channel=repo
prerelease=false
loop="$work_dir/manifest-loop.sh"
sed -n '/^while IFS= read -r package_spec; do$/,/^done < <(jq -c .*packages.json.*)$/p' \
  "$root/scripts/build-repository.sh" > "$loop"
test -s "$loop"

gh() {
  printf '%s\n' "$*" >> "$work_dir/github-calls"
  cat <<'JSON'
[{"draft":false,"prerelease":false,"tag_name":"v1.0","assets":[
  {"name":"required_1.0_amd64.deb","browser_download_url":"https://github.com/openresearchtools/required/releases/download/v1.0/required_1.0_amd64.deb","size":100,"digest":"sha256:fixture"}
]}]
JSON
}
append_remote_binary_record() {
  printf '%s\n' "$1" >> "$work_dir/indexed-assets"
}

cat > "$work_dir/packages.json" <<'JSON'
[
  {"repository":"openresearchtools/native-only","apt":false},
  {"repository":"openresearchtools/required","binary_asset_glob":"required_*.deb"}
]
JSON
(source "$loop")
test "$(wc -l < "$work_dir/github-calls")" = 1
grep -Fq 'repos/openresearchtools/required/releases' "$work_dir/github-calls"
grep -Fxq 'required_1.0_amd64.deb' "$work_dir/indexed-assets"

printf '%s\n' '[{"repository":"openresearchtools/required","binary_asset_glob":"typo_*.deb"}]' \
  > "$work_dir/packages.json"
if (source "$loop"); then
  echo 'Missing required Debian assets were incorrectly accepted' >&2; exit 1
fi

for apt_value in true '"false"'; do
  printf '[{"repository":"openresearchtools/required","apt":%s}]\n' "$apt_value" \
    > "$work_dir/packages.json"
  if (source "$loop"); then
    echo 'A required Debian asset glob was incorrectly optional' >&2; exit 1
  fi
done

echo 'Explicit native-only repositories skipped; required Debian configuration and assets remain mandatory.'
