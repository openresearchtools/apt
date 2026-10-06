#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if "$@"; then return 0; fi
    if [[ "$attempt" -eq 6 ]]; then return 1; fi
    sleep "$((attempt * 5))"
  done
}

for channel in repo nightly; do
  output_dir="$repository_root/_repo/$channel"
  if ! gh release view "$channel" >/dev/null 2>&1; then
    flags=()
    if [[ "$channel" == nightly ]]; then flags+=(--prerelease); fi
    gh release create "$channel" \
      --title "Open Research Tools APT $channel" \
      --notes 'Machine-generated signed APT repository assets.' \
      --latest=false "${flags[@]}"
  fi

  published_assets="$(gh api "repos/$GITHUB_REPOSITORY/releases/tags/$channel" --jq '.assets')"
  while IFS= read -r published_asset; do
    if [[ ! -f "$output_dir/$published_asset" ]]; then
      retry gh release delete-asset "$channel" "$published_asset" --yes
    fi
  done < <(jq -r '.[].name' <<<"$published_assets")

  # Make every setup package available before publishing an index that uses it.
  while IFS= read -r -d '' asset; do
    asset_name="$(basename "$asset")"
    local_digest="sha256:$(sha256sum "$asset" | cut -d ' ' -f 1)"
    published_digest="$(jq -r --arg name "$asset_name" \
      '.[] | select(.name == $name) | .digest // empty' <<<"$published_assets")"
    if [[ "$local_digest" == "$published_digest" ]]; then
      printf 'Release asset is unchanged: %s/%s\n' "$channel" "$asset_name"
      continue
    fi
    retry gh release upload "$channel" "$asset" --clobber
  done < <(find "$output_dir" -maxdepth 1 -type f \
    ! -name 'Packages' ! -name 'Packages.gz' ! -name 'Release' \
    ! -name 'Release.gpg' ! -name 'InRelease' -print0)

  retry gh release upload "$channel" \
    "$output_dir/Packages" "$output_dir/Packages.gz" \
    "$output_dir/Release" "$output_dir/Release.gpg" "$output_dir/InRelease" \
    --clobber
done
