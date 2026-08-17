#!/usr/bin/env bash

set -euo pipefail
umask 022

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${OUTPUT_DIR:-$repository_root/_repo}"
work_dir="$(mktemp -d)"
gnupg_home="$(mktemp -d)"
archive_url="${APT_REPOSITORY_URL:-https://apt.openresearchtools.com}"
metadata_suite="${APT_METADATA_SUITE:-apt/releases/download/repo/}"
github_organization="${GITHUB_ORGANIZATION:-openresearchtools}"
keyring_version="${KEYRING_VERSION:-2026.08.17}"
keyring_source_date_epoch="${KEYRING_SOURCE_DATE_EPOCH:-1786924800}"
archive_fingerprint="$(tr -d '[:space:]' < "$repository_root/keys/fingerprint.txt")"

cleanup() {
  find "$work_dir" "$gnupg_home" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

for command_name in \
  apt-ftparchive base64 curl dpkg dpkg-deb dpkg-scanpackages \
  gh gpg gpgv gzip jq; do
  require_command "$command_name"
done

case "$archive_url" in
  https://*) ;;
  *)
    printf 'APT_REPOSITORY_URL must use HTTPS: %s\n' "$archive_url" >&2
    exit 1
    ;;
esac

if [[ "$metadata_suite" == /* || "$metadata_suite" != */ ]]; then
  printf 'APT_METADATA_SUITE must be a relative path ending in /: %s\n' \
    "$metadata_suite" >&2
  exit 1
fi

if [[ ! "$keyring_source_date_epoch" =~ ^[0-9]+$ ]]; then
  printf 'KEYRING_SOURCE_DATE_EPOCH must be a non-negative integer: %s\n' \
    "$keyring_source_date_epoch" >&2
  exit 1
fi

find "$output_dir" -mindepth 1 -depth -delete 2>/dev/null || true
mkdir -p "$output_dir"
: > "$output_dir/Packages"

declare -A indexed_binary_versions=()

append_binary_records() {
  local package_dir="$1"
  local archive_prefix="$2"
  local records_file="$work_dir/binary-records.$RANDOM"
  local package_file package_name package_version package_architecture identity

  while IFS= read -r -d '' package_file; do
    package_name="$(dpkg-deb --field "$package_file" Package)"
    package_version="$(dpkg-deb --field "$package_file" Version)"
    package_architecture="$(dpkg-deb --field "$package_file" Architecture)"

    if [[ ! "$package_name" =~ ^[a-z0-9][a-z0-9+.-]+$ ]]; then
      printf 'Invalid package name in %s: %s\n' "$package_file" "$package_name" >&2
      exit 1
    fi
    if [[ ! "$package_architecture" =~ ^(all|amd64|arm64)$ ]]; then
      printf 'Unsupported package architecture in %s: %s\n' \
        "$package_file" "$package_architecture" >&2
      exit 1
    fi

    identity="$package_name|$package_version|$package_architecture"
    if [[ -n "${indexed_binary_versions[$identity]:-}" ]]; then
      printf 'Duplicate package/version/architecture: %s\n' "$identity" >&2
      exit 1
    fi
    indexed_binary_versions[$identity]="$package_file"
  done < <(find "$package_dir" -maxdepth 1 -type f -name '*.deb' -print0 | sort -z)

  (
    cd "$package_dir"
    dpkg-scanpackages --multiversion . /dev/null
  ) > "$records_file"

  if [[ ! -s "$records_file" ]]; then
    printf 'No Debian binary packages found in %s\n' "$package_dir" >&2
    exit 1
  fi

  awk -v prefix="$archive_prefix" '
    /^Filename: \.\// {
      sub(/^Filename: \.\//, "Filename: " prefix "/")
    }
    { print }
  ' "$records_file" >> "$output_dir/Packages"
}

