#!/usr/bin/env python3
"""Publish static pacman metadata for native Arch assets in GitHub Releases.

This program does not build software, execute PKGBUILDs, or mirror binaries.
The signed database records the original release URL in pacman's supported
DATA/xdata extension. The client downloader reads that field after pacman has
authenticated the catalogue. Signing and uploading are separate CI steps.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fnmatch
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Callable


class CatalogueError(Exception):
    """An invalid asset or unsuccessful discovery must stop publication."""


REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
NAME_RE = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@._+\-]*\Z")
FILENAME_RE = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@._+~:\-]*\Z")
SHA256_RE = re.compile(r"[a-fA-F0-9]{64}\Z")
FIELD_MAP = (
    ("pkgname", "NAME"), ("pkgbase", "BASE"), ("pkgver", "VERSION"),
    ("pkgdesc", "DESC"), ("group", "GROUPS"), ("size", "ISIZE"),
    ("url", "URL"), ("license", "LICENSE"), ("arch", "ARCH"),
    ("builddate", "BUILDDATE"), ("packager", "PACKAGER"),
    ("replaces", "REPLACES"), ("conflict", "CONFLICTS"),
    ("provides", "PROVIDES"), ("depend", "DEPENDS"),
    ("optdepend", "OPTDEPENDS"), ("makedepend", "MAKEDEPENDS"),
    ("checkdepend", "CHECKDEPENDS"),
)


def clean_value(value: str, label: str) -> str:
    if (not value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or re.fullmatch(r"%[A-Z_]+%", value)):
        raise CatalogueError(f"Invalid {label}: empty value or control character")
    return value


def validate_filename(filename: str) -> str:
    if not FILENAME_RE.fullmatch(filename) or filename.startswith("."):
        raise CatalogueError(f"Unsafe package filename: {filename!r}")
    return filename


def validate_repository(repository: str) -> str:
    if not REPOSITORY_RE.fullmatch(repository):
        raise CatalogueError(f"Invalid GitHub repository: {repository!r}")
    if any(part in (".", "..") for part in repository.split("/")):
        raise CatalogueError(f"Invalid GitHub repository: {repository!r}")
    return repository


def validate_download_url(url: str, repository: str | None = None) -> str:
    clean_value(url, "download URL")
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "github.com"
            or parts.query or parts.fragment or parts.username or parts.password):
        raise CatalogueError(f"Expected a GitHub HTTPS release asset URL: {url!r}")
    components = parts.path.strip("/").split("/")
    if len(components) < 6 or components[2:4] != ["releases", "download"]:
        raise CatalogueError(f"Expected a GitHub release asset URL: {url!r}")
    validate_repository("/".join(components[:2]))
    if repository and "/".join(components[:2]).lower() != repository.lower():
        raise CatalogueError(f"Asset URL does not belong to {repository}: {url!r}")
    return url


def parse_pkginfo(data: bytes) -> dict[str, list[str]]:
    try:
        contents = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CatalogueError(".PKGINFO is not UTF-8") from error
    info: dict[str, list[str]] = {}
    for line in contents.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" = ")
        if not separator or not re.fullmatch(r"[a-zA-Z_]+", key):
            raise CatalogueError(f"Malformed .PKGINFO line: {line!r}")
        info.setdefault(key, []).append(clean_value(value, f".PKGINFO {key}"))
    for key in ("pkgname", "pkgver", "arch", "size"):
        if len(info.get(key, [])) != 1:
            raise CatalogueError(f".PKGINFO must contain exactly one {key}")
    for key in ("pkgbase", "pkgdesc", "url", "packager", "builddate"):
        if len(info.get(key, [])) > 1:
            raise CatalogueError(f".PKGINFO must not repeat scalar field {key}")
    if not NAME_RE.fullmatch(info["pkgname"][0]):
        raise CatalogueError("Invalid package name")
    if info.get("pkgbase") and not NAME_RE.fullmatch(info["pkgbase"][0]):
        raise CatalogueError("Invalid package base name")
    version = info["pkgver"][0]
    if not re.fullmatch(r"[A-Za-z0-9.+_:~]+-[0-9]+(?:\.[0-9]+)*", version):
        raise CatalogueError(f"Invalid full Arch package version: {version!r}")
    if not re.fullmatch(r"[A-Za-z0-9_]+", info["arch"][0]):
        raise CatalogueError("Invalid package architecture")
    for key in ("size", "builddate"):
        if key in info and (len(info[key]) != 1 or not info[key][0].isdigit()):
            raise CatalogueError(f"Invalid numeric .PKGINFO {key}")
    for value in info.get("xdata", []):
        if "=" not in value or not value.partition("=")[0]:
            raise CatalogueError("Invalid .PKGINFO xdata value")
    return info


class LimitedReader(io.RawIOBase):
    """Limit metadata extraction, including archives with misplaced metadata."""

    def __init__(self, source: BinaryIO, limit: int, label: str):
        self.source = source
        self.remaining = limit
        self.label = label

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.remaining + 1
        size = min(size, self.remaining + 1)
        data = self.source.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise CatalogueError(f"{self.label} metadata read limit exceeded")
        return data

    def readinto(self, buffer: bytearray) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


@contextlib.contextmanager
def decompressed_stream(stream: BinaryIO, filename: str):
    if filename.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
            with zstd.ZstdFile(stream, "rb") as reader:
                yield reader
        except ImportError:
            try:
                import zstandard
            except ImportError as error:
                raise CatalogueError("Install python3-zstandard to read .pkg.tar.zst") from error
            with zstandard.ZstdDecompressor().stream_reader(stream, closefd=False) as reader:
                yield reader
    elif filename.endswith(".gz"):
        with gzip.GzipFile(fileobj=stream, mode="rb") as reader:
            yield reader
    elif filename.endswith(".xz"):
        with lzma.LZMAFile(stream, mode="rb") as reader:
            yield reader
    else:
        raise CatalogueError(f"Unsupported package compression: {filename}")


def extract_pkginfo(stream: BinaryIO, filename: str,
                    compressed_limit: int = 16 * 1024 * 1024,
                    uncompressed_limit: int = 64 * 1024 * 1024) -> dict[str, list[str]]:
    raw = LimitedReader(stream, compressed_limit, "Compressed")
    try:
        with decompressed_stream(raw, filename) as decompressed:
            bounded = LimitedReader(decompressed, uncompressed_limit, "Uncompressed")
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    if member.name in (".PKGINFO", "./.PKGINFO"):
                        if not member.isfile() or member.size > 1024 * 1024:
                            raise CatalogueError(".PKGINFO must be a regular file below 1 MiB")
                        handle = archive.extractfile(member)
                        if handle is None:
                            raise CatalogueError("Cannot read .PKGINFO")
                        return parse_pkginfo(handle.read())
    except CatalogueError:
        raise
    except Exception as error:
        raise CatalogueError(f"Cannot read metadata from {filename}: {error}") from error
    raise CatalogueError(f"No .PKGINFO in {filename}")


@dataclasses.dataclass(frozen=True)
class Package:
    filename: str
    url: str
    sha256: str
    compressed_size: int
    info: dict[str, list[str]]
    prerelease: bool = False
    source: str = ""

    @property
    def name(self) -> str:
        return self.info["pkgname"][0]

    @property
    def version(self) -> str:
        return self.info["pkgver"][0]

    @property
    def arch(self) -> str:
        return self.info["arch"][0]

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.name, self.version, self.arch


class GitHub:
    def __init__(self, state: dict[str, Any], token: str | None = None,
                 opener: Callable = urllib.request.urlopen):
        self.cache = state.setdefault("api", {})
        self.token = token
        self.opener = opener
        self.reserve_requests = 40  # Leave enough quota to checkpoint release state.

    def check_budget(self, headers) -> None:
        remaining = headers.get("X-RateLimit-Remaining", "")
        if self.token and remaining.isdigit() and int(remaining) < self.reserve_requests:
            raise CatalogueError("GitHub API budget reserved for scanner-state publication; "
                                 "the next scheduled run will resume using cached responses.")

    def get(self, url: str) -> tuple[Any, str | None]:
        if not url.startswith("https://api.github.com/"):
            raise CatalogueError(f"Refusing non-GitHub API URL: {url}")
        cached = self.cache.get(url, {})
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "OpenResearchTools-pacman-catalogue/1",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        for attempt in range(3):
            try:
                with self.opener(urllib.request.Request(url, headers=headers), timeout=60) as response:
                    data = json.load(response)
                    links = response.headers.get("Link", "")
                    next_url = None
                    for link in links.split(","):
                        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
                        if match:
                            next_url = match.group(1)
                    self.cache[url] = {"etag": response.headers.get("ETag"), "data": data, "next": next_url}
                    self.check_budget(response.headers)
                    return data, next_url
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 304 and "data" in cached:
                    self.check_budget(error.headers)
                    return cached["data"], cached.get("next")
                if error.code in (429, 403) and (error.headers.get("Retry-After") or error.headers.get("X-RateLimit-Remaining") == "0"):
                    raise CatalogueError("GitHub API rate limit reached; retry on a later scheduled run. "
                                         "Publication stopped without replacing the current catalogue.") from error
                if error.code >= 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise CatalogueError(f"GitHub API request failed ({error.code}): {url}") from error
            except urllib.error.URLError as error:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise CatalogueError(f"GitHub API request failed: {url}: {error.reason}") from error
        raise CatalogueError("Unreachable API retry state")

    def pages(self, path: str):
        url = path if path.startswith("https://") else "https://api.github.com" + path
        while url:
            data, url = self.get(url)
            if not isinstance(data, list):
                raise CatalogueError("Expected a GitHub API list")
            yield from data

    def repositories(self, config: dict[str, Any]) -> list[str]:
        explicit = config.get("repositories")
        if explicit is not None:
            repositories = [validate_repository(repository) for repository in explicit]
        elif config.get("discovery", "owner") == "owner":
            owner = config["owner"]
            if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
                raise CatalogueError("Invalid GitHub owner")
            account, _ = self.get(f"https://api.github.com/users/{owner}")
            path = f"/orgs/{owner}/repos?per_page=100&type=all" if account.get("type") == "Organization" else f"/users/{owner}/repos?per_page=100&type=owner"
            repositories = [validate_repository(repo["full_name"]) for repo in self.pages(path)
                            if not repo.get("private", False)]
        else:
            raise CatalogueError("Manifest discovery needs a repositories list or repositories_file")
        excluded = {repo.lower() for repo in config.get("exclude_repositories", [])}
        excluded.add(config["catalogue_repository"].lower())
        return sorted({repo for repo in repositories if repo.lower() not in excluded}, key=str.lower)


def remote_package(asset: dict[str, Any], repository: str, release: dict[str, Any],
                   state: dict[str, Any], config: dict[str, Any],
                   opener: Callable = urllib.request.urlopen) -> Package:
    filename = validate_filename(asset["name"])
    url = validate_download_url(asset["browser_download_url"], repository)
    if urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) != filename:
        raise CatalogueError(f"Asset name and URL disagree: {filename}")
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not SHA256_RE.fullmatch(digest[7:]):
        raise CatalogueError(f"{repository}/{filename}: GitHub has no SHA-256 digest. "
                             "Re-upload this Arch asset so GitHub records its digest.")
    sha256 = digest[7:].lower()
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise CatalogueError(f"Invalid asset size: {filename}")
    cache = state.setdefault("packages", {})
    cache_key = f"{asset['id']}:{sha256}"
    if cache_key in cache:
        info = parse_pkginfo(cache[cache_key]["pkginfo"].encode())
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "OpenResearchTools-pacman-catalogue/1", "Accept-Encoding": "identity"})
        with opener(request, timeout=60) as response:
            info = extract_pkginfo(response, filename,
                                   config.get("max_compressed_metadata_bytes", 16 * 1024 * 1024),
                                   config.get("max_uncompressed_metadata_bytes", 64 * 1024 * 1024))
        cache[cache_key] = {"pkginfo": "".join(f"{key} = {value}\n" for key, values in info.items() for value in values)}
    return Package(filename, url, sha256, size, info, bool(release.get("prerelease")),
                   f"{repository}@{release.get('tag_name', '')}")


def local_package(path: Path, url: str) -> Package:
    validate_filename(path.name)
    validate_download_url(url)
    with path.open("rb") as handle:
        info = extract_pkginfo(handle, path.name)
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return Package(path.name, url, digest, path.stat().st_size, info, False, "local bootstrap")


def deduplicate(packages: list[Package]) -> list[Package]:
    by_identity: dict[tuple[str, str, str], Package] = {}
    for package in packages:
        old = by_identity.get(package.identity)
        if old and (old.sha256 != package.sha256 or old.compressed_size != package.compressed_size):
            raise CatalogueError(f"Conflicting bytes for {package.identity}: {old.source} and {package.source}")
        if old is None or (old.prerelease and not package.prerelease):
            by_identity[package.identity] = package
    return list(by_identity.values())


class VersionComparator:
    def __init__(self, command: str = "vercmp"):
        self.command = command
        self.cache: dict[tuple[str, str], int] = {}

    def __call__(self, first: str, second: str) -> int:
        if first == second:
            return 0
        key = first, second
        if key not in self.cache:
            try:
                result = subprocess.run([self.command, first, second], text=True, capture_output=True, check=True)
                value = int(result.stdout.strip())
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                raise CatalogueError("Arch's vercmp is required to choose package versions") from error
            self.cache[key] = (value > 0) - (value < 0)
            self.cache[(second, first)] = -self.cache[key]
        return self.cache[key]


def select_packages(packages: list[Package], architecture: str, channel: str,
                    compare: Callable[[str, str], int]) -> list[Package]:
    if channel not in ("stable", "nightly", "testing"):
        raise CatalogueError(f"Unknown channel: {channel}")
    selected: dict[str, Package] = {}
    eligible = [package for package in packages
                if package.arch in (architecture, "any")
                and (channel != "stable" or not package.prerelease)
                and (channel != "nightly" or package.prerelease or package.source == "local bootstrap")]
    for package in deduplicate(eligible):
        old = selected.get(package.name)
        comparison = compare(package.version, old.version) if old else 1
        if comparison > 0 or (comparison == 0 and old and old.arch == "any" and package.arch == architecture):
            selected[package.name] = package
    filenames: dict[str, Package] = {}
    for package in selected.values():
        if package.filename in filenames and filenames[package.filename].identity != package.identity:
            raise CatalogueError(f"Ambiguous flat filename in {channel}/{architecture}: {package.filename}")
        filenames[package.filename] = package
    return [selected[name] for name in sorted(selected)]


def render_desc(package: Package) -> bytes:
    sections: list[tuple[str, list[str]]] = [("FILENAME", [validate_filename(package.filename)])]
    for source, destination in FIELD_MAP:
        if package.info.get(source):
            sections.append((destination, package.info[source]))
    sections.extend((("CSIZE", [str(package.compressed_size)]), ("SHA256SUM", [package.sha256])))
    data = [value for value in package.info.get("xdata", []) if not value.startswith("ort_download_url=")]
    data.append("ort_download_url=" + validate_download_url(package.url))
    sections.append(("DATA", data))
    return "".join(f"%{key}%\n" + "\n".join(clean_value(value, key) for value in values) + "\n\n"
                   for key, values in sections).encode()


def write_database(packages: list[Package], path: Path, epoch: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for package in packages:
                    data = render_desc(package)
                    item = tarfile.TarInfo(f"{package.name}-{package.version}/desc")
                    item.size = len(data)
                    item.mtime = epoch
                    item.mode = 0o644
                    item.uid = item.gid = 0
                    archive.addfile(item, io.BytesIO(data))
    temporary.replace(path)


def load_state(path: Path | None) -> dict[str, Any]:
    if path and path.exists():
        data = json.loads(path.read_text())
        if data.get("schema") != 1:
            raise CatalogueError("Unknown publisher state schema")
        return data
    return {"schema": 1, "api": {}, "packages": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


def build_catalogues(config: dict[str, Any], packages: list[Package], output: Path,
                     compare: Callable[[str, str], int], epoch: int = 0) -> dict[str, Any]:
    validate_repository(config["catalogue_repository"])
    repository_name = config.get("repository_name", "openresearchtools")
    if not NAME_RE.fullmatch(repository_name):
        raise CatalogueError("Invalid pacman repository name")
    prefix = config.get("release_tag_prefix", "pacman")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", prefix):
        raise CatalogueError("Invalid catalogue release tag prefix")
    # Detect reused identities with different bytes across channels as well.
    deduplicate(packages)
    summary: dict[str, Any] = {"catalogue_repository": config["catalogue_repository"], "catalogues": {}}
    planned = []
    for channel in ("stable", "nightly", "testing"):
        for architecture in config.get("architectures", ["x86_64", "aarch64"]):
            if architecture not in ("x86_64", "aarch64"):
                raise CatalogueError(f"Unsupported target architecture: {architecture}")
            tag = f"{prefix}-{channel}-{architecture}"
            chosen = select_packages(packages, architecture, channel, compare)
            # Render everything before replacing output files: a bad package fails closed.
            for package in chosen:
                render_desc(package)
            planned.append((tag, chosen))
            summary["catalogues"][tag] = {"count": len(chosen), "packages": [
                {"name": package.name, "version": package.version, "arch": package.arch,
                 "filename": package.filename, "url": package.url, "sha256": package.sha256}
                for package in chosen]}
    for tag, chosen in planned:
        write_database(chosen, output / tag / f"{repository_name}.db", epoch)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("catalogue.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--vercmp", default="vercmp")
    parser.add_argument("--local-package", type=Path, action="append", default=[])
    parser.add_argument("--local-url", action="append", default=[])
    args = parser.parse_args(argv)
    state = None
    try:
        if len(args.local_package) != len(args.local_url):
            raise CatalogueError("Every --local-package needs a matching --local-url, in the same order")
        config = json.loads(args.config.read_text())
        if config.get("repositories") is None and config.get("discovery", "owner") == "manifest":
            manifest = args.config.parent / config["repositories_file"]
            entries = json.loads(manifest.read_text())
            config["repositories"] = [entry["repository"] for entry in entries]
        validate_repository(config["catalogue_repository"])
        state = load_state(args.state)
        github = GitHub(state, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        repositories = github.repositories(config)
        packages = []
        checkpoint_at = time.monotonic()
        for number, repository in enumerate(repositories, 1):
            print(f"Scanning {number}/{len(repositories)}: {repository}", file=sys.stderr)
            for release in github.pages(f"/repos/{repository}/releases?per_page=100"):
                if release.get("draft"):
                    continue
                for asset in release.get("assets", []):
                    if any(fnmatch.fnmatchcase(asset.get("name", ""), pattern)
                           for pattern in config.get("asset_globs", ["*.pkg.tar.zst", "*.pkg.tar.xz", "*.pkg.tar.gz"])):
                        packages.append(remote_package(asset, repository, release, state, config))
            if args.state and time.monotonic() - checkpoint_at >= 30:
                save_state(args.state, state)
                checkpoint_at = time.monotonic()
        for path, url in zip(args.local_package, args.local_url):
            packages.append(local_package(path, url))
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
        summary = build_catalogues(config, packages, args.output, VersionComparator(args.vercmp), epoch)
        if args.state:
            save_state(args.state, state)
        for tag, catalogue in summary["catalogues"].items():
            print(f"{tag}: {catalogue['count']} package(s)")
        if not packages:
            print("No native Arch release assets found. Existing .deb files are not installable by pacman.", file=sys.stderr)
        return 0
    except (CatalogueError, KeyError, ValueError, OSError) as error:
        if args.state and state is not None:
            try:
                save_state(args.state, state)
            except OSError as cache_error:
                print(f"Could not checkpoint publisher cache: {cache_error}", file=sys.stderr)
        print(f"Catalogue publication failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
