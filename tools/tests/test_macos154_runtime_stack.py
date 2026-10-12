"""Independent 225-patch source proof; not compiled browser/runtime acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from tools import apply_restored_patches as arp
from tools import patch_selection as selection
from tools.tests.test_macos154_patch_selection import (
    BUILD, CORE, PLATFORM, PREIMAGES, VERSION, predecessor_sections,
)
from tools.tests.test_patch_selection import clone_repo, set_macos_pins

ROOT = Path(__file__).resolve().parents[2]
ADDITION_ROOT = "patches/chromium154-macos"
MANIFEST = "build/patch-additions-macos154.json"
ADDITION = ADDITION_ROOT + "/0226-headless-command-explicit-quit.patch"
HEADLESS = json.loads((Path(__file__).with_name("fixtures") /
                       "macos154_headless_quit.json").read_text())
TARGET = HEADLESS["target"]
SOURCE_BLOB = "c7b0a43fd05c881c7556fb35a64b2954746317cc"


@pytest.fixture
def runtime_repo(tmp_path):
    repo = clone_repo(tmp_path, additions=False)
    shutil.copytree(ROOT / ADDITION_ROOT, repo / ADDITION_ROOT)
    shutil.copy2(ROOT / MANIFEST, repo / MANIFEST)
    set_macos_pins(repo)
    return repo


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): {
            "sha256": selection.digest(path.read_bytes()),
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def contents(state):
    return {name: item["sha256"] for name, item in state.items()}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def selected_runtime(repo, **kwargs):
    identity, patches = selection.select(repo, "macos", **kwargs)
    assert len(patches) == 225
    assert patches[-1][0] == ADDITION
    additions = identity["selection"]["additions"]
    assert additions["manifest"]["path"] == MANIFEST
    assert additions["patches"] == [
        {"path": ADDITION, "sha256": selection.digest(patches[-1][1])}]
    assert identity["selection"]["version"] == VERSION
    assert identity["selection"]["core_commit"] == CORE
    assert identity["selection"]["platform_commit"] == PLATFORM
    return identity, patches


def test_runtime_selection_is_macos_only(runtime_repo):
    _, patches = selected_runtime(runtime_repo)
    for platform in ("linux", "windows"):
        identity, other = selection.select(runtime_repo, platform)
        assert other == patches[:-1]
        assert "additions" not in identity["selection"]
    assert not (runtime_repo / selection.LINUX155_ADDITIONS_MANIFEST).exists()
    assert not (runtime_repo / selection.LINUX155_ADDITIONS_ROOT).exists()


@pytest.mark.parametrize("substituted", [False, True], ids=["raw", "domain"])
def test_independent_225_stack(tmp_path, runtime_repo, substituted):
    donor_root = os.environ.get("CHROMIX_MACOS154_SOURCES")
    headless_root = os.environ.get("CHROMIX_HEADLESS154_SOURCE")
    if not donor_root or not headless_root:
        pytest.skip("set CHROMIX_MACOS154_SOURCES and CHROMIX_HEADLESS154_SOURCE "
                    "to independent complete source inputs")
    donor = Path(donor_root)
    pristine, core, mac = (donor / name for name in ("pristine", "core", "mac"))
    headless_source = Path(headless_root) / TARGET
    assert selection.source_version(pristine) == VERSION == HEADLESS["chromium_version"]
    assert (core / "chromium_version.txt").read_text().strip() == VERSION
    assert HEADLESS["source_url"] == (
        "https://chromium.googlesource.com/chromium/src/+/" + VERSION + "/" + TARGET)
    raw = headless_source.read_bytes()
    assert selection.digest(raw) == HEADLESS["source_sha256"]
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert blob == SOURCE_BLOB
    source_lines = raw.decode().splitlines(keepends=True)
    for first, excerpt in HEADLESS["sections"]:
        assert "".join(source_lines[first - 1:first - 1 + len(excerpt.splitlines())]) == excerpt
    for name, expected in PREIMAGES.items():
        assert selection.digest((pristine / name).read_bytes()) == expected

    immutable_roots = {"pristine": pristine, "core": core, "mac": mac,
                       "headless": Path(headless_root), "repo": runtime_repo}
    before = {name: snapshot(path) for name, path in immutable_roots.items()}
    write_json(tmp_path / "inputs-before.json", before)
    log_path = tmp_path / "apply.log"
    program = shutil.which("gpatch") or shutil.which("patch")
    if not program:
        pytest.skip("GNU patch is required")
    src = tmp_path / "src"

    def apply(name, data, reverse=False):
        patch = tmp_path / "current.patch"
        patch.write_bytes(data)
        options = ["--reverse"] if reverse else []
        result = subprocess.run(
            [program, *arp.PATCH_OPTIONS, *options, "--input", str(patch)],
            cwd=src, capture_output=True, text=True, timeout=30,
            env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})
        output = result.stdout + result.stderr
        with log_path.open("a") as stream:
            stream.write(("reverse " if reverse else "forward ") + name + "\n" + output)
        assert result.returncode == 0, name + "\n" + output
        assert not re.search(r"fuzz|FAILED", output, re.I), name + "\n" + output
        if name == ADDITION:
            assert not re.search(r"offset", output, re.I), output

    try:
        shutil.copytree(pristine, src)
        target = src / TARGET
        if target.exists():
            assert target.read_bytes() == raw
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        identity, selected = selected_runtime(runtime_repo, src=src, core=core)
        targets = {name for _, patch in selected
                   for name, _, _ in arp.transform_patch(patch, set(), [])[1]}
        assert len(targets) == 168
        predecessors = list(predecessor_sections(core, targets))
        mac_sections = list(predecessor_sections(mac, targets))
        assert len(predecessors) == 21
        assert [(name, name_target) for name, name_target, _ in mac_sections] == [
            ("ungoogled-chromium/macos/fix-build-without-crubit.patch", BUILD)]
        assert not any(name_target == TARGET for _, name_target, _ in
                       [*predecessors, *mac_sections])
        for name, _, section in [*predecessors, *mac_sections]:
            apply(name, section.encode())

        if substituted:
            rules = arp._rules((core / "domain_regex.list").read_bytes())
            listed = set((core / "domain_substitution.list").read_text().splitlines())
            for name in targets & listed:
                path = src / name
                if path.is_file():
                    text, encoding = arp._decode(path.read_bytes())
                    path.write_bytes(arp._substitute(text, rules).encode(encoding))
            _, patches, lite = arp._load(runtime_repo, core, mac, "macos", src=src)
            for name, (data, mode) in lite.items():
                path = src / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(mode)
            (src / ".chromix-domain-substituted").write_text(CORE)
        else:
            shutil.copytree(runtime_repo / arp.LITE, src, dirs_exist_ok=True)
            patches = [(name, *arp.transform_patch(data, set(), []))
                       for name, data in selected]
        assert len(patches) == 225
        assert [name for name, _, _ in patches] == [name for name, _ in selected]
        base = contents(snapshot(src))
        write_json(tmp_path / "source-before-225.json", base)
        for name, data, _ in patches[:-1]:
            apply(name, data)
        prebefore = snapshot(src)
        write_json(tmp_path / "source-before-addition.json", prebefore)
        apply(patches[-1][0], patches[-1][1])
        final = snapshot(src)
        changed = [name for name in prebefore.keys() | final.keys()
                   if prebefore.get(name) != final.get(name)]
        assert changed == [TARGET]
        callback = target.read_text().split("headless::ProcessHeadlessCommands(", 1)[1]
        callback = callback.split("      continue;", 1)[0]
        assert "chrome::CloseAllBrowsersAndQuit();" in callback
        assert "app_controller_mac::AllowApplicationToTerminate();" in callback
        assert "std::unique_ptr<ScopedProfileKeepAlive> profile_keepalive" in callback
        assert "browser->GetWindow()" not in callback and "WeakPtr" not in callback
        assert "exit(" not in callback and "QuitClosure" not in callback

        for name, data, _ in reversed(patches):
            apply(name, data, reverse=True)
        assert contents(snapshot(src)) == base
        for name, data, _ in patches:
            apply(name, data)
        assert contents(snapshot(src)) == contents(final)
        verification_before = snapshot(src)
        output = tmp_path / "verified.json"
        result = subprocess.run([
            sys.executable, str(ROOT / "tools/verify_patch_stack.py"),
            "--src", str(src), "--repo", str(runtime_repo), "--core", str(core),
            "--platform-tooling", str(mac), "--platform", "macos", "--output", str(output)],
            capture_output=True, text=True, timeout=120)
        (tmp_path / "verify.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(output.read_text())
        assert report["status"] == "verified" and report["patch_count"] == 225
        assert report["domain_substituted"] == substituted
        assert report["identity"]["selection"] == identity["selection"]
        assert len(report["outputs"]) == 168
        assert snapshot(src) == verification_before
        write_json(tmp_path / "source-after-225.json", contents(snapshot(src)))
        write_json(tmp_path / "proof.json", {
            "status": "passed", "scope": "source-only; no browser runtime acceptance",
            "chromium_version": VERSION, "patch_count": 225, "target_count": 168,
            "core_predecessor_sections": len(predecessors),
            "mac_predecessor_sections": len(mac_sections),
            "domain_substituted": substituted, "selection": identity,
            "startup_source": {"url": HEADLESS["source_url"],
                               "sha256": HEADLESS["source_sha256"], "git_blob": blob},
            "addition_changed_files": changed,
            "forward_reverse_forward": "passed", "verifier_source_immutable": True,
            "verification_report": str(output), "apply_log": str(log_path),
        })
    finally:
        after = {name: snapshot(path) for name, path in immutable_roots.items()}
        write_json(tmp_path / "inputs-after.json", after)
        assert after == before
