#!/usr/bin/env python3
"""Build the small architecture-independent pacman setup package, without root."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile


SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent


def build(output: Path, repository: str, version: str, epoch: int = 0) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be a GitHub owner/repository")
    if not re.fullmatch(r"[0-9][A-Za-z0-9._+]*", version):
        raise ValueError("invalid setup package version")
    defaults = {
        "catalogue_repository": repository,
        "channel": "stable",
        "allowed_owners": [repository.split("/")[0]],
        "db_path": "/var/lib/pacman/sync/openresearchtools.db",
    }
    payload = {
        "usr/bin/openresearchtools-pacman-fetch": ((SOURCE / "fetch.py").read_bytes(), 0o755),
        "usr/bin/openresearchtools-pacman-setup": ((SOURCE / "setup.py").read_bytes(), 0o755),
        "usr/share/openresearchtools-pacman/defaults.json": (
            (json.dumps(defaults, indent=2, sort_keys=True) + "\n").encode(), 0o644),
        "usr/share/pacman/keyrings/openresearchtools.gpg": (
            (ROOT / "keys/openresearchtools-archive-keyring.gpg").read_bytes(), 0o644),
    }
    info = (
        "pkgname = openresearchtools-pacman\n"
        "pkgbase = openresearchtools-pacman\n"
        f"pkgver = {version}-1\n"
        "pkgdesc = Signed GitHub-only Open Research Tools pacman catalogue support\n"
        f"url = https://github.com/{repository}\n"
        f"builddate = {epoch}\n"
        "packager = Open Research Tools <packages@openresearchtools.com>\n"
        f"size = {sum(len(data) for data, _ in payload.values())}\n"
        "arch = any\n"
        "license = MIT\n"
        "depend = python\n"
        "depend = curl\n"
        "depend = pacman>=7.1\n"
    ).encode()
    payload[".PKGINFO"] = (info, 0o644)
    # A real mtree lets pacman -Qkk check the installed setup files normally.
    mtree = ["#mtree"]
    for name, (data, mode) in sorted(payload.items()):
        if name.startswith("."):
            continue
        mtree.append(f"./{name} type=file uid=0 gid=0 mode={mode:o} size={len(data)} "
                     f"sha256digest={hashlib.sha256(data).hexdigest()} time={epoch}.0")
    payload[".MTREE"] = (gzip.compress(("\n".join(mtree) + "\n").encode(), mtime=0), 0o644)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"openresearchtools-pacman-{version}-1-any.pkg.tar.gz"
    with destination.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, (data, mode) in sorted(payload.items()):
                    member = tarfile.TarInfo(name)
                    member.uid = member.gid = 0
                    member.uname = member.gname = "root"
                    member.mode = mode
                    member.mtime = epoch
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "openresearchtools/apt"))
    parser.add_argument("--version", default=(SOURCE / "VERSION").read_text().strip())
    args = parser.parse_args()
    print(build(args.output, args.repository, args.version,
                int(os.environ.get("SOURCE_DATE_EPOCH", "0"))).resolve())


if __name__ == "__main__":
    main()
