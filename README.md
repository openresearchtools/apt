# Open Research Tools APT repository

This repository publishes the signed package catalogue for Open Research
Tools at `https://apt.openresearchtools.com`. Application binaries remain in
each application's own GitHub Releases; the central `openresearchtools/apt`
release contains only signed indexes, public-key files, and the small
archive-keyring package.

## Installation

### Recommended: install the keyring package

The architecture-independent keyring package installs both the public archive
key and the APT source definition:

```bash
curl -fsSLo /tmp/openresearchtools-archive-keyring.deb \
  https://apt.openresearchtools.com/apt/releases/download/repo/openresearchtools-archive-keyring.deb
sudo apt install /tmp/openresearchtools-archive-keyring.deb
sudo apt update
sudo apt install gnozzard
```

The keyring package has `Architecture: all`, so the same file works on AMD64
and ARM64 systems. Gnozzard currently publishes the architectures listed in
its latest GitHub release.

### Manual key and source setup

Do not combine this method with the keyring-package method above.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL \
  https://apt.openresearchtools.com/apt/releases/download/repo/openresearchtools-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/openresearchtools-archive-keyring.gpg >/dev/null
sudo chmod 0644 /etc/apt/keyrings/openresearchtools-archive-keyring.gpg
sudo tee /etc/apt/sources.list.d/openresearchtools.sources >/dev/null <<'EOF'
Types: deb
URIs: https://apt.openresearchtools.com
Suites: apt/releases/download/repo/
Signed-By: /etc/apt/keyrings/openresearchtools-archive-keyring.gpg
EOF
sudo apt update
sudo apt install gnozzard
```

## How routing works

Porkbun forwards `apt.openresearchtools.com` to:

```text
https://github.com/openresearchtools
```

The forward uses HTTP 302 and includes the requested URI path. The exact-path
APT suite therefore retrieves signed metadata from the central repository:

```text
apt.openresearchtools.com/apt/releases/download/repo/InRelease
→ github.com/openresearchtools/apt/releases/download/repo/InRelease
```

Each `Packages` entry contains an organization-relative GitHub Release path,
so package traffic goes directly to that application's own release:

```text
apt.openresearchtools.com/gnozzard/releases/download/v0.1.5/gnozzard_amd64.deb
→ github.com/openresearchtools/gnozzard/releases/download/v0.1.5/gnozzard_amd64.deb
```

APT verifies the central signed index and then verifies each downloaded file
against the size and cryptographic hashes recorded in that index.

The binary package metadata points users to the application's public GitHub
repository. GitHub automatically provides ZIP and tar.gz archives of the exact
repository tag on every release page; no separate `deb-src` repository or
manually uploaded source archive is used.

## Independent package releases

Package sources are declared in `packages.json`. Each configured repository
keeps its own version numbers, release schedule, architectures, and binary
assets. The publishing workflow reads the latest non-prerelease release of
every configured repository and rebuilds the central catalogue. Debian version
comparison determines which upgrade APT offers.

The workflow can be triggered after an application publishes a release and
also refreshes hourly. It downloads assets only into the temporary Actions
workspace to calculate and validate metadata; application packages are not
uploaded to the central release, committed to Git, or served by GitHub Pages.

## Archive signing key

The `keys` directory intentionally contains only public material:

- `keys/openresearchtools-archive-keyring.gpg`: binary public key used by APT
- `keys/openresearchtools-archive-keyring.asc`: the same public key in ASCII
  armor
- `keys/fingerprint.txt`: public identifier used to verify the key

Fingerprint:

```text
94A2 D5BD BD2A 22C1 B6F0 914D C7B3 EFA0 BA41 EB0A
```

The private primary key and CI signing subkey must never be committed, even in
encrypted form. The CI signing subkey is held in the protected `apt-signing`
GitHub environment. The encrypted primary-key recovery copy is stored outside
this repository.

## Adding a package

Every application release selected for indexing must contain one or more `.deb`
files. Its release tag must identify the source used to build those packages.

Add its repository and asset patterns to `packages.json`, then run the
`Publish APT repository metadata` workflow. All repositories can use the same
archive key and user-installed keyring package.
