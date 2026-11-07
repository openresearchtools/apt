import importlib.util
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACMAN_DIR = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load_module("fetch_under_test", PACMAN_DIR / "fetch.py")
setup = load_module("setup_under_test", PACMAN_DIR / "setup.py")


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "openresearchtools.db"
        self.config = fetch.validate_configuration({
            "architecture": "aarch64", "db_path": str(self.db)
        })
        self.filename = "test-app-1.2.3-1-aarch64.pkg.tar.zst"
        self.asset = "https://github.com/openresearchtools/test-app/releases/download/v1.2.3/" + self.filename
        self.url = fetch.server_url(self.config) + "/" + self.filename
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        self.payload = b"fixture package bytes"
        self.sha256 = hashlib.sha256(self.payload).hexdigest()

    def database(self, entries, integrity=None):
        if integrity is None:
            integrity = {"SHA256SUM": [self.sha256], "CSIZE": [str(len(self.payload))]}
        with tarfile.open(self.db, "w:gz") as archive:
            for index, (filename, routes) in enumerate(entries):
                text = f"%FILENAME%\n{filename}\n\n"
                for key, values in integrity.items():
                    text += f"%{key}%\n" + "\n".join(values) + "\n\n"
                text += "%DATA%\nother=value\n"
                text += "\n".join("ort_download_url=" + route for route in routes) + "\n\n"
                data = text.encode()
                member = tarfile.TarInfo(f"app-{index}/desc")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_uses_current_signed_database_route(self):
        self.database([(self.filename, [self.asset])])
        self.assertEqual(fetch.resolve_url(self.url, self.config), self.asset)
        self.assertEqual(fetch.catalogue_package(self.filename, self.config),
                         (self.asset, self.sha256, len(self.payload)))

    def test_nightly_selects_the_configured_release_prefix(self):
        self.config["channel"] = "nightly"
        self.database([(self.filename, [self.asset])])
        nightly = fetch.server_url(self.config) + "/" + self.filename
        self.assertEqual(fetch.resolve_url(nightly, self.config), self.asset)
        self.assertEqual(fetch.resolve_url(self.url, self.config), self.url)

    def test_testing_is_an_explicit_separate_channel(self):
        config = fetch.validate_configuration(dict(self.config, channel="testing"))
        self.assertTrue(fetch.server_url(config).endswith("pacman-testing-aarch64"))
        self.assertEqual(fetch.validate_configuration(dict(self.config))["channel"], "stable")

    def test_metadata_and_unrelated_repositories_do_not_need_database(self):
        for url in [
            fetch.server_url(self.config) + "/openresearchtools.db",
            fetch.server_url(self.config) + "/openresearchtools.db.sig",
            "https://mirror.omarchy.org/core/os/aarch64/example.pkg.tar.zst",
        ]:
            with self.subTest(url=url):
                self.assertEqual(fetch.resolve_url(url, self.config), url)

    def test_package_signatures_follow_the_same_exact_asset(self):
        self.database([(self.filename, [self.asset])])
        self.assertEqual(fetch.resolve_url(self.url + ".sig", self.config), self.asset + ".sig")

    def test_missing_duplicate_and_ambiguous_routes_fail_closed(self):
        for entries in [
            [], [(self.filename, [])], [(self.filename, [self.asset, self.asset])],
            [(self.filename, [self.asset]), (self.filename, [self.asset])],
        ]:
            with self.subTest(entries=entries):
                self.database(entries)
                with self.assertRaises(fetch.FetchError):
                    fetch.resolve_url(self.url, self.config)

    def test_corrupt_database_and_oversized_description_fail(self):
        self.db.write_bytes(b"not a tar archive")
        with self.assertRaises(fetch.FetchError):
            fetch.resolve_url(self.url, self.config)
        with tarfile.open(self.db, "w:gz") as archive:
            member = tarfile.TarInfo("app/desc")
            member.size = fetch.MAX_DESC_BYTES + 1
            archive.addfile(member, io.BytesIO(b"x" * member.size))
        with self.assertRaises(fetch.FetchError):
            fetch.resolve_url(self.url, self.config)

    def test_routes_must_be_allowed_immutable_https_github_assets(self):
        for asset in [
            self.asset.replace("https:", "http:"),
            self.asset.replace("github.com", "github.com.evil.invalid"),
            self.asset.replace("github.com/", "github.com@evil.invalid/"),
            self.asset.replace("openresearchtools/", "different-owner/"),
            self.asset.replace("releases/download/v1.2.3", "releases/latest/download"),
            self.asset + "?redirect=elsewhere", self.asset + "#fragment",
            self.asset.replace("/v1.2.3/", "/../"), self.asset + " bad",
        ]:
            with self.subTest(asset=asset):
                self.database([(self.filename, [asset])])
                with self.assertRaises(fetch.FetchError):
                    fetch.resolve_url(self.url, self.config)

    def test_route_supports_slashes_within_upstream_release_tag(self):
        asset = self.asset.replace("/v1.2.3/", "/release/v1.2.3/")
        self.database([(self.filename, [asset])])
        self.assertEqual(fetch.resolve_url(self.url, self.config), asset)

    def test_direct_url_validation_rejects_control_characters(self):
        with self.assertRaises(fetch.FetchError):
            fetch.validate_download_url(self.asset + "\n", self.config)

    def test_flat_filename_cannot_route_a_path(self):
        self.database([(self.filename, [self.asset])])
        for filename in ["../" + self.filename, "folder/" + self.filename,
                         "folder%2F" + self.filename]:
            with self.subTest(filename=filename), self.assertRaises(fetch.FetchError):
                fetch.resolve_url(fetch.server_url(self.config) + "/" + filename, self.config)

    def test_curl_uses_argument_list_and_keeps_exact_output_path(self):
        output = str(self.root / "a file;$(echo unsafe).part")
        self.database([(self.filename, [self.asset])])
        def curl(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(self.payload)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(fetch.subprocess, "run", side_effect=curl) as run:
            result = fetch.main(["--config", str(self.config_path), self.url, output])
        self.assertEqual(result, 0)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[-2:], ["--", self.asset])
        self.assertEqual(arguments[arguments.index("--output") + 1], output)
        self.assertIn("--progress-bar", arguments)
        self.assertNotIn("--silent", arguments)
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_optional_database_signature_404_hides_only_the_expected_curl_message(self):
        signature = "https://mirror.omarchy.org/core/os/aarch64/core.db.sig"
        for status in ("404", "404\n", " 404 \n"):
            with self.subTest(status=status), \
                 patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], 22, stdout=status, stderr="curl: (22) The requested URL returned error: 404\n")) as run, \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr:
                result = fetch.download(signature, str(self.root / "optional-signature.part"), quiet_not_found=True)
            self.assertEqual(result, 22, "An optional 404 must remain a failed download for pacman")
            self.assertEqual(stderr.getvalue(), "")
            arguments = run.call_args.args[0]
            self.assertIn("--silent", arguments)
            self.assertIn("--show-error", arguments)
            self.assertEqual(arguments[arguments.index("--write-out") + 1], "%{http_code}")
            self.assertEqual(run.call_args.kwargs.get("capture_output"), True)
            self.assertEqual(run.call_args.kwargs.get("text"), True)
            self.assertEqual(arguments[-2:], ["--", signature])

    def test_optional_signature_other_failures_remain_visible_and_keep_exit_status(self):
        signature = "https://mirror.omarchy.org/core/os/aarch64/core.db.sig"
        for code, status, message in [
            (22, "403", "curl: (22) forbidden\n"),
            (22, "500", "curl: (22) server error\n"),
            (60, "000", "curl: (60) certificate verification failed\n"),
            (6, "000", "curl: (6) could not resolve host\n"),
            (28, "404", "curl: (28) operation timed out\n"),
            (22, "", "curl: (22) missing HTTP status\n"),
            (22, "404 403", "curl: (22) ambiguous HTTP status\n"),
            (22, "404oops", "curl: (22) malformed HTTP status\n"),
            (0, "200", "curl warning remains visible\n"),
        ]:
            with self.subTest(code=code, status=status), \
                 patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], code, stdout=status, stderr=message)), \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr:
                result = fetch.download(signature, str(self.root / "optional-signature.part"), quiet_not_found=True)
            self.assertEqual(result, code)
            self.assertEqual(stderr.getvalue(), message)

    def test_only_foreign_database_signature_paths_use_the_quiet_404_probe(self):
        prefix = fetch.server_url(self.config) + "/"
        for url in (
            "https://mirror.omarchy.org/core/os/aarch64/core.db.sig",
            "https://mirror.omarchy.org/core/os/aarch64/core.db.sig?cache=1",
        ):
            with self.subTest(url=url), patch.object(fetch, "download", return_value=22) as download:
                self.assertEqual(fetch.fetch_url(url, str(self.root / "signature.part"), self.config), 22)
                download.assert_called_once_with(url, str(self.root / "signature.part"), quiet_not_found=True)
        for url in (
            "https://mirror.omarchy.org/core/os/aarch64/core.db",
            "https://mirror.omarchy.org/core/os/aarch64/example.pkg.tar.zst",
            "https://mirror.omarchy.org/core/os/aarch64/example.pkg.tar.zst.sig",
            "https://mirror.omarchy.org/core/os/aarch64/core.db.sig.txt",
            "https://mirror.omarchy.org/core/os/aarch64/core.db.sig/extra",
            prefix + "unknown.db.sig",
        ):
            with self.subTest(url=url), patch.object(fetch, "download", return_value=22) as download:
                self.assertEqual(fetch.fetch_url(url, str(self.root / "ordinary.part"), self.config), 22)
                self.assertFalse(download.call_args.kwargs.get("quiet_not_found", False))

    def test_corrupt_partial_or_complete_download_retries_fresh_and_changes_only_its_output(self):
        self.database([(self.filename, [self.asset])])
        untouched = self.root / "other-package.part"
        untouched.write_bytes(b"unrelated cache entry")
        for existing in (b"broken", b"X" + self.payload[1:]):
            with self.subTest(existing=existing):
                output = self.root / "requested-package.part"
                output.write_bytes(existing)
                attempts = []

                def resumed_download(url, destination, maximum_size=None):
                    target = Path(destination)
                    attempts.append(target.exists())
                    self.assertEqual(url, self.asset)
                    if len(attempts) == 1:
                        # Reproduce curl --continue-at -: a valid suffix cannot
                        # repair corrupted bytes already stored in the prefix.
                        with target.open("ab") as stream:
                            stream.write(self.payload[target.stat().st_size:])
                    else:
                        self.assertFalse(target.exists(), "The bad prefix was reused for the fresh retry")
                        target.write_bytes(self.payload)
                    return 0

                with patch.object(fetch, "download", side_effect=resumed_download):
                    self.assertEqual(fetch.fetch_url(self.url, str(output), self.config), 0)
                self.assertEqual(attempts, [True, False])
                self.assertEqual(output.read_bytes(), self.payload)
                self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.sha256)
                self.assertEqual(untouched.read_bytes(), b"unrelated cache entry")

    def test_valid_partial_and_complete_download_keep_verified_resume_without_retry(self):
        self.database([(self.filename, [self.asset])])
        for existing in (self.payload[:7], self.payload):
            with self.subTest(existing=existing):
                output = self.root / "valid-package.part"
                output.write_bytes(existing)

                def resumed_download(url, destination, maximum_size=None):
                    target = Path(destination)
                    self.assertEqual(target.read_bytes(), existing)
                    with target.open("ab") as stream:
                        stream.write(self.payload[target.stat().st_size:])
                    return 0

                with patch.object(fetch, "download", side_effect=resumed_download) as download:
                    self.assertEqual(fetch.fetch_url(self.url, str(output), self.config), 0)
                self.assertEqual(download.call_count, 1)
                self.assertEqual(output.read_bytes(), self.payload)
                self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.sha256)

    def test_tampered_bytes_or_wrong_size_fail_after_one_fresh_retry_and_remove_bad_output(self):
        self.database([(self.filename, [self.asset])])
        for bad_payload in (b"X" + self.payload[1:], self.payload + b"extra bytes"):
            with self.subTest(bad_payload=bad_payload):
                output = self.root / "tampered-package.part"

                def tampered_download(url, destination, maximum_size=None):
                    Path(destination).write_bytes(bad_payload)
                    return 0

                with patch.object(fetch, "download", side_effect=tampered_download) as download, \
                     patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    result = fetch.main(["--config", str(self.config_path), self.url, str(output)])
                self.assertNotEqual(result, 0)
                self.assertEqual(download.call_count, 2)
                self.assertIn("checksum", stderr.getvalue().lower())
                self.assertFalse(output.exists(), "A rejected partial must not poison a later invocation")

    def test_missing_or_malformed_signed_integrity_metadata_stops_before_network(self):
        valid_size = [str(len(self.payload))]
        for integrity in [
            {}, {"SHA256SUM": [self.sha256]}, {"CSIZE": valid_size},
            {"SHA256SUM": ["not-a-sha256"], "CSIZE": valid_size},
            {"SHA256SUM": [self.sha256, self.sha256], "CSIZE": valid_size},
            {"SHA256SUM": [self.sha256], "CSIZE": ["0"]},
            {"SHA256SUM": [self.sha256], "CSIZE": ["-1"]},
            {"SHA256SUM": [self.sha256], "CSIZE": ["21oops"]},
            {"SHA256SUM": [self.sha256], "CSIZE": valid_size * 2},
        ]:
            with self.subTest(integrity=integrity):
                self.database([(self.filename, [self.asset])], integrity=integrity)
                with patch.object(fetch, "download") as download, \
                     patch("sys.stderr", new_callable=io.StringIO):
                    result = fetch.main(["--config", str(self.config_path), self.url,
                                         str(self.root / "never-downloaded.part")])
                self.assertNotEqual(result, 0)
                download.assert_not_called()

    def test_non_catalogue_packages_and_detached_signatures_keep_the_existing_download_path(self):
        self.database([(self.filename, [self.asset])])
        foreign = "https://mirror.omarchy.org/core/os/aarch64/example.pkg.tar.zst"
        for url, target in ((foreign, foreign), (self.url + ".sig", self.asset + ".sig")):
            with self.subTest(url=url), patch.object(fetch, "download", return_value=0) as download:
                self.assertEqual(fetch.fetch_url(url, str(self.root / "output.part"), self.config), 0)
                self.assertEqual(download.call_args.args[:2], (target, str(self.root / "output.part")))

    def test_download_failure_and_missing_route_propagate(self):
        self.database([(self.filename, [self.asset])])
        with patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess([], 22)):
            self.assertEqual(fetch.main(["--config", str(self.config_path), self.url,
                                         str(self.root / "output.part")]), 22)
        self.database([])
        with patch.object(fetch.subprocess, "run") as run, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(fetch.main(["--config", str(self.config_path), self.url,
                                         str(self.root / "output.part")]), 1)
        run.assert_not_called()

    def bundle(self, entries):
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w:gz") as archive:
            for name, data, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return result.getvalue()

    def test_database_and_signature_come_from_one_downloaded_bundle(self):
        bundle = self.bundle([
            ("openresearchtools.db", b"database generation one", tarfile.REGTYPE),
            ("openresearchtools.db.sig", b"signature generation one", tarfile.REGTYPE),
        ])
        calls = []

        def download_bundle(url, output, maximum_size=None):
            calls.append(url)
            Path(output).write_bytes(bundle)
            return 0

        prefix = fetch.server_url(self.config) + "/"
        database = self.root / "openresearchtools.db.part"
        signature = self.root / "openresearchtools.db.sig.part"
        with patch.object(fetch, "download", side_effect=download_bundle):
            self.assertEqual(fetch.fetch_url(prefix + "openresearchtools.db", str(database), self.config), 0)
            # No second network request can pick a signature from another generation.
            self.assertEqual(fetch.fetch_url(prefix + "openresearchtools.db.sig", str(signature), self.config), 0)
        self.assertEqual(calls, [prefix + fetch.BUNDLE_NAME])
        self.assertEqual(database.read_bytes(), b"database generation one")
        self.assertEqual(signature.read_bytes(), b"signature generation one")
        self.assertFalse(fetch.signature_stash(database, self.config).exists())

    def test_failed_database_refresh_discards_old_staged_signature(self):
        database = self.root / "openresearchtools.db.part"
        fetch.signature_stash(database, self.config).write_bytes(b"old signature")
        with patch.object(fetch, "download", return_value=22):
            self.assertEqual(fetch.fetch_url(fetch.server_url(self.config) + "/openresearchtools.db", str(database), self.config), 22)
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_url(fetch.server_url(self.config) + "/openresearchtools.db.sig", str(self.root / "signature.part"), self.config)

    def test_signature_stash_is_separate_per_channel(self):
        output = self.root / "database.part"
        stable = fetch.signature_stash(output, self.config)
        nightly = dict(self.config, channel="nightly")
        self.assertNotEqual(stable, fetch.signature_stash(output, nightly))

    def test_bundle_never_extracts_paths_links_or_extra_files(self):
        base = [
            ("openresearchtools.db", b"database", tarfile.REGTYPE),
            ("openresearchtools.db.sig", b"signature", tarfile.REGTYPE),
        ]
        for entries in [
            base[:1], base + [base[0]],
            [("../escape", b"bad", tarfile.REGTYPE)] + base,
            [("openresearchtools.db", b"", tarfile.SYMTYPE), base[1]],
        ]:
            with self.subTest(entries=entries):
                path = self.root / "bundle.tar.gz"
                path.write_bytes(self.bundle(entries))
                with self.assertRaises(fetch.FetchError):
                    fetch.read_catalogue_bundle(path)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_bundle_uncompressed_size_is_bounded(self):
        data = self.bundle([
            ("openresearchtools.db", b"a" * 2000, tarfile.REGTYPE),
            ("openresearchtools.db.sig", b"signature", tarfile.REGTYPE),
        ])
        path = self.root / "bundle.tar.gz"
        path.write_bytes(data)
        with patch.object(fetch, "MAX_BUNDLE_BYTES", 1000), self.assertRaises(fetch.FetchError):
            fetch.read_catalogue_bundle(path)


