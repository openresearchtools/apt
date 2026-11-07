# Arch Linux and Omarchy packages on GitHub

This is a separate native Arch catalogue alongside the existing APT catalogue.
It uses GitHub Releases and GitHub Actions only. No web server, Cloudflare
Worker, paid hosting, or additional domain forwarding is required.

## What is tracked

The repositories in the existing `packages.json` are scanned for native
`.pkg.tar.zst`, `.pkg.tar.xz`, and `.pkg.tar.gz` release assets. Source archives
and `.deb` files are not repackaged by this repository.

Each application's **manual** `Repackage Linux release for Arch` workflow
creates its Arch assets from the Linux Debian packages in the selected release.
An empty release-tag input selects that app's latest published stable release.
The workflow attaches the Arch packages, separate Arch checksums, and provenance
to the **same** release. Original application binaries and existing release
assets are preserved. A new upstream dependency or changed Debian maintainer
script can require updating the reviewed rules in that application's repository.

The formats and architecture labels are kept separate:

| Platform | Package files | Architecture metadata |
| --- | --- | --- |
| Native Termux | `.deb` | `aarch64`, `x86_64` |
| Debian/Ubuntu Linux | `.deb` | `arm64`, `amd64` |
| Arch Linux/Omarchy | `.pkg.tar.*` | `aarch64`, `x86_64` |

The Arch repackagers explicitly reject Debian packages with the Termux
architecture labels. Existing APT asset patterns remain `.deb`-only.

## Installation on Omarchy

Install the small setup package once:

```bash
curl -fL --retry 3 -o /tmp/openresearchtools-pacman.pkg.tar.gz \
  https://github.com/openresearchtools/apt/releases/download/pacman-tools/openresearchtools-pacman-0.1.0-1-any.pkg.tar.gz
sudo pacman -U /tmp/openresearchtools-pacman.pkg.tar.gz
sudo openresearchtools-pacman-setup --apply --channel stable
omarchy update
omarchy pkg add transcribe-offline pdf-markdown-studio lufus simplehf
```

The setup imports the same public archive key as APT, checks its expected
fingerprint, and configures this separate signed pacman catalogue. It also
installs a refresh hook for the invoking Omarchy user so repository settings
survive Omarchy's pacman configuration refreshes. The setup command without
`--apply` previews the changes. A previously configured custom downloader is
preserved unless `--replace-downloader` is explicitly supplied.

On ordinary Arch Linux, use `sudo pacman -Syu` instead of `omarchy update`, then
`sudo pacman -S transcribe-offline pdf-markdown-studio lufus simplehf`.

### Channels

```bash
# Stable published releases only (the default)
sudo openresearchtools-pacman-setup --apply --channel stable

# Prereleases only, for packages that have a prerelease
sudo openresearchtools-pacman-setup --apply --channel nightly

# Optional combined stable/prerelease view, choosing the highest Arch version
sudo openresearchtools-pacman-setup --apply --channel testing
```

Run `omarchy update` after switching. Draft releases are never indexed. Raw
`stable` and `nightly` catalogues stay separate; `testing` is the optional
combined view that reproduces APT's highest-version-across-both behavior. Only
one Open Research Tools view is configured at a time. Switching back to stable
does not automatically downgrade an already installed higher prerelease.

## How GitHub-only downloads work

Pacman normally constructs package URLs from a repository base and a flat
filename. Unlike APT, its filename field cannot contain a path to another app's
GitHub release. This setup installs a small downloader through pacman's
supported global `XferCommand` option:

1. Pacman requests the selected channel's catalogue.
2. The downloader fetches a single static GitHub release asset containing the
   database and its matching detached signature. This avoids mixed generations
   while metadata is being republished.
3. Pacman verifies the database with the trusted public archive key.
4. Each signed package entry carries `ort_download_url` in pacman's supported
   `DATA` extension. The downloader reads that exact original release URL and
   downloads the selected package directly from its own application's release.
5. Pacman checks the downloaded bytes against the SHA-256 in the signed index.

The private signing key stays in this repository's existing `apt-signing`
GitHub environment. Upstream app repositories need no signing key. Their own
`GITHUB_TOKEN` only uploads packaging results back to their own releases.

The catalogue is signed; individual application package signatures are not
required. Each repository is configured with
`SigLevel = PackageNever DatabaseRequired TrustedOnly`.

**Downloader tradeoff:** `XferCommand` replaces pacman's built-in downloader
globally. Ordinary repository downloads are passed straight to curl, and
downloads are sequential. Other package managers using libalpm directly may
not use pacman's external-download configuration.

## Publisher operation

`Publish GitHub-only pacman catalogues` runs hourly, on manual dispatch, and
after `package-released` dispatches. It only reads release metadata/package
headers and publishes catalogue metadata. It never runs an upstream recipe or
repackages an app. Cached package metadata and conditional API responses are
stored in the small `pacman-state` release so unchanged large packages do not
need to be downloaded again. A failed scan leaves the live catalogue intact.

`catalogue.json` can omit exact package versions with `excluded_package_versions`.
The eGPU app's earlier `1:0.1.0-1` and `1:0.1.0-2` versions are omitted so the
normal `0.1.1` version is selected. Their release assets remain available, and
the retained NVIDIA driver and GPU Unlock packages remain indexed. Exclusions
match the full package name and version in `.PKGINFO`; they do not use patterns.

The existing `apt-signing` environment and its
`APT_GPG_SIGNING_SUBKEY_B64` / `APT_KEY_PASSWORD` secrets are reused. No additional
signing secret is needed in any application repository.

```bash
# Refresh catalogue metadata after application Arch assets have been published
gh workflow run publish-pacman.yml --repo openresearchtools/apt

# Repackage only an application's latest stable release (manual)
gh workflow run repackage-arch.yml --repo openresearchtools/simplehf

# Explicitly select an existing stable or prerelease tag
gh workflow run repackage-arch.yml --repo openresearchtools/simplehf -f release_tag=v0.2.5
```

GitHub API and Actions limits still apply. The workflow batches metadata
refreshes and checkpoints scanner progress; it does not claim unlimited
throughput. Standard runners for public repositories are used; no larger paid
runners or external hosting services are configured.

## Development checks

```bash
python -m unittest discover -s pacman/tests -p 'test_*.py'
python pacman/tests/integration.py
```

The integration test uses temporary files, an ephemeral signing key and an
isolated pacman database. It verifies stable/prerelease selection, downloads
from separate original-release paths, and rejection of altered packages and
invalid catalogue signatures. It does not alter the host's pacman configuration
or keyring. Bump `pacman/VERSION` whenever the installed setup package changes;
published versioned setup package bytes are immutable.
