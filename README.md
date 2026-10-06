# Open Research Tools APT repository

This repository publishes the signed package catalogue for Open Research
Tools at `https://apt.openresearchtools.com`. Application binaries remain in
each application's own GitHub Releases; the central `openresearchtools/apt`
releases contain only signed indexes, public-key files, and the small
Debian and Termux setup packages. The `repo` catalogue contains normal releases;
`nightly` contains prereleases. Neither includes draft releases.

## Installation

### Debian/Ubuntu: install the keyring package

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
sudo apt install simplehf
sudo apt install buzzardos
sudo apt install lufus
```

The keyring package has `Architecture: all`, so the same file works on AMD64
and ARM64 Debian/Ubuntu systems. Its filesystem paths are not suitable for
native Termux; use the separate Termux setup below. Individual applications
and engine backends are available only for the architectures present in their
published GitHub releases.

Buzzard OS publishes four independently versioned packages. Install the host
manager on the host; its signed reference-machine recipes install the three
guest packages from this same repository and leave them APT-updatable:

```bash
sudo apt install buzzardos
# Installed inside a Buzzard reference machine by its Containerfile:
sudo apt install buzzardos-guest buzzardos-desktop buzzardoscua
```

On AMD64, PDF Markdown Studio and Transcribe Offline depend on the co-installable
Vulkan and CUDA engine packages. On ARM64, both apps depend only on the Vulkan
package `openresearchtools-engine (>= 1.17)`. APT automatically selects the
engine package for the machine architecture; ARM64 does not require CUDA.
The engine package names are:

```text
openresearchtools-engine
openresearchtools-engine-cuda
```

To install the Vulkan engine runtime on ARM64 or AMD64:

```bash
sudo apt install openresearchtools-engine
```

To install both engine backends on AMD64:

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

### Native Termux: install the Termux keyring package

On an ARM64 or x86-64 Android device running native Termux (not a Debian proot), run:

```bash
pkg install wget
architecture="$(dpkg --print-architecture)"
case "$architecture" in aarch64|x86_64) ;; *) echo 'Unsupported architecture'; exit 1;; esac
wget -O "$HOME/openresearchtools-termux-keyring.deb" \
  "https://github.com/openresearchtools/apt/releases/download/repo/openresearchtools-termux-keyring_${architecture}.deb"
apt install "$HOME/openresearchtools-termux-keyring.deb"
apt update
```

No root or `sudo` is needed. This separately named architecture-specific package installs
the same public archive key and the same flat repository URL under
`/data/data/com.termux/files/usr`. It also works with custom-signed Termux
builds that retain that package name and prefix.

After setup, use `apt install PACKAGE` and `apt upgrade` normally. Only
applications actually published as native Termux/Bionic builds are usable;
adding the repository does not convert Debian binaries into Android binaries.
Application builds are published independently by their respective projects.
`https://termux.openresearchtools.com` and the undated
`openresearchtools-termux-keyring.deb` alias download the ARM64 setup package;
x86-64 users must select the `_x86_64.deb` asset shown above.

Zotero is available as a native `aarch64` Termux package. Enable Termux's X11
repository for its desktop dependencies, then install it by package name:

```bash
pkg install x11-repo
apt update
apt install zotero
# Optional, for citations in Writer:
apt install libreoffice
```

