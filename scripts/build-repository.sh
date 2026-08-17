#!/usr/bin/env bash

set -euo pipefail
umask 022

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${OUTPUT_DIR:-$repository_root/_site}"
work_dir="$(mktemp -d)"
gnupg_home="$(mktemp -d)"
archive_url="${APT_REPOSITORY_URL:-https://apt.openresearchtools.com}"
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

for command_name in apt-ftparchive base64 dpkg-deb dpkg-scanpackages gh gpg gpgv gzip jq; do
  require_command "$command_name"
done

case "$archive_url" in
  https://*) ;;
  *)
    printf 'APT_REPOSITORY_URL must use HTTPS: %s\n' "$archive_url" >&2
    exit 1
    ;;
esac

find "$output_dir" -mindepth 1 -depth -delete 2>/dev/null || true
mkdir -p "$output_dir/pool/main"

keyring_root="$work_dir/keyring-root"
mkdir -p \
  "$keyring_root/DEBIAN" \
  "$keyring_root/etc/apt/sources.list.d" \
  "$keyring_root/usr/share/doc/openresearchtools-archive-keyring" \
  "$keyring_root/usr/share/keyrings"

install -m 0644 \
  "$repository_root/keys/openresearchtools-archive-keyring.gpg" \
  "$keyring_root/usr/share/keyrings/openresearchtools-archive-keyring.gpg"

cat > "$keyring_root/etc/apt/sources.list.d/openresearchtools.sources" <<EOF
Types: deb
URIs: $archive_url
Suites: stable
Components: main
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

  * Install the initial Open Research Tools archive key and source.

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

keyring_deb="$work_dir/openresearchtools-archive-keyring_${keyring_version}_all.deb"
dpkg-deb --root-owner-group --build "$keyring_root" "$keyring_deb" >/dev/null

add_deb_to_pool() {
  local source_deb="$1"
  local package_name package_version package_architecture first_letter destination

  package_name="$(dpkg-deb --field "$source_deb" Package)"
  package_version="$(dpkg-deb --field "$source_deb" Version)"
  package_architecture="$(dpkg-deb --field "$source_deb" Architecture)"

  if [[ ! "$package_name" =~ ^[a-z0-9][a-z0-9+.-]+$ ]]; then
    printf 'Invalid package name in %s: %s\n' "$source_deb" "$package_name" >&2
    exit 1
  fi
  if [[ ! "$package_architecture" =~ ^(all|amd64|arm64)$ ]]; then
    printf 'Unsupported package architecture in %s: %s\n' \
      "$source_deb" "$package_architecture" >&2
    exit 1
  fi

  first_letter="${package_name:0:1}"
  destination="$output_dir/pool/main/$first_letter/$package_name"
  mkdir -p "$destination"
  install -m 0644 "$source_deb" \
    "$destination/${package_name}_${package_version}_${package_architecture}.deb"
}

add_deb_to_pool "$keyring_deb"
install -m 0644 "$keyring_deb" "$output_dir/openresearchtools-archive-keyring.deb"

while IFS= read -r package_spec; do
  package_repository="$(jq -r '.repository' <<<"$package_spec")"
  asset_glob="$(jq -r '.asset_glob' <<<"$package_spec")"
  download_dir="$(mktemp -d "$work_dir/download.XXXXXX")"

  if [[ ! "$package_repository" =~ ^openresearchtools/[A-Za-z0-9_.-]+$ ]]; then
    printf 'Invalid configured GitHub repository: %s\n' "$package_repository" >&2
    exit 1
  fi

  release_tag="$(gh api "repos/$package_repository/releases/latest" --jq '.tag_name')"
  printf 'Downloading %s release %s (%s)\n' \
    "$package_repository" "$release_tag" "$asset_glob"
  gh release download "$release_tag" --repo "$package_repository" \
    --pattern "$asset_glob" --dir "$download_dir"

  downloaded=0
  while IFS= read -r -d '' package_file; do
    add_deb_to_pool "$package_file"
    downloaded=$((downloaded + 1))
  done < <(find "$download_dir" -maxdepth 1 -type f -name '*.deb' -print0)

  if [[ "$downloaded" -eq 0 ]]; then
    printf 'No Debian packages downloaded from %s release %s\n' \
      "$package_repository" "$release_tag" >&2
    exit 1
  fi
done < <(jq -c '.[]' "$repository_root/packages.json")

for architecture in amd64 arm64; do
  binary_dir="$output_dir/dists/stable/main/binary-$architecture"
  mkdir -p "$binary_dir"
  (
    cd "$output_dir"
    dpkg-scanpackages --arch "$architecture" --multiversion pool /dev/null \
      > "dists/stable/main/binary-$architecture/Packages"
  )
  gzip -n -9 -c "$binary_dir/Packages" > "$binary_dir/Packages.gz"
done

release_file="$output_dir/dists/stable/Release"
temporary_release="$work_dir/Release"
(
  cd "$output_dir"
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin='Open Research Tools' \
    -o APT::FTPArchive::Release::Label='Open Research Tools' \
    -o APT::FTPArchive::Release::Suite='stable' \
    -o APT::FTPArchive::Release::Codename='stable' \
    -o APT::FTPArchive::Release::Architectures='amd64 arm64' \
    -o APT::FTPArchive::Release::Components='main' \
    -o APT::FTPArchive::Release::Description='Open Research Tools packages' \
    release dists/stable > "$temporary_release"
)
install -m 0644 "$temporary_release" "$release_file"

install -m 0644 "$repository_root/keys/openresearchtools-archive-keyring.gpg" \
  "$output_dir/openresearchtools-archive-keyring.gpg"
install -m 0644 "$repository_root/keys/openresearchtools-archive-keyring.asc" \
  "$output_dir/openresearchtools-archive-keyring.asc"
install -m 0644 "$repository_root/site/index.html" "$output_dir/index.html"
touch "$output_dir/.nojekyll"

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
  --clearsign --output "$output_dir/dists/stable/InRelease" "$release_file"
gpg --batch --yes --armor --local-user "$archive_fingerprint" \
  --digest-algo SHA256 --detach-sign \
  --output "$output_dir/dists/stable/Release.gpg" "$release_file"

gpgv --keyring "$repository_root/keys/openresearchtools-archive-keyring.gpg" \
  "$output_dir/dists/stable/InRelease" >/dev/null

printf 'Built and signed APT repository at %s\n' "$output_dir"