class SetupTests(unittest.TestCase):
    CONF = "# My configuration\n[options]\nColor\n\n[core]\nInclude = /missing/mirrorlist\n"

    def test_render_is_idempotent_and_keeps_user_settings(self):
        rendered = setup.render_pacman_conf(self.CONF)
        self.assertEqual(setup.render_pacman_conf(rendered), rendered)
        self.assertIn("# My configuration\n", rendered)
        self.assertIn("Color\n", rendered)
        self.assertIn("[core]\nInclude = /missing/mirrorlist\n", rendered)
        self.assertLess(rendered.index(str(setup.OPTIONS_INCLUDE)), rendered.index("[core]"))
        self.assertEqual(rendered.count(str(setup.OPTIONS_INCLUDE)), 1)
        self.assertEqual(rendered.count(str(setup.REPOSITORY_INCLUDE)), 1)

    def test_switch_removes_old_inline_repo_without_duplicate_sections(self):
        original = self.CONF + "[openresearchtools]\nServer = https://old.invalid\n[extra]\nServer = https://extra.invalid\n"
        rendered = setup.render_pacman_conf(original)
        self.assertNotIn("[openresearchtools]", rendered)
        self.assertNotIn("old.invalid", rendered)
        self.assertIn("[extra]\nServer = https://extra.invalid", rendered)

    def test_preserves_existing_downloader_unless_explicitly_replaced(self):
        original = self.CONF.replace("Color", "Color\nXferCommand = /usr/bin/wget %u")
        with self.assertRaises(setup.SetupError):
            setup.check_existing_configuration(original, Path("/etc/pacman.conf"))
        setup.check_existing_configuration(original, Path("/etc/pacman.conf"), True)
        self.assertIn("XferCommand = /usr/bin/wget %u", setup.render_pacman_conf(original))

    def test_checks_downloader_inside_include_and_rejects_recursive_include(self):
        with tempfile.TemporaryDirectory() as temporary:
            include = Path(temporary) / "options.conf"
            include.write_text("XferCommand = /custom/downloader %u\n")
            original = self.CONF.replace("Color", f"Color\nInclude = {include}")
            with self.assertRaises(setup.SetupError):
                setup.check_existing_configuration(original, Path(temporary) / "pacman.conf")
            include.write_text(f"Include = {include}\n")
            with self.assertRaises(setup.SetupError):
                setup.check_existing_configuration(original, Path(temporary) / "pacman.conf", True)

    def test_rejects_external_duplicate_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            include = Path(temporary) / "external.conf"
            include.write_text("[openresearchtools]\nServer = https://old.invalid\n")
            with self.assertRaises(setup.SetupError):
                setup.check_existing_configuration(self.CONF + f"Include = {include}\n", Path(temporary) / "pacman.conf")

    def test_dry_run_does_not_write_or_import_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pacman_conf = root / "pacman.conf"
            pacman_conf.write_text(self.CONF)
            settings = root / "settings.json"
            with patch.object(setup, "PACMAN_CONF", pacman_conf), \
                 patch.object(setup, "CONFIG_PATH", settings), \
                 patch.object(setup, "DEFAULTS_PATH", root / "defaults.json"), \
                 patch.object(setup, "atomic_write") as write, \
                 patch.object(setup, "verify_public_key") as verify, \
                 patch.object(setup, "invoking_account", return_value=None), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(setup.main(["--channel", "nightly", "--architecture", "aarch64"]), 0)
            self.assertFalse(settings.exists())
            self.assertEqual(pacman_conf.read_text(), self.CONF)
            write.assert_not_called()
            verify.assert_not_called()

    def test_atomic_update_preserves_original_backup_and_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pacman.conf"
            path.write_text("original\n")
            setup.atomic_write(path, "managed\n")
            setup.atomic_write(path, "changed\n")
            self.assertEqual(path.with_name(path.name + ".openresearchtools.bak").read_text(), "original\n")
            modified = path.stat().st_mtime_ns
            setup.atomic_write(path, "changed\n")
            self.assertEqual(path.stat().st_mtime_ns, modified)


if __name__ == "__main__":
    unittest.main()
