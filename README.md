# Open Research Tools APT repository

This repository publishes the signed APT package index for Open Research Tools
at `https://apt.openresearchtools.com`.

## Installation

### Recommended: install the keyring package

The architecture-independent keyring package installs both the public archive
key and the APT source definition:

```bash
curl -fsSLo /tmp/openresearchtools-archive-keyring.deb \
  https://apt.openresearchtools.com/openresearchtools-archive-keyring.deb
sudo apt install /tmp/openresearchtools-archive-keyring.deb
sudo apt update
sudo apt install gnozzard
```

The keyring package has `Architecture: all`, so the same file works on AMD64
and ARM systems. Individual applications are available only for architectures
listed in the repository; Gnozzard is currently available for AMD64.

### Manual key and source setup

Do not combine this method with the keyring-package method above.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.openresearchtools.com/openresearchtools-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/openresearchtools-archive-keyring.gpg >/dev/null
sudo chmod 0644 /etc/apt/keyrings/openresearchtools-archive-keyring.gpg
sudo tee /etc/apt/sources.list.d/openresearchtools.sources >/dev/null <<'EOF'
Types: deb
URIs: https://apt.openresearchtools.com
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/openresearchtools-archive-keyring.gpg
EOF
sudo apt update
sudo apt install gnozzard
```

APT verifies the signed repository metadata and the package hashes it contains.
The repository publisher retrieves package files from the latest configured
GitHub releases and deploys them to GitHub Pages without committing `.deb`
files to Git history.

## Archive signing key

The public archive key is available in binary and ASCII-armored forms:

- `keys/openresearchtools-archive-keyring.gpg`
- `keys/openresearchtools-archive-keyring.asc`

Fingerprint:

```text
94A2 D5BD BD2A 22C1 B6F0 914D C7B3 EFA0 BA41 EB0A
```

Only the public archive key belongs in Git. The private primary key and CI
signing subkey must never be committed, even in encrypted form. The CI signing
subkey is held in the protected `apt-signing` GitHub environment. The encrypted
primary-key recovery copy is stored separately from this repository.

## Publishing

Package sources are declared in `packages.json`. The publishing workflow
downloads `.deb` assets from each repository's latest GitHub release, builds
AMD64 and ARM64 indexes, signs the `stable` release, and deploys the generated
tree to GitHub Pages. It can be run manually and also refreshes weekly.
