#!/usr/bin/env python3
"""Exercise real pacman against signed fixture catalogues; no system changes."""

from __future__ import annotations

import functools
import gzip
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import catalogue


def run(command, **kwargs):
    result = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise AssertionError(f"{command[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result


def fixture_package(path: Path, version: str):
    info = (
        "pkgname = ort-integration\npkgbase = ort-integration\n"
        f"pkgver = {version}-1\npkgdesc = Isolated catalogue integration fixture\n"
        "size = 8\narch = any\nlicense = MIT\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, contents in ((".PKGINFO", info), ("usr/share/ort-integration/test.txt", b"fixture\n")):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(contents))


def main():
    for command in ("pacman", "gpg", "gpgconf", "curl", "vercmp"):
        if not shutil.which(command):
            raise SystemExit(f"Integration test requires {command}")
    if os.geteuid() != 0 and not shutil.which("fakeroot"):
        raise SystemExit("Integration test requires fakeroot when run without root")
    with tempfile.TemporaryDirectory(prefix="ort-pacman-integration-") as directory:
        work = Path(directory)
        web = work / "web"
        for name in ("root", "cache", "db", "gpg"):
            (work / name).mkdir()
        (work / "gpg").chmod(0o700)
        environment = dict(os.environ, GNUPGHOME=str(work / "gpg"))
        run(["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
             "--quick-generate-key", "ORT fixture <fixture@example.invalid>", "ed25519", "sign", "0"],
            env=environment)
        keys = run(["gpg", "--batch", "--with-colons", "--list-keys"], env=environment).stdout
        fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))
        packages = []
        for version, prerelease in (("1.0", False), ("1.1rc1", True)):
            relative = Path("openresearchtools/fixture/releases/download") / version / f"ort-integration-{version}-1-any.pkg.tar.gz"
            archive = web / relative
            fixture_package(archive, version)
            package = catalogue.local_package(archive, "https://github.com/" + relative.as_posix())
            packages.append(catalogue.dataclasses.replace(package, prerelease=prerelease, source="fixture"))
        config = {"catalogue_repository": "openresearchtools/apt", "architectures": ["aarch64", "x86_64"]}
        output = work / "catalogues"
        catalogue.build_catalogues(config, packages, output, catalogue.VersionComparator())
        architecture = "aarch64" if os.uname().machine in ("aarch64", "arm64") else "x86_64"
        for channel in ("stable", "nightly", "testing"):
            tag = f"pacman-{channel}-{architecture}"
            target = web / "openresearchtools/apt/releases/download" / tag
            target.mkdir(parents=True)
            database = output / tag / "openresearchtools.db"
            signature = database.with_suffix(".db.sig")
            run(["gpg", "--batch", "--yes", "--local-user", fingerprint, "--output", str(signature),
                 "--detach-sign", str(database)], env=environment)
            with tarfile.open(target / "openresearchtools.catalogue.tar.gz", "w:gz") as bundle:
                bundle.add(database, arcname="openresearchtools.db")
                bundle.add(signature, arcname="openresearchtools.db.sig")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(web)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        # Production URL validation remains enabled. Only the test transport is
        # redirected to the isolated fixture HTTP server.
        wrapper = work / "fixture-fetch.py"
        wrapper.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SOURCE)!r})\n"
            "import fetch\n"
            "original = fetch.download\n"
            "def transport(url, output, maximum_size=None):\n"
            "    if url.startswith('https://github.com/'):\n"
            f"        url = {origin!r} + url[len('https://github.com'):]\n"
            "    return original(url, output, maximum_size)\n"
            "fetch.download = transport\n"
            "raise SystemExit(fetch.main())\n"
        )
        helper_config = work / "helper.json"
        pacman_config = work / "pacman.conf"
        command = ([] if os.geteuid() == 0 else ["fakeroot"]) + [
            "pacman", "--config", str(pacman_config), "-Syw", "--noconfirm", "ort-integration"]
        try:
            for channel, expected in (("stable", "1.0"), ("nightly", "1.1rc1"), ("testing", "1.1rc1")):
                helper_config.write_text(json.dumps({
                    "catalogue_repository": "openresearchtools/apt", "channel": channel,
                    "architecture": architecture, "db_path": str(work / "db/sync/openresearchtools.db"),
                    "allowed_owners": ["openresearchtools"],
                }))
                pacman_config.write_text(
                    "[options]\n" + "\n".join(f"{key} = {value}" for key, value in {
                        "RootDir": work / "root", "DBPath": work / "db", "CacheDir": work / "cache",
                        "GPGDir": work / "gpg", "LogFile": work / "pacman.log", "Architecture": "auto",
                    }.items()) + "\n"
                    f"XferCommand = {sys.executable} {wrapper} --config {helper_config} %u %o\n"
                    "[openresearchtools]\nSigLevel = PackageNever DatabaseRequired TrustedOnly\n"
                    f"Server = https://github.com/openresearchtools/apt/releases/download/pacman-{channel}-{architecture}\n"
                )
                run(command, env=environment)
                selected = next(package for package in packages if package.version == expected + "-1")
                assert (work / "cache" / selected.filename).read_bytes() == (web / selected.url.removeprefix("https://github.com/")).read_bytes()
                print(f"PASS: {channel} signed bundle resolves original release and selects {expected}")
            selected = packages[1]
            cached = work / "cache" / selected.filename
            cached.unlink()
            upstream = web / selected.url.removeprefix("https://github.com/")
            upstream.write_bytes(upstream.read_bytes() + b"altered")
            failure = subprocess.run(command, text=True, capture_output=True, env=environment)
            assert failure.returncode != 0 and "checksum" in (failure.stdout + failure.stderr).lower(), failure
            print("PASS: modified package is rejected against the signed catalogue checksum")
            # A bogus replacement signature cannot make an untrusted bundle usable.
            tag = f"pacman-testing-{architecture}"
            bundle_path = web / "openresearchtools/apt/releases/download" / tag / "openresearchtools.catalogue.tar.gz"
            with tarfile.open(bundle_path, "w:gz") as bundle:
                bundle.add(output / tag / "openresearchtools.db", arcname="openresearchtools.db")
                bad = tarfile.TarInfo("openresearchtools.db.sig")
                bad.size = 7
                bundle.addfile(bad, io.BytesIO(b"invalid"))
            signature_failure = subprocess.run(command, text=True, capture_output=True, env=environment)
            assert signature_failure.returncode != 0
            print("PASS: invalid catalogue signature is rejected by pacman")
        finally:
            server.shutdown()
            server.server_close()
            subprocess.run(["gpgconf", "--kill", "all"], env=environment, capture_output=True)


if __name__ == "__main__":
    main()
