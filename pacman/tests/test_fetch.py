import importlib.util
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

    def database(self, entries):
        with tarfile.open(self.db, "w:gz") as archive:
            for index, (filename, routes) in enumerate(entries):
                text = f"%FILENAME%\n{filename}\n\n%DATA%\nother=value\n"
                text += "\n".join("ort_download_url=" + route for route in routes) + "\n\n"
                data = text.encode()
                member = tarfile.TarInfo(f"app-{index}/desc")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_uses_current_signed_database_route(self):
        self.database([(self.filename, [self.asset])])
        self.assertEqual(fetch.resolve_url(self.url, self.config), self.asset)

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
        with patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = fetch.main(["--config", str(self.config_path), self.url, output])
        self.assertEqual(result, 0)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[-2:], ["--", self.asset])
        self.assertEqual(arguments[arguments.index("--output") + 1], output)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_download_failure_and_missing_route_propagate(self):
        self.database([(self.filename, [self.asset])])
        with patch.object(fetch.subprocess, "run", return_value=subprocess.CompletedProcess([], 22)):
            self.assertEqual(fetch.main(["--config", str(self.config_path), self.url, "/tmp/output.part"]), 22)
        self.database([])
        with patch.object(fetch.subprocess, "run") as run, patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(fetch.main(["--config", str(self.config_path), self.url, "/tmp/output.part"]), 1)
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
