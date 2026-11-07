#!/usr/bin/env python3
"""Offline publisher tests: no GitHub tokens or upstream requests needed."""

import dataclasses
import base64
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalogue as c
import publish as pub


def info_bytes(name="demo", version="1.0-1", arch="aarch64", extra=""):
    return (f"pkgname = {name}\npkgbase = {name}\npkgver = {version}\n"
            f"pkgdesc = Test package\narch = {arch}\nsize = 5\n"
            "builddate = 1\nlicense = MIT\ndepend = glibc>=2.39\n"
            "depend = zlib\noptdepend = ffmpeg: video support\n"
            + extra).encode()


def package(name="demo", version="1.0-1", arch="aarch64", prerelease=False,
            digest="a" * 64, filename=None):
    filename = filename or f"{name}-{version}-{arch}.pkg.tar.gz"
    return c.Package(filename,
                     f"https://github.com/openresearchtools/{name}/releases/download/v1/{filename}",
                     digest, 150, c.parse_pkginfo(info_bytes(name, version, arch)),
                     prerelease, name)


def archive_bytes(metadata=None, before=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        entries = [] if before is None else [("payload", before)]
        entries.append((".PKGINFO", metadata or info_bytes()))
        for name, data in entries:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def ordered(first, second):
    order = {"1.0-1": 0, "1.1rc1-1": 1, "1.1-1": 2, "1.2-1": 3}
    return (order[first] > order[second]) - (order[first] < order[second])


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class PackageMetadataTests(unittest.TestCase):
    def test_metadata_preserves_dependencies_and_xdata(self):
        result = c.extract_pkginfo(io.BytesIO(archive_bytes(info_bytes(extra="xdata = pkgtype=pkg\n"))),
                                   "demo.pkg.tar.gz")
        self.assertEqual(result["depend"], ["glibc>=2.39", "zlib"])
        self.assertEqual(result["optdepend"], ["ffmpeg: video support"])
        self.assertEqual(result["xdata"], ["pkgtype=pkg"])

    def test_extracts_without_reading_later_payload(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            data = info_bytes()
            item = tarfile.TarInfo(".PKGINFO"); item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
            # Uncompressible later content should never be downloaded.
            payload = random.Random(0).randbytes(1024 * 1024)
            item = tarfile.TarInfo("payload"); item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
        source = io.BytesIO(output.getvalue())
        self.assertEqual(c.extract_pkginfo(source, "demo.pkg.tar.gz")["pkgname"], ["demo"])
        self.assertLess(source.tell(), len(output.getvalue()))

    def test_xz_package_metadata(self):
        contents = lzma.compress(gzip.decompress(archive_bytes()))
        self.assertEqual(c.extract_pkginfo(io.BytesIO(contents), "demo.pkg.tar.xz")["pkgname"], ["demo"])

    @unittest.skipUnless(shutil.which("zstd"), "zstd compressor not installed")
    def test_zstd_package_metadata(self):
        contents = subprocess.run(["zstd", "-q", "-c"], input=gzip.decompress(archive_bytes()),
                                  capture_output=True, check=True).stdout
        self.assertEqual(c.extract_pkginfo(io.BytesIO(contents), "demo.pkg.tar.zst")["pkgname"], ["demo"])

    def test_decompressed_limit_rejects_large_member_before_pkginfo(self):
        with self.assertRaisesRegex(c.CatalogueError, "Uncompressed"):
            c.extract_pkginfo(io.BytesIO(archive_bytes(before=b"0" * 200000)),
                              "demo.pkg.tar.gz", uncompressed_limit=65536)

    def test_compressed_limit_is_enforced(self):
        with self.assertRaisesRegex(c.CatalogueError, "Compressed"):
            c.extract_pkginfo(io.BytesIO(archive_bytes()), "demo.pkg.tar.gz", compressed_limit=10)

    def test_rejects_symlink_pkginfo(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            item = tarfile.TarInfo(".PKGINFO"); item.type = tarfile.SYMTYPE; item.linkname = "/etc/passwd"
            archive.addfile(item)
        with self.assertRaisesRegex(c.CatalogueError, "regular file"):
            c.extract_pkginfo(io.BytesIO(output.getvalue()), "demo.pkg.tar.gz")

    def test_malicious_names_are_rejected(self):
        for filename in ("../demo.pkg.tar.gz", "demo/file.pkg.tar.gz", ".hidden.pkg.tar.gz", "demo\n.pkg.tar.gz"):
            with self.subTest(filename=filename), self.assertRaises(c.CatalogueError):
                c.validate_filename(filename)
        with self.assertRaises(c.CatalogueError):
            c.parse_pkginfo(info_bytes(name="../demo"))
        with self.assertRaises(c.CatalogueError):
            c.parse_pkginfo(info_bytes(version="1.0/../../bad-1"))

    def test_duplicate_scalar_fields_are_rejected(self):
        with self.assertRaisesRegex(c.CatalogueError, "scalar field pkgdesc"):
            c.parse_pkginfo(info_bytes(extra="pkgdesc = another description\n"))

    def test_download_urls_must_be_github_release_assets(self):
        for url in ("http://github.com/org/repo/releases/download/tag/a.pkg.tar.gz",
                    "https://evil.invalid/org/repo/releases/download/tag/a.pkg.tar.gz",
                    "https://github.com/org/repo/raw/main/a.pkg.tar.gz",
                    "https://github.com/org/repo/releases/download/tag/a.pkg.tar.gz?token=secret"):
            with self.subTest(url=url), self.assertRaises(c.CatalogueError):
                c.validate_download_url(url)

    def test_local_package_records_archive_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo-1.0-1-aarch64.pkg.tar.gz"
            path.write_bytes(archive_bytes())
            result = c.local_package(path, f"https://github.com/org/app/releases/download/v1/{path.name}")
            self.assertEqual(result.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(result.compressed_size, path.stat().st_size)


class SelectionTests(unittest.TestCase):
    def test_stable_nightly_and_merged_testing_selection(self):
        candidates = [package(), package(version="1.1rc1-1", prerelease=True)]
        self.assertEqual(c.select_packages(candidates, "aarch64", "stable", ordered)[0].version, "1.0-1")
        self.assertEqual(c.select_packages(candidates, "aarch64", "nightly", ordered)[0].version, "1.1rc1-1")
        self.assertEqual(c.select_packages(candidates, "aarch64", "testing", ordered)[0].version, "1.1rc1-1")
        candidates.append(package(version="1.1-1"))
        self.assertEqual(c.select_packages(candidates, "aarch64", "testing", ordered)[0].version, "1.1-1")
        self.assertEqual(c.select_packages(candidates, "aarch64", "nightly", ordered)[0].version, "1.1rc1-1")

    def test_any_architecture_and_native_override(self):
        candidates = [package(name="shared", arch="any"), package(arch="x86_64"), package()]
        arm = c.select_packages(candidates, "aarch64", "stable", ordered)
        self.assertEqual([(p.name, p.arch) for p in arm], [("demo", "aarch64"), ("shared", "any")])
        native = c.select_packages([package(arch="any"), package()], "aarch64", "stable", ordered)
        self.assertEqual(native[0].arch, "aarch64")

    def test_carried_forward_identical_binary_is_deduplicated(self):
        stable = package()
        earlier = dataclasses.replace(stable, prerelease=True, source="old tag")
        result = c.deduplicate([earlier, stable])
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].prerelease)

    def test_same_identity_with_different_content_fails(self):
        with self.assertRaisesRegex(c.CatalogueError, "Conflicting bytes"):
            c.deduplicate([package(), package(digest="b" * 64)])

    def test_identical_binary_can_be_in_both_raw_channels(self):
        stable = package()
        prerelease = dataclasses.replace(stable, prerelease=True)
        self.assertEqual(len(c.select_packages([stable, prerelease], "aarch64", "stable", ordered)), 1)
        self.assertEqual(len(c.select_packages([stable, prerelease], "aarch64", "nightly", ordered)), 1)

    def test_flat_filename_collision_fails(self):
        first = package(filename="generic.pkg.tar.gz")
        second = package(name="different", filename="generic.pkg.tar.gz")
        with self.assertRaisesRegex(c.CatalogueError, "Ambiguous flat filename"):
            c.select_packages([first, second], "aarch64", "stable", ordered)

    @unittest.skipUnless(shutil.which("vercmp"), "Arch vercmp not installed")
    def test_actual_arch_version_comparison(self):
        compare = c.VersionComparator()
        self.assertLess(compare("1.1rc20261107-1", "1.1-1"), 0)
        self.assertGreater(compare("1:1.0-1", "2.0-1"), 0)


class DatabaseTests(unittest.TestCase):
    def test_desc_has_supported_signed_url_extension_and_dependencies(self):
        demo = package()
        demo.info["xdata"] = ["pkgtype=pkg", "ort_download_url=https://old.invalid"]
        desc = c.render_desc(demo).decode()
        self.assertIn("%DATA%\npkgtype=pkg\nort_download_url=" + demo.url + "\n", desc)
        self.assertNotIn("%ORT_DOWNLOAD_URL%", desc)
        self.assertNotIn("old.invalid", desc)
        self.assertIn("%DEPENDS%\nglibc>=2.39\nzlib\n", desc)
        self.assertIn("%SHA256SUM%\n" + "a" * 64 + "\n", desc)

    def test_writes_six_deterministic_static_catalogues(self):
        config = {"catalogue_repository": "openresearchtools/apt", "architectures": ["aarch64", "x86_64"]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary = c.build_catalogues(config, [package(arch="any")], output, ordered)
            self.assertEqual(len(summary["catalogues"]), 6)
            path = output / "pacman-stable-aarch64/openresearchtools.db"
            first = path.read_bytes()
            c.build_catalogues(config, [package(arch="any")], output, ordered)
            self.assertEqual(first, path.read_bytes())
            with tarfile.open(path) as archive:
                self.assertEqual(archive.getnames(), ["demo-1.0-1/desc"])
                self.assertIn(b"ort_download_url=", archive.extractfile(archive.getmembers()[0]).read())


class DiscoveryTests(unittest.TestCase):
    def test_native_asset_metadata_is_cached_by_id_and_digest(self):
        asset = {"id": 9, "name": "demo-1.0-1-aarch64.pkg.tar.gz",
                 "browser_download_url": "https://github.com/org/app/releases/download/v1/demo-1.0-1-aarch64.pkg.tar.gz",
                 "digest": "sha256:" + "a" * 64, "size": 100}
        state = {}
        requests = []
        def opener(request, **kwargs):
            requests.append(request.full_url)
            return Response(archive_bytes())
        first = c.remote_package(asset, "org/app", {"prerelease": True}, state, {}, opener)
        second = c.remote_package(asset, "org/app", {"prerelease": False}, state, {}, opener)
        self.assertEqual(len(requests), 1)
        self.assertTrue(first.prerelease)
        self.assertFalse(second.prerelease)

    def test_missing_remote_hash_fails_closed(self):
        asset = {"id": 1, "name": "demo.pkg.tar.gz", "digest": None,
                 "browser_download_url": "https://github.com/org/app/releases/download/v1/demo.pkg.tar.gz", "size": 1}
        with self.assertRaisesRegex(c.CatalogueError, "no SHA-256"):
            c.remote_package(asset, "org/app", {}, {}, {})

    def test_api_conditional_cache_and_pagination(self):
        calls = []
        state = {}
        def opener(request, **kwargs):
            calls.append(request)
            if len(calls) == 2:
                raise urllib.error.HTTPError(request.full_url, 304, "Not modified", {}, None)
            return Response(b'[1]', {"ETag": '"abc"', "Link": '<https://api.github.com/next>; rel="next"'})
        api = c.GitHub(state, token="not-logged", opener=opener)
        self.assertEqual(api.get("https://api.github.com/example"), ([1], "https://api.github.com/next"))
        self.assertEqual(api.get("https://api.github.com/example"), ([1], "https://api.github.com/next"))
        self.assertEqual(calls[1].get_header("If-none-match"), '"abc"')
        self.assertNotIn("not-logged", json.dumps(state))

    def test_user_and_organization_discovery_exclude_catalogue(self):
        for account_type, expected in (("User", "/users/org/repos"), ("Organization", "/orgs/org/repos")):
            calls = []
            class API(c.GitHub):
                def get(self, url):
                    return {"type": account_type}, None
                def pages(self, path):
                    calls.append(path)
                    return iter([{"full_name": "org/app"}, {"full_name": "org/catalogue"}, {"full_name": "org/private", "private": True}])
            api = API({})
            result = api.repositories({"owner": "org", "catalogue_repository": "org/catalogue"})
            self.assertEqual(result, ["org/app"])
            self.assertTrue(calls[0].startswith(expected))

    def test_api_preserves_last_response_before_reserving_checkpoint_budget(self):
        state = {}
        api = c.GitHub(state, token="test", opener=lambda *args, **kwargs:
                       Response(b'[]', {"X-RateLimit-Remaining": "39", "ETag": '"last"'}))
        with self.assertRaisesRegex(c.CatalogueError, "reserved"):
            api.get("https://api.github.com/example")
        self.assertEqual(state["api"]["https://api.github.com/example"]["data"], [])

    def test_failed_scan_checkpoints_cache_without_generating_catalogues(self):
        class API:
            def __init__(self, state, token):
                self.state = state
            def repositories(self, config):
                return ["org/app"]
            def pages(self, path):
                self.state["api"]["remembered"] = {"data": ["validated metadata"]}
                raise c.CatalogueError("API unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({"catalogue_repository": "org/catalogue"}))
            with mock.patch.object(c, "GitHub", API), mock.patch("sys.stderr", new=io.StringIO()):
                result = c.main(["--config", str(config), "--output", str(root / "output"),
                                 "--state", str(root / "state.json")])
            self.assertEqual(result, 1)
            self.assertIn("remembered", json.loads((root / "state.json").read_text())["api"])
            self.assertFalse((root / "output").exists())


class PublishTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gpg") and shutil.which("gpgconf"), "GnuPG not installed")
    def test_existing_encrypted_subkey_format_signs_and_checks_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix="ort-signing-envelope-test-") as directory:
            root = Path(directory)
            home = root / "gpg"; home.mkdir(mode=0o700)
            environment = dict(os.environ, GNUPGHOME=str(home))
            password = "fixture-only-envelope-password"
            def gpg(*arguments, data=None):
                return subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", *arguments],
                                      env=environment, input=data, capture_output=True, check=True).stdout
            try:
                gpg("--passphrase", "", "--quick-generate-key", "ORT fixture <fixture@example.invalid>", "ed25519", "cert", "0")
                listing = gpg("--with-colons", "--list-secret-keys").decode()
                fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
                gpg("--passphrase", "", "--quick-add-key", fingerprint, "ed25519", "sign", "0")
                exported = root / "subkey-export.gpg"
                exported.write_bytes(gpg("--export-secret-subkeys", fingerprint))
                exported.chmod(0o600)
                encrypted = root / "encrypted-subkey.gpg"
                gpg("--passphrase-fd", "0", "--output", str(encrypted), "--symmetric", str(exported), data=(password + "\n").encode())
                secrets = {
                    "APT_GPG_SIGNING_SUBKEY_B64": base64.b64encode(encrypted.read_bytes()).decode(),
                    "APT_KEY_PASSWORD": password,
                }
                payload = root / "openresearchtools.db"; payload.write_bytes(b"fixture catalogue")
                with mock.patch.dict(os.environ, secrets):
                    with pub.signing_environment(fingerprint) as signing:
                        signature = pub.sign(payload, signing, fingerprint)
                        self.assertTrue(signature.is_file())
                    with self.assertRaisesRegex(pub.PublishError, "does not match"):
                        with pub.signing_environment("0" * 40):
                            self.fail("Mismatched signing key was accepted")
                with mock.patch.dict(os.environ, {**secrets, "APT_KEY_PASSWORD": "wrong-fixture-password"}):
                    with self.assertRaisesRegex(pub.PublishError, "Could not decrypt"):
                        with pub.signing_environment(fingerprint):
                            self.fail("Wrong envelope password was accepted")
            finally:
                subprocess.run(["gpgconf", "--kill", "all"], env=environment, capture_output=True)

    def test_atomic_bundle_contains_exact_database_and_matching_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "openresearchtools.db"
            signature = root / "openresearchtools.db.sig"
            database.write_bytes(b"exact signed database bytes")
            signature.write_bytes(b"matching detached signature bytes")
            bundle = pub.write_bundle(database, signature)
            with tarfile.open(bundle) as archive:
                self.assertEqual(archive.getnames(), [database.name, signature.name])
                self.assertEqual(archive.extractfile(database.name).read(), database.read_bytes())
                self.assertEqual(archive.extractfile(signature.name).read(), signature.read_bytes())

    def test_changed_bootstrap_cannot_overwrite_same_package_version(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "setup-0.1.0-1-any.pkg.tar.gz"
            package.write_bytes(b"new bytes")
            release = {"assets": [{"name": package.name, "digest": "sha256:" + "0" * 64}]}
            with self.assertRaisesRegex(pub.PublishError, "bump pacman/VERSION"):
                pub.check_immutable_bootstrap(release, [package])
            release["assets"][0]["digest"] = "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest()
            pub.check_immutable_bootstrap(release, [package])

    def test_dry_run_does_not_access_signing_key_or_remote_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            c.build_catalogues({"catalogue_repository": "org/catalogue", "architectures": ["aarch64"]},
                               [package()], output, ordered)
            bootstrap = root / "bootstrap"; bootstrap.mkdir()
            (bootstrap / "setup-0.1.0-1-any.pkg.tar.gz").write_bytes(b"not read on dry run")
            release = mock.Mock(repository="org/catalogue")
            with mock.patch.object(pub, "signing_environment", side_effect=AssertionError("must not import key")), mock.patch("sys.stdout", new=io.StringIO()):
                pub.publish_catalogues(release, output, bootstrap, pub.FINGERPRINT,
                                       root / "missing-public-key", dry_run=True)
            self.assertEqual(release.method_calls, [])


if __name__ == "__main__":
    unittest.main()
