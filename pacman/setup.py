#!/usr/bin/env python3
"""Preview or explicitly apply Open Research Tools pacman configuration.

No system changes are made unless --apply is supplied. Installing the package
alone does not replace the user's downloader or enable a repository.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _load_fetch_module():
    # Development checkout and installed package use different file names.
    for candidate in (
        Path(__file__).with_name("fetch.py"),
        Path("/usr/lib/openresearchtools-pacman/fetch.py"),
        Path("/usr/bin/openresearchtools-pacman-fetch"),
    ):
        if candidate.is_file():
            from importlib.machinery import SourceFileLoader
            loader = SourceFileLoader("ort_pacman_fetch", str(candidate))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    raise RuntimeError("The Open Research Tools pacman downloader is not installed")


fetch = _load_fetch_module()
FINGERPRINT = "94A2D5BDBD2A22C1B6F0914DC7B3EFA0BA41EB0A"
PACMAN_CONF = Path("/etc/pacman.conf")
OPTIONS_INCLUDE = Path("/etc/pacman.d/openresearchtools-options.conf")
REPOSITORY_INCLUDE = Path("/etc/pacman.d/openresearchtools.conf")
CONFIG_PATH = fetch.CONFIG_PATH
DEFAULTS_PATH = Path("/usr/share/openresearchtools-pacman/defaults.json")
KEY_PATH = Path("/usr/share/pacman/keyrings/openresearchtools.gpg")
FETCH_COMMAND = "/usr/bin/openresearchtools-pacman-fetch %u %o"
SETUP_COMMAND = "/usr/bin/openresearchtools-pacman-setup"
SECTION = re.compile(r"^\s*\[([^]\n]+)\]\s*(?:#.*)?$")
DIRECTIVE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*?)\s*$")


class SetupError(ValueError):
    pass


def configuration_sources(text: str, path: Path, seen: set[Path] | None = None):
    """Read active directives, including nested Include files, without edits."""
    seen = set() if seen is None else seen
    resolved = path.resolve()
    if resolved in seen:
        raise SetupError(f"Recursive or repeated pacman Include: {path}")
    seen.add(resolved)
    for line in text.splitlines():
        match = SECTION.fullmatch(line)
        if match:
            yield path, "section", match.group(1)
            continue
        match = DIRECTIVE.fullmatch(line)
        if not match:
            continue
        key, value = match.groups()
        yield path, key, value
        if key == "Include" and value != str(REPOSITORY_INCLUDE):
            for name in sorted(glob.glob(value)):
                included = Path(name)
                try:
                    included_text = included.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise SetupError(f"Cannot read pacman Include {included}: {exc}") from exc
                yield from configuration_sources(included_text, included, seen)
    seen.remove(resolved)


def check_existing_configuration(text: str, path: Path, replace_downloader: bool = False):
    for source, key, value in configuration_sources(text, path):
        if key == "section" and value == "openresearchtools" and source != path:
            raise SetupError(
                f"An existing [openresearchtools] section lives in {source}; "
                "remove that manual definition before enabling the managed repository"
            )
        if key != "XferCommand":
            continue
        try:
            command = shlex.split(value)
        except ValueError as exc:
            raise SetupError(f"Cannot parse existing XferCommand in {source}") from exc
        ours = command and command[0] == "/usr/bin/openresearchtools-pacman-fetch"
        if not ours and not replace_downloader:
            raise SetupError(
                f"Existing XferCommand in {source}: {value}. "
                "It is preserved; use --replace-downloader to explicitly override it"
            )


def render_pacman_conf(text: str) -> str:
    """Keep user content, replacing only our section and managed Include lines."""
    lines = []
    skipping_our_section = False
    for line in text.splitlines(keepends=True):
        section = SECTION.fullmatch(line.rstrip("\r\n"))
        if section:
            skipping_our_section = section.group(1) == "openresearchtools"
        if skipping_our_section:
            continue
        directive = DIRECTIVE.fullmatch(line.rstrip("\r\n"))
        if directive and directive.group(1) == "Include" and directive.group(2) in {
            str(OPTIONS_INCLUDE), str(REPOSITORY_INCLUDE)
        }:
            continue
        lines.append(line)
    options = [i for i, line in enumerate(lines) if SECTION.fullmatch(line.rstrip("\r\n"))
               and SECTION.fullmatch(line.rstrip("\r\n")).group(1) == "options"]
    if len(options) > 1:
        raise SetupError("Multiple [options] sections in pacman.conf")
    if not options:
        lines[0:0] = ["[options]\n"]
        options = [0]
    end = next(
        (i for i in range(options[0] + 1, len(lines))
         if SECTION.fullmatch(lines[i].rstrip("\r\n"))), len(lines)
    )
    if end and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines.insert(end, f"Include = {OPTIONS_INCLUDE}\n")
    rendered = "".join(lines)
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered + f"Include = {REPOSITORY_INCLUDE}\n"


def repository_config(config: dict) -> str:
    return (
        "# Managed by openresearchtools-pacman-setup.\n"
        "[openresearchtools]\n"
        "SigLevel = PackageNever DatabaseRequired TrustedOnly\n"
        f"Server = {fetch.server_url(config)}\n"
    )


def hook_content() -> str:
    return (
        "#!/bin/sh\n"
        "# Restore the vendor repository after Omarchy refreshes pacman.conf.\n"
        f"exec sudo {SETUP_COMMAND} --apply --restore\n"
    )


def invoking_account(username: str | None = None):
    username = username or os.environ.get("SUDO_USER")
    if username:
        account = pwd.getpwnam(username)
    else:
        account = pwd.getpwuid(os.getuid())
    return account if account.pw_uid != 0 else None


def verify_public_key(path: Path = KEY_PATH):
    if not path.is_file():
        raise SetupError(f"Packaged public key is missing: {path}")
    with tempfile.TemporaryDirectory(prefix="ort-pacman-key-") as temporary:
        result = subprocess.run(
            ["/usr/bin/gpg", "--batch", "--no-options", "--homedir", temporary,
             "--with-colons", "--show-keys", str(path)],
            text=True, capture_output=True, check=True,
        )
    fingerprints = []
    primary = False
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            primary = True
        elif fields[0] == "sub":
            primary = False
        elif fields[0] == "fpr" and primary:
            fingerprints.append(fields[9])
            primary = False
    if fingerprints != [FINGERPRINT]:
        raise SetupError("The packaged public key does not match the archive fingerprint")


def atomic_write(path: Path, content: str, mode: int = 0o644):
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".openresearchtools.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_user_hook(account):
    # Drop privileges before touching a user-owned directory or following any
    # links within it. The helper process needs no shell or system privileges.
    destination = Path(account.pw_dir) / ".config/omarchy/hooks/pre-refresh-pacman.d/openresearchtools-pacman"
    writer = (
        "import os,pathlib,sys,tempfile; p=pathlib.Path(sys.argv[1]); data=sys.stdin.read(); "
        "p.parent.mkdir(parents=True,exist_ok=True); "
        "fd,tmp=tempfile.mkstemp(prefix='.openresearchtools-',dir=p.parent); "
        "f=os.fdopen(fd,'w'); f.write(data); f.close(); "
        "os.chmod(tmp,0o755); os.replace(tmp,p)"
    )
    subprocess.run(
        [sys.executable, "-I", "-c", writer, str(destination)], input=hook_content(),
        text=True, check=True, user=account.pw_uid, group=account.pw_gid, extra_groups=[],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the previewed system changes (requires sudo)")
    parser.add_argument("--restore", action="store_true", help="Restore existing settings after an Omarchy refresh")
    parser.add_argument("--channel", choices=("stable", "nightly", "testing"))
    parser.add_argument("--architecture", choices=("x86_64", "aarch64"))
    parser.add_argument("--catalogue-repository", help="GitHub owner/repository containing the catalogue")
    parser.add_argument("--replace-downloader", action="store_true", help="Explicitly override an existing global XferCommand")
    parser.add_argument("--user", help="Omarchy account to receive the refresh hook (defaults to the sudo caller)")
    args = parser.parse_args(argv)
    try:
        if args.restore and not CONFIG_PATH.is_file():
            raise SetupError("Cannot restore before the repository has been configured")
        base = CONFIG_PATH if CONFIG_PATH.is_file() else DEFAULTS_PATH
        config = json.loads(base.read_text(encoding="utf-8")) if base.is_file() else {}
        if not isinstance(config, dict):
            raise SetupError("The downloader configuration must be a JSON object")
        if args.channel:
            config["channel"] = args.channel
        if args.architecture:
            config["architecture"] = args.architecture
        if args.catalogue_repository:
            config["catalogue_repository"] = args.catalogue_repository
        config = fetch.validate_configuration(config)
        config["replace_downloader"] = bool(args.replace_downloader or config.get("replace_downloader", False))
        original = PACMAN_CONF.read_text(encoding="utf-8")
        check_existing_configuration(original, PACMAN_CONF, config["replace_downloader"])
        files = {
            CONFIG_PATH: json.dumps(config, indent=2, sort_keys=True) + "\n",
            OPTIONS_INCLUDE: "# Managed by openresearchtools-pacman-setup.\nXferCommand = " + FETCH_COMMAND + "\n",
            REPOSITORY_INCLUDE: repository_config(config),
            PACMAN_CONF: render_pacman_conf(original),
        }
        account = invoking_account(args.user)
        use_hook = account is not None and Path("/usr/share/omarchy").is_dir() and not args.restore
        if not args.apply:
            print("Preview only. Run with sudo and --apply to enable these settings.")
            print("This replaces pacman's downloader globally; downloads use curl sequentially.")
            for path, content in files.items():
                print(f"\n--- {path}\n{content}", end="")
            if not args.restore:
                print(f"\nImport and locally trust public key {FINGERPRINT} from {KEY_PATH}.")
            if use_hook:
                print(f"Install the Omarchy pre-refresh-pacman hook for {account.pw_name}.")
            return 0
        if os.geteuid() != 0:
            raise SetupError("--apply requires root; run this command using sudo")
        if not args.restore:
            verify_public_key()
            subprocess.run(["/usr/bin/pacman-key", "--add", str(KEY_PATH)], check=True)
            subprocess.run(["/usr/bin/pacman-key", "--lsign-key", FINGERPRINT], check=True)
        for path, content in files.items():
            atomic_write(path, content)
        if use_hook:
            install_user_hook(account)
        print(f"Enabled Open Research Tools {config['channel']} for {config['architecture']}.")
        print("Refresh packages with omarchy update on Omarchy, or pacman -Syu on Arch.")
        return 0
    except (SetupError, fetch.FetchError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"openresearchtools-pacman-setup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
