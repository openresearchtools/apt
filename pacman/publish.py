#!/usr/bin/env python3
"""Sign and upload metadata-only pacman releases using the existing APT key.

Modes restore/state transfer the incremental scanner cache without importing a
signing key. Publish imports the CI signing subkey only after scanning and tests
have succeeded. Only the small client setup package is hosted centrally.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from catalogue import CatalogueError, validate_repository


FINGERPRINT = "94A2D5BDBD2A22C1B6F0914DC7B3EFA0BA41EB0A"
STATE_TAG = "pacman-state"
TOOLS_TAG = "pacman-tools"
STATE_ASSET = "state.json.gz"
MAX_STATE_BYTES = 512 * 1024 * 1024


class PublishError(Exception):
    pass


def command(argv: list[str], *, env: dict | None = None, data: bytes | None = None) -> bytes:
    result = subprocess.run(argv, input=data, env=env, capture_output=True)
    if result.returncode:
        # Never echo the environment, input, or secret-key importer output.
        raise PublishError(f"{Path(argv[0]).name} failed (exit {result.returncode})")
    return result.stdout


class Releases:
    def __init__(self, repository: str, token: str | None = None):
        self.repository = validate_repository(repository)
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise PublishError("GH_TOKEN or GITHUB_TOKEN is required")
        self.environment = dict(os.environ, GH_TOKEN=self.token)

    def api(self, path: str) -> dict | None:
        request = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "OpenResearchTools-pacman-publisher/1",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise PublishError(f"GitHub release lookup failed ({error.code})") from error

    def get(self, tag: str) -> dict | None:
        return self.api(f"/repos/{self.repository}/releases/tags/{urllib.parse.quote(tag, safe='')}")

    def ensure(self, tag: str, title: str, *, prerelease: bool = False) -> dict:
        release = self.get(tag)
        if release is None:
            argv = ["gh", "release", "create", tag, "--repo", self.repository,
                    "--title", title, "--notes", "Generated Open Research Tools pacman catalogue metadata.",
                    "--latest=false"]
            if os.environ.get("GITHUB_SHA"):
                argv += ["--target", os.environ["GITHUB_SHA"]]
            if prerelease:
                argv += ["--prerelease"]
            command(argv, env=self.environment)
            release = self.get(tag)
        if release is None:
            raise PublishError(f"Could not create release {tag}")
        return release

    def upload(self, tag: str, paths: list[Path]) -> None:
        if paths:
            command(["gh", "release", "upload", tag, "--repo", self.repository,
                     "--clobber", *[str(path) for path in paths]], env=self.environment)


def restore_state(releases: Releases, destination: Path) -> None:
    release = releases.get(STATE_TAG)
    if not release:
        print("No previous pacman scanner state; this is the first scan.")
        return
    asset = next((item for item in release.get("assets", []) if item["name"] == STATE_ASSET), None)
    if not asset:
        print("No previous pacman scanner state asset; this is the first scan.")
        return
    with tempfile.TemporaryDirectory(prefix="ort-pacman-state-") as directory:
        command(["gh", "release", "download", STATE_TAG, "--repo", releases.repository,
                 "--pattern", STATE_ASSET, "--dir", directory], env=releases.environment)
        packed = Path(directory) / STATE_ASSET
        digest = asset.get("digest")
        if digest and digest != "sha256:" + hashlib.sha256(packed.read_bytes()).hexdigest():
            raise PublishError("Scanner-state download did not match GitHub's asset digest")
        with gzip.open(packed, "rb") as source:
            data = source.read(MAX_STATE_BYTES + 1)
        if len(data) > MAX_STATE_BYTES:
            raise PublishError("Scanner state exceeds the configured 512 MiB uncompressed limit")
        parsed = json.loads(data)
        if parsed.get("schema") != 1:
            raise PublishError("Unknown scanner state schema")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    print("Restored incremental pacman scanner state.")


def publish_state(releases: Releases, source: Path) -> None:
    if not source.exists():
        print("No scanner state to checkpoint.")
        return
    data = source.read_bytes()
    if json.loads(data).get("schema") != 1:
        raise PublishError("Refusing to publish invalid scanner state")
    with tempfile.TemporaryDirectory(prefix="ort-pacman-state-") as directory:
        packed = Path(directory) / STATE_ASSET
        with packed.open("wb") as output:
            with gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0) as compressor:
                compressor.write(data)
        releases.ensure(STATE_TAG, "Pacman scanner state")
        releases.upload(STATE_TAG, [packed])
    print("Checkpointed scanner state; application binaries were not uploaded.")


@contextlib.contextmanager
def signing_environment(expected_fingerprint: str):
    encoded = os.environ.get("APT_GPG_SIGNING_SUBKEY_B64", "")
    if not encoded:
        raise PublishError("The apt-signing environment needs APT_GPG_SIGNING_SUBKEY_B64")
    try:
        key = base64.b64decode("".join(encoded.split()), validate=True)
    except ValueError as error:
        raise PublishError("APT_GPG_SIGNING_SUBKEY_B64 is not valid base64") from error
    if not key or len(key) > 1024 * 1024:
        raise PublishError("Unexpected signing-subkey size")
    with tempfile.TemporaryDirectory(prefix="ort-pacman-gnupg-") as directory:
        Path(directory).chmod(0o700)
        environment = dict(os.environ, GNUPGHOME=directory)
        try:
            command(["gpg", "--batch", "--no-tty", "--import"], env=environment, data=key)
            listing = command(["gpg", "--batch", "--with-colons", "--list-secret-keys",
                               expected_fingerprint], env=environment).decode()
            fingerprints = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
            if expected_fingerprint not in fingerprints:
                raise PublishError("Imported signing key does not match the existing archive fingerprint")
            yield environment
        finally:
            subprocess.run(["gpgconf", "--kill", "all"], env=environment, capture_output=True)


def sign(path: Path, environment: dict, fingerprint: str) -> Path:
    signature = Path(str(path) + ".sig")
    password = (os.environ.get("APT_KEY_PASSWORD", "") + "\n").encode()
    command(["gpg", "--batch", "--yes", "--no-tty", "--pinentry-mode", "loopback",
             "--passphrase-fd", "0", "--local-user", fingerprint,
             "--detach-sign", "--output", str(signature), str(path)],
            env=environment, data=password)
    command(["gpg", "--batch", "--verify", str(signature), str(path)], env=environment)
    return signature


def write_bundle(database: Path, signature: Path, epoch: int = 0) -> Path:
    bundle = database.with_name("openresearchtools.catalogue.tar.gz")
    with bundle.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for source in (database, signature):
                    data = source.read_bytes()
                    item = tarfile.TarInfo(source.name)
                    item.size = len(data); item.mode = 0o644; item.mtime = epoch
                    archive.addfile(item, io.BytesIO(data))
    return bundle


def bootstrap_packages(directory: Path) -> list[Path]:
    packages = sorted(path for path in directory.iterdir() if path.name.endswith((".pkg.tar.gz", ".pkg.tar.zst", ".pkg.tar.xz")))
    if len(packages) != 1:
        raise PublishError("Bootstrap directory must contain exactly one native Arch setup package")
    return packages


def check_immutable_bootstrap(existing: dict, packages: list[Path]) -> None:
    remote = {asset["name"]: asset for asset in existing.get("assets", [])}
    for package in packages:
        asset = remote.get(package.name)
        if asset:
            actual = "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest()
            if asset.get("digest") != actual:
                raise PublishError(f"Setup package {package.name} already exists with different or unverified bytes; "
                                   "bump pacman/VERSION before publishing changed client code")


def publish_catalogues(releases: Releases, output: Path, bootstrap_directory: Path,
                       fingerprint: str, public_key: Path, dry_run: bool = False) -> None:
    summary = json.loads((output / "summary.json").read_text())
    if summary["catalogue_repository"].lower() != releases.repository.lower():
        raise PublishError("Scanner catalogue_repository does not match upload destination")
    packages = bootstrap_packages(bootstrap_directory)
    catalogues = []
    for tag in sorted(summary["catalogues"]):
        if not re.fullmatch(r"pacman-(stable|nightly|testing)-(aarch64|x86_64)", tag):
            raise PublishError(f"Unexpected catalogue tag: {tag}")
        database = output / tag / "openresearchtools.db"
        if not database.is_file():
            raise PublishError(f"Missing generated database for {tag}")
        catalogues.append((tag, database))
    if not catalogues:
        raise PublishError("No catalogues to publish")
    if dry_run:
        print(f"Would sign/upload {packages[0].name} to {TOOLS_TAG}")
        for tag, database in catalogues:
            print(f"Would sign/upload atomic catalogue bundle to {tag}: {database}")
        return
    if not public_key.is_file():
        raise PublishError("Missing existing public archive key")
    with signing_environment(fingerprint) as environment:
        package_signatures = [sign(package, environment, fingerprint) for package in packages]
        # Prepare and verify all signatures before changing any remote release.
        signed_catalogues = [(tag, database, sign(database, environment, fingerprint))
                             for tag, database in catalogues]
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        bundles = [(tag, database, signature, write_bundle(database, signature, epoch))
                   for tag, database, signature in signed_catalogues]
        tools = releases.ensure(TOOLS_TAG, "Pacman client setup")
        check_immutable_bootstrap(tools, packages)
        releases.upload(TOOLS_TAG, packages + package_signatures + [public_key])
        for tag, database, signature, bundle in bundles:
            releases.ensure(tag, f"Pacman {tag.removeprefix('pacman-')}", prerelease="-nightly-" in tag or "-testing-" in tag)
            # The matching database/signature pair is inside one asset. The
            # downloader consumes that bundle, never a mixed pair during upload.
            releases.upload(tag, [bundle, database, signature])
            print(f"Published {tag}: {summary['catalogues'][tag]['count']} package(s), metadata only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("restore", "state", "publish"), default="publish")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "openresearchtools/apt"))
    parser.add_argument("--state", type=Path, default=Path("_repo/pacman-state.json"))
    parser.add_argument("--output", type=Path, default=Path("_repo/pacman"))
    parser.add_argument("--bootstrap-dir", type=Path, default=Path("_repo/pacman-bootstrap"))
    parser.add_argument("--public-key", type=Path, default=Path("keys/openresearchtools-archive-keyring.gpg"))
    parser.add_argument("--fingerprint", default=FINGERPRINT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        # A dry run checks local outputs without requiring a publishing token.
        releases = Releases(args.repository, "dry-run" if args.dry_run else None)
        if args.mode == "restore":
            restore_state(releases, args.state)
        elif args.mode == "state":
            publish_state(releases, args.state)
        else:
            publish_catalogues(releases, args.output, args.bootstrap_dir,
                               args.fingerprint, args.public_key, args.dry_run)
        return 0
    except (PublishError, CatalogueError, OSError, ValueError, KeyError) as error:
        print(f"Pacman publication failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
