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
  apt-ftparchive base64 dpkg-deb dpkg-scanpackages dpkg-scansources \
  gh gpg gpgv gzip jq md5sum sha1sum sha256sum; do
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

find "$output_dir" -mindepth 1 -depth -delete 2>/dev/null || true
mkdir -p "$output_dir"
: > "$output_dir/Packages"
: > "$output_dir/Sources"

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

append_source_records() {
  local source_dir="$1"
  local archive_prefix="$2"
  local records_file="$work_dir/source-records.$RANDOM"

  (
    cd "$source_dir"
    dpkg-scansources . /dev/null
  ) > "$records_file"

  if [[ ! -s "$records_file" ]]; then
    printf 'No Debian source packages found in %s\n' "$source_dir" >&2
    exit 1
  fi

  awk -v prefix="$archive_prefix" '
    /^Directory:/ {
      print "Directory: " prefix
      next
    }
    { print }
  ' "$records_file" >> "$output_dir/Sources"
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
Types: deb deb-src
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
dpkg-deb --root-owner-group --build "$keyring_root" "$keyring_deb" >/dev/null
append_binary_records "$keyring_packages" "apt/releases/download/repo"
install -m 0644 "$keyring_deb" "$output_dir/$keyring_filename"
install -m 0644 "$keyring_deb" "$output_dir/openresearchtools-archive-keyring.deb"

while IFS= read -r package_spec; do
  package_repository="$(jq -r '.repository' <<<"$package_spec")"
  binary_asset_glob="$(jq -r '.binary_asset_glob // .asset_glob // empty' \
    <<<"$package_spec")"
  download_dir="$(mktemp -d "$work_dir/download.XXXXXX")"

  if [[ ! "$package_repository" =~ ^$github_organization/[A-Za-z0-9_.-]+$ ]]; then
    printf 'Invalid configured GitHub repository: %s\n' "$package_repository" >&2
    exit 1
  fi
  if [[ -z "$binary_asset_glob" ]]; then
    printf 'No binary_asset_glob configured for %s\n' "$package_repository" >&2
    exit 1
  fi

  repository_name="${package_repository#*/}"
  release_tag="$(gh api "repos/$package_repository/releases/latest" --jq '.tag_name')"
  if [[ ! "$release_tag" =~ ^[A-Za-z0-9._+~-]+$ ]]; then
    printf 'Release tag contains unsupported URL characters: %s\n' "$release_tag" >&2
    exit 1
  fi
  archive_prefix="$repository_name/releases/download/$release_tag"

  printf 'Indexing %s release %s\n' "$package_repository" "$release_tag"
  gh release download "$release_tag" --repo "$package_repository" \
    --pattern "$binary_asset_glob" --dir "$download_dir"
  append_binary_records "$download_dir" "$archive_prefix"

  source_patterns="$(jq -r '.source_asset_globs[]? // empty' <<<"$package_spec")"
  if [[ -z "$source_patterns" ]]; then
    printf 'No source_asset_globs configured for %s\n' "$package_repository" >&2
    exit 1
  fi
  while IFS= read -r source_pattern; do
    gh release download "$release_tag" --repo "$package_repository" \
      --pattern "$source_pattern" --dir "$download_dir"
  done <<< "$source_patterns"
  append_source_records "$download_dir" "$archive_prefix"
done < <(jq -c '.[]' "$repository_root/packages.json")

gzip -n -9 -c "$output_dir/Packages" > "$output_dir/Packages.gz"
gzip -n -9 -c "$output_dir/Sources" > "$output_dir/Sources.gz"

temporary_release="$work_dir/Release"
(
  cd "$output_dir"
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin='Open Research Tools' \
    -o APT::FTPArchive::Release::Label='Open Research Tools' \
    -o APT::FTPArchive::Release::Suite='stable' \
    -o APT::FTPArchive::Release::Codename='stable' \
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
