#!/usr/bin/env python3
"""Pacman's external downloader for the GitHub-only ORT catalogue.

Pacman verifies the signed sync database and package checksums. This helper
resolves a package's flat filename using its signed ``DATA`` entry and checks
the downloaded bytes before accepting a resumed transfer. It never executes
commands obtained from repository metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


CONFIG_PATH = Path("/etc/openresearchtools-pacman.json")
DATABASE_NAME = "openresearchtools"
DOWNLOAD_DATA_KEY = "ort_download_url"
PACKAGE_SUFFIX = re.compile(r"\.pkg\.tar(?:\.(?:zst|xz|gz|bz2|lz4|lrz|lzo|lz|Z))?$")
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
OWNER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z")
MAX_DESC_BYTES = 1024 * 1024
MAX_CATALOGUE_ENTRIES = 100_000
BUNDLE_NAME = DATABASE_NAME + ".catalogue.tar.gz"
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_SIGNATURE_BYTES = 1024 * 1024


class FetchError(ValueError):
    """An invalid configuration or catalogue must not fall back to a download."""


def normalize_architecture(value: str | None = None) -> str:
    value = value or platform.machine()
    value = {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)
    if value not in {"x86_64", "aarch64"}:
        raise FetchError(f"Unsupported architecture: {value!r}")
    return value


def validate_configuration(value: object) -> dict:
    if not isinstance(value, dict):
        raise FetchError("The downloader configuration must be a JSON object")
    config = dict(value)
    repository = config.get("catalogue_repository", "openresearchtools/apt")
    if not isinstance(repository, str) or not REPOSITORY_NAME.fullmatch(repository):
        raise FetchError("catalogue_repository must be an owner/repository name")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise FetchError("Invalid catalogue_repository")
    channel = config.get("channel", "stable")
    if not isinstance(channel, str) or channel not in {"stable", "nightly", "testing"}:
        raise FetchError("channel must be stable, nightly, or testing")
    architecture = config.get("architecture")
    if architecture is not None and not isinstance(architecture, str):
        raise FetchError("architecture must be a string")
    owners = config.get("allowed_owners", ["openresearchtools"])
    if not isinstance(owners, list) or not owners or any(
        not isinstance(owner, str) or not OWNER_NAME.fullmatch(owner) for owner in owners
    ):
        raise FetchError("allowed_owners must be a nonempty list of GitHub owners")
    db_path = config.get("db_path", f"/var/lib/pacman/sync/{DATABASE_NAME}.db")
    if not isinstance(db_path, str) or not Path(db_path).is_absolute():
        raise FetchError("db_path must be an absolute filesystem path")
    if "replace_downloader" in config and not isinstance(config["replace_downloader"], bool):
        raise FetchError("replace_downloader must be a JSON boolean")
    config.update(
        catalogue_repository=repository,
        channel=channel,
        architecture=normalize_architecture(architecture),
        allowed_owners=sorted({owner.lower() for owner in owners}),
        db_path=db_path,
    )
    return config


def load_configuration(path: Path = CONFIG_PATH) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"Cannot read downloader configuration {path}: {exc}") from exc
    return validate_configuration(value)


def server_url(config: dict) -> str:
    return (
        f"https://github.com/{config['catalogue_repository']}/releases/download/"
        f"pacman-{config['channel']}-{config['architecture']}"
    )


def parse_description(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    key = None
    for line in text.splitlines():
        if line.startswith("%") and line.endswith("%"):
            key = line[1:-1]
            if key in fields:
                raise FetchError(f"Duplicate catalogue field %{key}%")
            fields[key] = []
        elif not line:
            key = None
        elif key is not None:
            fields[key].append(line)
    return fields


def validate_download_url(url: str, config: dict) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise FetchError("Package routes must be HTTPS GitHub release-asset URLs")
    parts = parsed.path.split("/")
    if (
        len(parts) < 7
        or parts[0] != ""
        or parts[3:5] != ["releases", "download"]
        or not all(parts[1:3] + parts[5:])
        or parts[1].lower() not in config["allowed_owners"]
        or not REPOSITORY_NAME.fullmatch("/".join(parts[1:3]))
        or any(unquote(part) in {".", ".."} for part in parts[1:])
    ):
        raise FetchError("Package route is not an allowed immutable GitHub release asset")
    return url


def catalogue_package(filename: str, config: dict) -> tuple[str, str, int]:
    """Read a package route and integrity metadata from the signed sync DB."""
    if any(character in filename for character in "/?#") or unquote(filename) != filename:
        raise FetchError("Invalid flat repository filename")
    matches: list[tuple[str, str, int]] = []
    total = 0
    try:
        with tarfile.open(config["db_path"], "r:*") as archive:
            for count, member in enumerate(archive, 1):
                if count > MAX_CATALOGUE_ENTRIES:
                    raise FetchError("The sync database contains too many entries")
                total += member.size
                if total > MAX_BUNDLE_BYTES:
                    raise FetchError("The sync database exceeds the metadata size limit")
                if not member.isfile() or not member.name.endswith("/desc"):
                    continue
                if member.size > MAX_DESC_BYTES:
                    raise FetchError("The sync database contains an oversized description")
                stream = archive.extractfile(member)
                if stream is None:
                    raise FetchError("Cannot read sync database description")
                fields = parse_description(stream.read(MAX_DESC_BYTES + 1).decode("utf-8"))
                if fields.get("FILENAME") != [filename]:
                    continue
                routes = [
                    item.split("=", 1)[1]
                    for item in fields.get("DATA", [])
                    if item.startswith(DOWNLOAD_DATA_KEY + "=")
                ]
                if len(routes) != 1:
                    raise FetchError(f"Missing or ambiguous download route for {filename}")
                digests = fields.get("SHA256SUM", [])
                sizes = fields.get("CSIZE", [])
                if len(digests) != 1 or not re.fullmatch(r"[a-fA-F0-9]{64}", digests[0]):
                    raise FetchError(f"Missing or invalid signed SHA-256 for {filename}")
                if len(sizes) != 1 or not re.fullmatch(r"[1-9][0-9]*", sizes[0]):
                    raise FetchError(f"Missing or invalid signed download size for {filename}")
                try:
                    size = int(sizes[0])
                except ValueError as exc:
                    raise FetchError(f"Invalid signed download size for {filename}") from exc
                matches.append((validate_download_url(routes[0], config), digests[0].lower(), size))
    except (OSError, tarfile.TarError, UnicodeError) as exc:
        raise FetchError(f"Cannot read sync database {config['db_path']}: {exc}") from exc
    if len(matches) != 1:
        raise FetchError(f"Expected one catalogue entry for {filename}; found {len(matches)}")
    return matches[0]


def catalogue_download_url(filename: str, config: dict) -> str:
    return catalogue_package(filename, config)[0]


def resolve_url(url: str, config: dict) -> str:
    prefix = server_url(config) + "/"
    if not url.startswith(prefix):
        return url
    filename = url[len(prefix):]
    if any(character in filename for character in "/?#") or unquote(filename) != filename:
        raise FetchError("Invalid flat repository filename")
    signature = filename.endswith(".sig")
    package_filename = filename[:-4] if signature else filename
    if not PACKAGE_SUFFIX.search(package_filename):
        return url  # Sync database, its signature, and other metadata.
    target = catalogue_download_url(package_filename, config)
    return target + ".sig" if signature else target


def download(url: str, output: str, maximum_size: int | None = None,
             quiet_not_found: bool = False) -> int:
    # Use argv throughout: filenames and URLs never become shell code.
    command = [
        "/usr/bin/curl", "--fail", "--location", "--retry", "3", "--progress-bar",
        "--connect-timeout", "30", "--continue-at", "-", "--output", output,
    ]
    if maximum_size is not None:
        command.extend(["--max-filesize", str(maximum_size)])
    if quiet_not_found:
        command.extend(["--silent", "--show-error", "--write-out", "%{http_code}"])
    command.extend(["--", url])
    try:
        if not quiet_not_found:
            return subprocess.run(command, check=False).returncode
        result = subprocess.run(command, check=False, text=True, capture_output=True)
        # Some foreign repositories have no detached database signature. Keep
        # the failing status so pacman applies its own optional/required policy;
        # suppress only this expected HTTP 404 diagnostic, never other failures.
        if not (result.returncode == 22 and result.stdout.strip() == "404"):
            sys.stderr.write(result.stderr)
        return result.returncode
    except OSError as exc:
        raise FetchError(f"Cannot run curl: {exc}") from exc


def package_matches(path: Path, digest: str, size: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        return checksum.hexdigest() == digest
    except OSError:
        return False


def download_package(url: str, output: str, digest: str, size: int) -> int:
    """Retain valid resumes, but recover a corrupt partial with one clean try."""
    destination = Path(output)
    for attempt in range(2):
        result = download(url, output)
        if result:
            return result
        if package_matches(destination, digest, size):
            return 0
        # This is precisely pacman's requested partial output. Do not remove
        # any neighbouring cached package or bypass pacman's own validation.
        destination.unlink(missing_ok=True)
        if attempt == 0:
            print("openresearchtools-pacman-fetch: Download checksum or size differs from the signed catalogue; retrying once from the beginning.", file=sys.stderr)
    raise FetchError("Package checksum or size still differs from the signed catalogue after a fresh retry; the invalid partial download was removed")


def read_catalogue_bundle(path: Path) -> tuple[bytes, bytes]:
    """Read only the two expected regular files, without extracting paths."""
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        raise FetchError("The catalogue bundle exceeds the metadata size limit")
    expected = {DATABASE_NAME + ".db", DATABASE_NAME + ".db.sig"}
    contents: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.name not in expected or member.name in contents or not member.isfile():
                    raise FetchError("The catalogue bundle must contain exactly the database and its signature")
                maximum = MAX_SIGNATURE_BYTES if member.name.endswith(".sig") else MAX_BUNDLE_BYTES
                total += member.size
                if member.size < 1 or member.size > maximum or total > MAX_BUNDLE_BYTES:
                    raise FetchError("The catalogue bundle exceeds the metadata size limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise FetchError("Cannot read catalogue bundle member")
                data = stream.read(maximum + 1)
                if len(data) != member.size:
                    raise FetchError("The catalogue bundle is truncated")
                contents[member.name] = data
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise FetchError(f"Cannot read catalogue bundle: {exc}") from exc
    if contents.keys() != expected:
        raise FetchError("The catalogue bundle is missing the database or its signature")
    return contents[DATABASE_NAME + ".db"], contents[DATABASE_NAME + ".db.sig"]


def write_download(path: Path, data: bytes):
    descriptor, temporary = tempfile.mkstemp(prefix=".ort-download-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def signature_stash(output: Path, config: dict) -> Path:
    namespace = hashlib.sha256(server_url(config).encode()).hexdigest()[:24]
    return output.parent / (".ort-db-signature-" + namespace)


def fetch_url(url: str, output: str, config: dict) -> int:
    """Keep the mutable GitHub catalogue's database/signature generation paired."""
    prefix = server_url(config) + "/"
    destination = Path(output)
    if url == prefix + DATABASE_NAME + ".db":
        stash = signature_stash(destination, config)
        stash.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ort-catalogue-", dir=destination.parent) as temporary:
            bundle = Path(temporary) / BUNDLE_NAME
            result = download(prefix + BUNDLE_NAME, str(bundle), MAX_BUNDLE_BYTES)
            if result:
                return result
            database, signature = read_catalogue_bundle(bundle)
            write_download(stash, signature)
            write_download(destination, database)
        return 0
    if url == prefix + DATABASE_NAME + ".db.sig":
        stash = signature_stash(destination, config)
        try:
            if stash.stat().st_size > MAX_SIGNATURE_BYTES:
                raise FetchError("The staged database signature is too large")
            signature = stash.read_bytes()
        except OSError as exc:
            raise FetchError("No matching database signature is staged; refresh the catalogue database first") from exc
        write_download(destination, signature)
        stash.unlink()
        return 0
    if url.startswith(prefix) and PACKAGE_SUFFIX.search(url[len(prefix):]):
        target, digest, size = catalogue_package(url[len(prefix):], config)
        return download_package(target, output, digest, size)
    if not url.startswith(prefix) and urlsplit(url).path.endswith(".db.sig"):
        return download(url, output, quiet_not_found=True)
    return download(resolve_url(url, config), output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("url")
    parser.add_argument("output", help="Pacman's %%o destination, including .part")
    args = parser.parse_args(argv)
    try:
        config = load_configuration(args.config)
        return fetch_url(args.url, args.output, config)
    except (FetchError, OSError) as exc:
        print(f"openresearchtools-pacman-fetch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