append_remote_binary_record() {
  local asset_name="$1"
  local asset_url="$2"
  local asset_size="$3"
  local asset_digest="$4"
  local archive_prefix="$5"
  local control_deb control_file package_name package_version
  local package_architecture identity range_bytes asset_sha256

  if [[ ! "$asset_name" =~ ^[A-Za-z0-9][A-Za-z0-9._+~-]*\.deb$ ]]; then
    printf 'Unsupported Debian asset filename: %s\n' "$asset_name" >&2
    exit 1
  fi
  if [[ ! "$asset_url" =~ ^https://github\.com/ ]]; then
    printf 'Unexpected Debian asset URL: %s\n' "$asset_url" >&2
    exit 1
  fi
  if [[ ! "$asset_size" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid asset size for %s: %s\n' "$asset_name" "$asset_size" >&2
    exit 1
  fi
  if [[ ! "$asset_digest" =~ ^sha256:([0-9a-f]{64})$ ]]; then
    printf 'GitHub did not provide a SHA-256 digest for %s\n' "$asset_name" >&2
    exit 1
  fi
  asset_sha256="${BASH_REMATCH[1]}"

  control_deb="$(mktemp --suffix=.deb "$work_dir/control.XXXXXX")"
  control_file="$(mktemp "$work_dir/control-fields.XXXXXX")"
  for range_bytes in 1048576 4194304 16777216; do
    curl --fail --silent --show-error --location \
      --retry 3 --retry-all-errors \
      --range "0-$((range_bytes - 1))" \
      --max-filesize "$range_bytes" \
      "$asset_url" --output "$control_deb"
    if dpkg-deb --field "$control_deb" > "$control_file" 2>/dev/null; then
      break
    fi
    : > "$control_file"
  done
  if [[ ! -s "$control_file" ]]; then
    printf 'Could not read Debian control metadata from %s\n' "$asset_url" >&2
    exit 1
  fi

  package_name="$(dpkg-deb --field "$control_deb" Package)"
  package_version="$(dpkg-deb --field "$control_deb" Version)"
  package_architecture="$(dpkg-deb --field "$control_deb" Architecture)"
  if [[ ! "$package_name" =~ ^[a-z0-9][a-z0-9+.-]+$ ]]; then
    printf 'Invalid package name in %s: %s\n' "$asset_name" "$package_name" >&2
    exit 1
  fi
  if ! dpkg --validate-version "$package_version"; then
    printf 'Invalid package version in %s: %s\n' "$asset_name" "$package_version" >&2
    exit 1
  fi
  if [[ ! "$package_architecture" =~ ^(all|amd64|arm64)$ ]]; then
    printf 'Unsupported package architecture in %s: %s\n' \
      "$asset_name" "$package_architecture" >&2
    exit 1
  fi
  if awk -F: '
    $1 == "Filename" || $1 == "Size" || $1 == "MD5sum" ||
    $1 == "SHA1" || $1 == "SHA256" { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$control_file"; then
    printf 'Reserved repository fields found in %s control metadata\n' \
      "$asset_name" >&2
    exit 1
  fi

  identity="$package_name|$package_version|$package_architecture"
  if [[ -n "${indexed_binary_versions[$identity]:-}" ]]; then
    printf 'Duplicate package/version/architecture: %s\n' "$identity" >&2
    exit 1
  fi
  indexed_binary_versions[$identity]="$asset_url"

  cat "$control_file" >> "$output_dir/Packages"
  printf 'Filename: %s/%s\nSize: %s\nSHA256: %s\n\n' \
    "$archive_prefix" "$asset_name" "$asset_size" "$asset_sha256" \
    >> "$output_dir/Packages"
}

keyring_root="$work_dir/keyring-root"
keyring_packages="$work_dir/keyring-packages"
mkdir -p \
  "$keyring_root/DEBIAN" \
  "$keyring_root/etc/apt/sources.list.d" \
  "$keyring_root/usr/share/doc/openresearchtools-archive-keyring" \
  "$keyring_root/usr/share/keyrings" \
  "$keyring_packages"

install -m 0644 \
  "$repository_root/keys/openresearchtools-archive-keyring.gpg" \
  "$keyring_root/usr/share/keyrings/openresearchtools-archive-keyring.gpg"

cat > "$keyring_root/etc/apt/sources.list.d/openresearchtools.sources" <<EOF
Types: deb
URIs: $archive_url
Suites: $metadata_suite
Signed-By: /usr/share/keyrings/openresearchtools-archive-keyring.gpg
EOF

cat > "$keyring_root/DEBIAN/control" <<EOF
Package: openresearchtools-archive-keyring
Version: $keyring_version
Architecture: all
Maintainer: Open Research Tools <openresearchtools@users.noreply.github.com>
Depends: ca-certificates
Section: misc
Priority: optional
Homepage: https://github.com/openresearchtools/apt
Description: Open Research Tools APT archive key and source
 Installs the public signing key and APT source definition used to verify and
 download packages published by Open Research Tools.
EOF

cat > "$keyring_root/DEBIAN/conffiles" <<'EOF'
/etc/apt/sources.list.d/openresearchtools.sources
EOF

cat > "$work_dir/changelog" <<EOF
openresearchtools-archive-keyring ($keyring_version) stable; urgency=medium

  * Install the Open Research Tools archive key and source.

 -- Open Research Tools <openresearchtools@users.noreply.github.com>  Mon, 17 Aug 2026 00:00:00 +0000
EOF
gzip -n -9 < "$work_dir/changelog" \
  > "$keyring_root/usr/share/doc/openresearchtools-archive-keyring/changelog.gz"

cat > "$keyring_root/usr/share/doc/openresearchtools-archive-keyring/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: openresearchtools-archive-keyring
Source: https://github.com/openresearchtools/apt

Files: *
Copyright: 2026 Open Research Tools
License: CC0-1.0
 The public signing key and package metadata may be copied and redistributed
 without restriction under the Creative Commons CC0 1.0 Universal dedication.
EOF

keyring_filename="openresearchtools-archive-keyring_${keyring_version}_all.deb"
keyring_deb="$keyring_packages/$keyring_filename"
SOURCE_DATE_EPOCH="$keyring_source_date_epoch" \
  dpkg-deb --root-owner-group --build "$keyring_root" "$keyring_deb" >/dev/null
append_binary_records "$keyring_packages" "apt/releases/download/repo"
install -m 0644 "$keyring_deb" "$output_dir/$keyring_filename"
install -m 0644 "$keyring_deb" "$output_dir/openresearchtools-archive-keyring.deb"

while IFS= read -r package_spec; do
  package_repository="$(jq -r '.repository' <<<"$package_spec")"
  binary_asset_glob="$(jq -r '.binary_asset_glob // .asset_glob // empty' \
    <<<"$package_spec")"

  if [[ ! "$package_repository" =~ ^$github_organization/[A-Za-z0-9_.-]+$ ]]; then
    printf 'Invalid configured GitHub repository: %s\n' "$package_repository" >&2
    exit 1
  fi
  if [[ -z "$binary_asset_glob" ]]; then
    printf 'No binary_asset_glob configured for %s\n' "$package_repository" >&2
    exit 1
  fi

  repository_name="${package_repository#*/}"
  matched_repository_assets=0
  while IFS= read -r encoded_release; do
    release_json="$(base64 --decode <<<"$encoded_release")"
    release_tag="$(jq -r '.tag_name' <<<"$release_json")"
    matched_release_assets=0

    while IFS= read -r asset_spec; do
      asset_name="$(jq -r '.name' <<<"$asset_spec")"
      if [[ "$asset_name" != $binary_asset_glob ]]; then
        continue
      fi

      if [[ "$matched_release_assets" -eq 0 ]]; then
        if [[ ! "$release_tag" =~ ^[A-Za-z0-9._+~-]+$ ]]; then
          printf 'Release tag contains unsupported URL characters: %s\n' \
            "$release_tag" >&2
          exit 1
        fi
        archive_prefix="$repository_name/releases/download/$release_tag"
        printf 'Indexing %s release %s\n' "$package_repository" "$release_tag"
      fi

      append_remote_binary_record \
        "$asset_name" \
        "$(jq -r '.browser_download_url' <<<"$asset_spec")" \
        "$(jq -r '.size' <<<"$asset_spec")" \
        "$(jq -r '.digest // empty' <<<"$asset_spec")" \
        "$archive_prefix"
      matched_release_assets=$((matched_release_assets + 1))
      matched_repository_assets=$((matched_repository_assets + 1))
    done < <(jq -c '.assets[]' <<<"$release_json")
  done < <(
    gh api --paginate "repos/$package_repository/releases?per_page=100" \
      --jq '.[] | select(.draft == false) | @base64'
  )
  if [[ "$matched_repository_assets" -eq 0 ]]; then
    printf 'No published release assets matched %s in %s\n' \
      "$binary_asset_glob" "$package_repository" >&2
    exit 1
  fi
done < <(jq -c '.[]' "$repository_root/packages.json")

gzip -n -9 -c "$output_dir/Packages" > "$output_dir/Packages.gz"

temporary_release="$work_dir/Release"
(
  cd "$output_dir"
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin='Open Research Tools' \
    -o APT::FTPArchive::Release::Label='Open Research Tools' \
    -o "APT::FTPArchive::Release::Suite=$metadata_suite" \
    -o "APT::FTPArchive::Release::Codename=$metadata_suite" \
    -o APT::FTPArchive::Release::Architectures='amd64 arm64' \
    -o APT::FTPArchive::Release::Description='Open Research Tools packages' \
    release . > "$temporary_release"
)
install -m 0644 "$temporary_release" "$output_dir/Release"

install -m 0644 "$repository_root/keys/openresearchtools-archive-keyring.gpg" \
  "$output_dir/openresearchtools-archive-keyring.gpg"
install -m 0644 "$repository_root/keys/openresearchtools-archive-keyring.asc" \
  "$output_dir/openresearchtools-archive-keyring.asc"

if [[ "${SKIP_SIGNING:-0}" == "1" ]]; then
  printf 'Skipping repository signing because SKIP_SIGNING=1\n'
  exit 0
fi

if [[ -z "${APT_GPG_SIGNING_SUBKEY_B64:-}" || -z "${APT_KEY_PASSWORD:-}" ]]; then
  printf 'APT signing secrets are not available\n' >&2
  exit 1
fi

export GNUPGHOME="$gnupg_home"
encrypted_subkey="$work_dir/ci-signing-subkey.gpg"
printf '%s' "$APT_GPG_SIGNING_SUBKEY_B64" | base64 --decode > "$encrypted_subkey"
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 \
  --decrypt "$encrypted_subkey" 3<<<"$APT_KEY_PASSWORD" \
  | gpg --batch --quiet --import

gpg --batch --yes --local-user "$archive_fingerprint" --digest-algo SHA256 \
  --clearsign --output "$output_dir/InRelease" "$output_dir/Release"
gpg --batch --yes --armor --local-user "$archive_fingerprint" \
  --digest-algo SHA256 --detach-sign \
  --output "$output_dir/Release.gpg" "$output_dir/Release"

gpgv --keyring "$repository_root/keys/openresearchtools-archive-keyring.gpg" \
  "$output_dir/InRelease" >/dev/null

printf 'Built and signed external-asset APT repository at %s\n' "$output_dir"
