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
wget -qO /tmp/keyring.deb https://keyring.openresearchtools.com
sudo apt install /tmp/keyring.deb
sudo apt update
sudo apt install gnozzard
sudo apt install pdf-markdown-studio
sudo apt install transcribe-offline
sudo apt install llama-cpp
```

The keyring package has `Architecture: all`, so the same file works on AMD64
and ARM64 systems. Individual applications and engine backends are available
only for the architectures present in their published GitHub releases.

PDF Markdown Studio and Transcribe Offline both depend on the co-installable
Vulkan and CUDA engine packages. APT installs these automatically:

```text
openresearchtools-engine
openresearchtools-engine-cuda
```

To install only the engine runtimes:

```bash
sudo apt install openresearchtools-engine openresearchtools-engine-cuda
```

The llama.cpp repository publishes mutually exclusive standard and CUDA
packages for AMD64 and ARM64. Install one of them, not both. The CUDA variant
also requires NVIDIA's CUDA runtime packages to be available from a configured
package source:

```bash
# Standard Vulkan build
sudo apt install llama-cpp

# Or the CUDA build
sudo apt install llama-cpp-cuda
```

### Manual key and source setup

Do not combine this method with the keyring-package method above.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
wget -qO- \
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
sudo apt install pdf-markdown-studio
sudo apt install transcribe-offline
sudo apt install llama-cpp
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
apt.openresearchtools.com/gnozzard/releases/download/v0.1.6/gnozzard_amd64.deb
→ github.com/openresearchtools/gnozzard/releases/download/v0.1.6/gnozzard_amd64.deb
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
assets. The publishing workflow reads only proper GitHub Releases: drafts and
prereleases are excluded. From those releases it indexes only attached `.deb`
assets matching the configured filename pattern. GitHub Actions artifacts and
unrelated release assets are never indexed. Debian version comparison determines
which stable version APT selects as the default upgrade candidate.

List every indexed version or install one explicitly:

```bash
apt list -a gnozzard
sudo apt install gnozzard=0.1.5
```

APT can install an older version only while that GitHub release and its `.deb`
asset still exist. Downgrading an already-installed newer package may require
`--allow-downgrades`.

The workflow can be triggered after an application publishes a release and
also refreshes hourly. It reads only the small control section at the start of
each remote `.deb` and uses GitHub's recorded asset size and SHA-256 digest, so
large engine packages are not downloaded during every catalogue refresh.
Application packages are not uploaded to the central release, committed to
Git, or served by GitHub Pages.

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