APT installs Zotero's required `openjdk-21` and `openjdk-21-x` dependencies
automatically. Save documents and close LibreOffice, then use Zotero's
**Settings → Cite → Word Processors → Install/Reinstall LibreOffice Add-in**.
The optional Pi skill and helper are bundled at `$PREFIX/share/zotero/pi`.
See the [Zotero Termux instructions](https://github.com/openresearchtools/zotero-termux/blob/main/termux/README.md)
for the Termux:X11 Android app, desktop startup, and Pi setup.

The hourly catalogue refresh tracks stable releases from `zotero-termux`.
It indexes only `zotero_*_aarch64.deb`; the separately reusable Gecko build
archive is not an additional package users need to install.

Both platforms share each signed `Packages` index. Debian uses `amd64` or
`arm64`; Termux uses `x86_64` or `aarch64`. Both also consider `Architecture: all`
packages, so `all` does not mean cross-platform compatibility. The differently
named keyring packages do not replace each other during upgrades. Do not
install the Debian keyring or other Debian-only `all` packages in Termux.

### Enable or remove nightly updates

Install the stable keyring explicitly as shown above first. This leaves it
manually installed, so removing nightly and running `apt autoremove` does not
remove the stable source. The nightly setup package also depends on that
platform's keyring; it never overwrites its source or public key.

On Debian/Ubuntu:

```bash
wget -O /tmp/openresearchtools-nightly.deb https://nightly.openresearchtools.com
sudo apt install /tmp/openresearchtools-nightly.deb
sudo apt update
sudo apt upgrade
```

On native Termux:

```bash
architecture="$(dpkg --print-architecture)"
wget -O "$HOME/openresearchtools-termux-nightly.deb" \
  "https://github.com/openresearchtools/apt/releases/download/nightly/openresearchtools-termux-nightly_${architecture}.deb"
apt install "$HOME/openresearchtools-termux-nightly.deb"
apt update
apt upgrade
```

`https://nightly-termux.openresearchtools.com` and the undated
`openresearchtools-termux-nightly.deb` alias download the ARM64 nightly setup.
Both architecture-specific aliases remain available on the `nightly` release.

The added source uses the existing redirect and signing key:

```text
apt.openresearchtools.com/apt/releases/download/nightly/InRelease
→ github.com/openresearchtools/apt/releases/download/nightly/InRelease
```

Stable and nightly have equal default priority. APT selects the highest
Debian package version from either catalogue, regardless of the release date.
Applications must use the same package names and increasing versions; for
example `153.4 < 153.5~nightly.20261006.1 < 153.5`. A newer stable build then
replaces the nightly automatically. A channel does not force a downgrade.

To stop receiving prereleases, remove `openresearchtools-nightly` on Debian or
`openresearchtools-termux-nightly` on Termux and run `apt update`. The package
owns `openresearchtools-nightly.sources` as a regular file rather than a
conffile, so **plain removal disables the source; purge is not required**.
The stable source remains. Installed nightly applications stay installed
until a stable package with a higher version is available. User-defined APT
pins can override the default equal-priority selection.

### Debian/Ubuntu: manual key and source setup

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
sudo apt install simplehf
sudo apt install buzzardos
sudo apt install lufus
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
assets. The publishing workflow reads GitHub Releases into two catalogues: `repo` selects
only non-prereleases, and `nightly` selects only prereleases. Drafts are excluded
from both. Both use exactly the same repository list and filename patterns,
and repositories without prereleases simply contribute no nightly packages.
From those releases it indexes only attached `.deb`
assets matching the configured filename pattern. GitHub Actions artifacts and
unrelated release assets are never indexed. Debian version comparison determines
which stable version APT selects as the default upgrade candidate.
Identical binaries carried forward into another release are indexed once when
the package name, version, architecture, SHA-256 digest, and size match. A
repeated package identity with different binary content fails publication.

List every indexed version or install one explicitly:

```bash
apt list -a gnozzard
sudo apt install gnozzard=0.1.5
```

APT can install an older version only while that GitHub release and its `.deb`
asset still exist. Downgrading an already-installed newer package may require
`--allow-downgrades`.

The workflow refreshes both catalogues hourly, on manual dispatch, and after
a `package-released` repository dispatch. It reads only the small control section at the start of
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
archive key, with the appropriate user-installed keyring package for each platform.

The publisher accepts `all`, `amd64`, `arm64`, `aarch64`, and `x86_64` package metadata.
Use `aarch64` or `x86_64` for native Termux builds in this catalogue and keep their paths
and dependencies compatible with Termux. Asset patterns include architectures
without requiring a separate catalogue or signing key. Debian-specific
`Architecture: all` packages must not be dependencies of Termux packages.
