"""Pinned Linux155 GN repair; isolated factory tests are not Chromium link tests."""
from __future__ import annotations

import gzip
import hashlib
import itertools
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
from tools.tests.test_chromium155_patch_selection import MANIFEST, authenticate_donor
from tools.tests.test_macos152_patch_selection import run_patch
from tools.tests.test_macos154_patch_selection import predecessor_sections
from tools.tests.test_patch_selection import clone_repo, set_platform_pins

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tools/tests/fixtures/linux155_managed_profile_link.json.gz"
FIXTURE_SHA256 = "4d61302ef75a32fd759bd2b7d4b6d28c35f7c3c5702b8db29277ace8ee1b7e92"
PATCH = selection.LINUX155_ADDITIONS_ROOT + "/0225-managed-profile-controller-link.patch"
BUILD = "chrome/browser/enterprise/BUILD.gn"
CONTROLLER = "chrome/browser/enterprise/signin/managed_profile_creation_controller"
CONSUMER = "chrome/browser/enterprise/signin/profile_management_disclaimer_service.cc"
UTILS = "chrome/browser/ui/webui/signin/signin_utils_desktop.cc"
SOURCE_LINE = '      "signin/managed_profile_creation_controller.cc",\n'
FACTORY = "CreateManagedProfileForTesting"


@pytest.fixture(scope="module")
def source_data():
    raw = FIXTURE.read_bytes()
    assert selection.digest(raw) == FIXTURE_SHA256
    data = json.loads(gzip.decompress(raw))
    assert data["chromium_commit"] == "3ff7ac5a9224be9156d7f8703a06e22890aafd34"
    assert data["chromium_version"] == selection.VERSION155
    assert data["core_commit"] == selection.CORE155
    assert data["platform_commit"] == selection.PLATFORM155_COMMITS["linux"]
    for entry in data["sources"].values():
        raw = entry["text"].encode()
        assert selection.digest(raw) == entry["sha256"]
        assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == entry["git_blob"]
    for entry in data["tooling"].values():
        if "text" in entry:
            assert selection.digest(entry["text"].encode()) == entry["sha256"]
    return data


def install_additions(repo):
    shutil.copy2(ROOT / selection.LINUX155_ADDITIONS_MANIFEST,
                 repo / selection.LINUX155_ADDITIONS_MANIFEST)
    shutil.copytree(ROOT / selection.LINUX155_ADDITIONS_ROOT,
                    repo / selection.LINUX155_ADDITIONS_ROOT)


@pytest.fixture
def repo(tmp_path):
    repo = clone_repo(tmp_path, additions=False)
    set_platform_pins(repo, "linux", selection.VERSION155, selection.CORE155,
                      selection.PLATFORM155_COMMITS["linux"])
    install_additions(repo)
    return repo


def test_current_checkout_requires_exact_single_addition():
    manifest = selection.read(ROOT, selection.LINUX155_ADDITIONS_MANIFEST)
    assert selection.digest(manifest) == selection.LINUX155_ADDITIONS_SHA256
    entries = json.loads(manifest)
    assert entries["chromium_version"] == selection.VERSION155
    assert entries["core_commit"] == selection.CORE155
    assert entries["platform_commit"] == selection.PLATFORM155_COMMITS["linux"]
    assert entries["patches"] == [{"path": PATCH, "sha256": selection.digest((ROOT / PATCH).read_bytes())}]
    assert {p.name for p in (ROOT / selection.LINUX155_ADDITIONS_ROOT).iterdir()} == {Path(PATCH).name}
    assert not list((ROOT / "patches").glob("0225-*.patch"))
    assert len(selection.select(ROOT, "linux")[1]) == 225
    assert PATCH.encode() not in (ROOT / "patches/series").read_bytes()


def test_historical_selection_and_key_are_preserved_before_typed_addition(repo):
    current, patches = selection.select(repo, "linux")
    keys = {style: selection.preparation_key(repo, "linux", style) for style in ("posix", "windows")}
    (repo / selection.LINUX155_ADDITIONS_MANIFEST).unlink()
    shutil.rmtree(repo / selection.LINUX155_ADDITIONS_ROOT)
    historical, old = selection.select(repo, "linux")
    assert len(old) == 224 and len(patches) == 225
    assert patches == [*old, (PATCH, (ROOT / PATCH).read_bytes())]
    assert current["selection"]["base_series_sha256"] == historical["selection"]["base_series_sha256"]
    assert {k: v for k, v in current["selection"].items() if k != "additions"} == historical["selection"]
    assert "additions" not in historical["selection"]
    series = (repo / "patches/series").read_bytes()
    for name, _ in selection.OVERRIDES155.items():
        series = series.replace(("patches/" + name).encode(), (selection.OVERRIDE155_ROOT + "/" + name).encode())
    assert historical["series_sha256"] == selection.digest(series)
    assert current["series_sha256"] == selection.digest(series + (PATCH + "\n").encode())
    assert current["patches"] == historical["patches"] + [json.loads(
        (ROOT / selection.LINUX155_ADDITIONS_MANIFEST).read_text())["patches"][0]]
    additions = current["selection"]["additions"]
    assert additions["kind"] == "linux155-managed-profile-controller-link"
    assert additions["manifest"] == {"path": selection.LINUX155_ADDITIONS_MANIFEST,
                                     "sha256": selection.LINUX155_ADDITIONS_SHA256}
    for style, key in keys.items():
        old_key = selection.preparation_key(repo, "linux", style)
        assert old_key != key
        install_additions(repo)
        assert selection.preparation_key(repo, "linux", style) == key
        (repo / selection.LINUX155_ADDITIONS_MANIFEST).unlink()
        shutil.rmtree(repo / selection.LINUX155_ADDITIONS_ROOT)
        assert selection.preparation_key(repo, "linux", style) == old_key


def test_apply_receipt_carries_addition_identity(repo, source_data, tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "chromium_version.txt").write_text(selection.VERSION155 + "\n")
    for name in ("domain_regex.list", "domain_substitution.list"):
        (core / name).write_text(source_data["tooling"]["core/" + name]["text"])
    identity, patches, _ = arp._load(repo, core, tmp_path / "linux", "linux")
    selected, _ = selection.select(repo, "linux", core=core)
    assert identity["selection"] == selected["selection"]
    assert identity["series"]["sha256"] == selected["series_sha256"]
    assert identity["series"]["patches"] == selected["patches"]
    assert len(patches) == 225 and patches[-1][0] == PATCH
    assert patches[-1][2] == [(BUILD, "modify", None)]


@pytest.mark.parametrize("mutation", ["patch-missing", "patch-changed", "extra", "subdirectory",
                                      "manifest-missing", "manifest-changed", "directory-missing",
                                      "patch-link", "manifest-link", "directory-link"])
def test_additions_fail_closed(repo, mutation):
    patch = repo / PATCH
    manifest = repo / selection.LINUX155_ADDITIONS_MANIFEST
    directory = repo / selection.LINUX155_ADDITIONS_ROOT
    if mutation == "patch-missing":
        patch.unlink()
    elif mutation == "patch-changed":
        patch.write_bytes(patch.read_bytes() + b"\n")
    elif mutation == "extra":
        (directory / "unexpected.patch").write_text("unexpected")
    elif mutation == "subdirectory":
        (directory / "extra").mkdir()
    elif mutation == "manifest-missing":
        manifest.unlink()
    elif mutation == "manifest-changed":
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    elif mutation == "directory-missing":
        shutil.rmtree(directory)
    else:
        path = {"patch-link": patch, "manifest-link": manifest, "directory-link": directory}[mutation]
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        path.symlink_to(ROOT / path.relative_to(repo), target_is_directory=mutation == "directory-link")
    with pytest.raises(selection.SelectionError):
        selection.select(repo, "linux")
    with pytest.raises(selection.SelectionError):
        selection.preparation_key(repo, "linux", "posix")


@pytest.mark.parametrize("platform,version,core,commit", [
    ("windows", selection.VERSION155, selection.CORE155, selection.PLATFORM155_COMMITS["windows"]),
    ("linux", selection.VERSION, selection.CORE, selection.PLATFORM_COMMITS["linux"]),
    ("macos", selection.VERSION, selection.MACOS154_CORE, selection.PLATFORM_COMMITS["macos"]),
    ("linux", "153.0.8010.36", selection.CORE, selection.PLATFORM_COMMITS["linux"]),
])
def test_other_platforms_and_versions_ignore_additions(repo, platform, version, core, commit):
    set_platform_pins(repo, platform, version, core, commit)
    before = selection.select(repo, platform)
    keys = {style: selection.preparation_key(repo, platform, style) for style in ("posix", "windows")}
    assert len(before[1]) == 224
    (repo / selection.LINUX155_ADDITIONS_MANIFEST).unlink()
    shutil.rmtree(repo / selection.LINUX155_ADDITIONS_ROOT)
    assert selection.select(repo, platform) == before
    assert keys == {style: selection.preparation_key(repo, platform, style) for style in keys}


@pytest.mark.parametrize("field", ["LinuxUngoogledCommit", "UngoogledLinuxCommit"])
def test_additions_cannot_bypass_core_platform_pins(repo, field):
    path = repo / "build/ungoogled-revisions.psd1"
    path.write_text(re.sub(rf'(?m)^(  {field} = )"[^"]+"', rf'\g<1>"{"0" * 40}"', path.read_text()))
    with pytest.raises(selection.SelectionError, match="core/platform pins"):
        selection.select(repo, "linux")


@pytest.mark.parametrize("which", ["src", "core"])
def test_additions_preserve_source_version_validation(repo, tmp_path, which):
    root = tmp_path / which
    path = root / ("chrome/VERSION" if which == "src" else "chromium_version.txt")
    path.parent.mkdir(parents=True)
    path.write_text("MAJOR=154\nMINOR=0\nBUILD=8037\nPATCH=97\n" if which == "src" else selection.VERSION + "\n")
    with pytest.raises(selection.SelectionError, match="version"):
        selection.select(repo, "linux", **{which: root})


@pytest.fixture
def patched_sources(tmp_path, source_data):
    src = tmp_path / "src"
    for name, entry in source_data["sources"].items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry["text"])
    applied = []
    for kind in ("core", "linux"):
        series = source_data["tooling"][kind + "/patches/series"]["text"]
        for name in series.splitlines():
            name = name.split("#", 1)[0].strip()
            if not name:
                continue
            entry = source_data["tooling"][kind + "/patches/" + name]
            targets = set(entry["targets"]) & source_data["sources"].keys()
            if not targets:
                continue
            sections = [section for section in re.split(r"(?m)(?=^--- a/)", entry["text"])
                        if (match := re.match(r"--- a/([^\n]+)", section)) and match[1] in targets]
            patch = tmp_path / "predecessor.patch"
            patch.write_text("".join(sections))
            result = run_patch(src, patch)
            assert result.returncode == 0, result.stdout + result.stderr
            assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
            applied.append((kind, name))
    assert applied == [("core", "core/ungoogled-chromium/fix-building-without-safebrowsing.patch"),
                       ("core", "core/ungoogled-chromium/remove-unused-preferences-fields.patch")]
    return src


@pytest.mark.parametrize("substituted", [False, True])
def test_exact_source_application_restores_only_original_tu(patched_sources, source_data, tmp_path, substituted):
    src = patched_sources
    rules = arp._rules(source_data["tooling"]["core/domain_regex.list"]["text"].encode())
    listed = set(source_data["tooling"]["core/domain_substitution.list"]["text"].splitlines())
    assert BUILD not in listed and CONTROLLER + ".cc" not in listed and CONSUMER in listed
    if substituted:
        for name in source_data["sources"].keys() & listed:
            path = src / name
            path.write_text(arp._substitute(path.read_text(), rules))
    before = {name: (src / name).read_bytes() for name in source_data["sources"]}
    assert SOURCE_LINE.encode() not in before[BUILD]
    assert FACTORY.encode() in before[CONTROLLER + ".cc"] and FACTORY.encode() in before[CONSUMER]
    patch, entries = arp.transform_patch((ROOT / PATCH).read_bytes(), listed, rules)
    assert patch == (ROOT / PATCH).read_bytes()
    assert entries == [(BUILD, "modify", None)]
    path = tmp_path / "addition.patch"
    path.write_bytes(patch)
    result = run_patch(src, path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
    after = (src / BUILD).read_text()
    assert after.count(SOURCE_LINE) == 1
    assert after.replace(SOURCE_LINE, "").encode() == before[BUILD]
    original = source_data["sources"][BUILD]["text"]
    assert after == original.replace('    "data_protection/view_source_navigation_throttle.cc",\n', "")
    for name in source_data["sources"].keys() - {BUILD}:
        assert (src / name).read_bytes() == before[name]
    result = run_patch(src, path, reverse=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (src / BUILD).read_bytes() == before[BUILD]


def test_real_implementation_dependencies_and_disabled_signin_path(patched_sources, source_data):
    build = (patched_sources / BUILD).read_text()
    original = source_data["sources"][BUILD]["text"]
    assert build == original.replace(SOURCE_LINE, "").replace(
        '    "data_protection/view_source_navigation_throttle.cc",\n', "")
    for dep in ("//chrome/browser/signin", "//chrome/browser/themes", "//chrome/browser/search_engine_choice",
                "//chrome/browser/ui/signin", "//chrome/browser/ui/webui/signin:signin_utils",
                "//chrome/browser/ui/window_feature_controller", "//chrome/browser/profiles:profile_util",
                "//chrome/browser/ui/browser_window:create_browser_window"):
        assert '"' + dep + '"' in build
    controller = (patched_sources / (CONTROLLER + ".cc")).read_text()
    assert "prefs::" not in controller and "signin_pref_names.h" not in controller
    assert "    OnProfileCreationDone(base::ok(nullptr));\n    return false;" in controller
    assert "  ShowManagementDisclaimer();" not in controller
    utils = (patched_sources / UTILS).read_text()
    body = utils.split("SigninUIError CanOfferSignin(", 1)[1].split("{", 1)[1].split("}", 1)[0]
    assert body.strip() == "return SigninUIError::SigninDisallowed(email);"
    header = source_data["sources"][CONTROLLER + ".h"]["text"]
    def parameters(text):
        params = re.search(FACTORY + r"\((.*?)\)\s*[;{]", text, re.S)[1]
        return re.sub(r"\s+", " ", params.replace(" = std::nullopt", "")).strip()
    assert parameters(header) == parameters(controller)
    assert "void(base::expected<Profile*, ManagedProfileCreationFailureReason>, bool)>;" in header
    consumer = (patched_sources / CONSUMER).read_text()
    assert "ManagedProfileCreationController::CreateManagedProfileForTesting(" in consumer
    assert "profile_separation_policies_for_testing_,\n            user_choice_for_testing_);" in consumer
    for text in (header, controller, consumer):
        assert not re.search(r"^#if.*(?:ENABLE_BOUND_SESSION|IS_LINUX|TEST_ONLY)", text, re.M)


def test_isolated_real_factory_compile_and_missing_object_link(patched_sources, source_data, tmp_path):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        pytest.skip("C++23 compiler required for isolated real-factory contract test")
    controller = (patched_sources / (CONTROLLER + ".cc")).read_text()
    definition = controller[controller.index("// static", controller.index("CreateManagedProfile(")):
                            controller.index("void ManagedProfileCreationController::OnProfileWillBeDestroyed")]
    assert definition.count(FACTORY) == 1
    original_header = source_data["sources"][CONTROLLER + ".h"]["text"]
    declaration = re.search(r"  \[\[nodiscard\]\] static std::unique_ptr<ManagedProfileCreationController>\n  "
                            + FACTORY + r"\(.*?;", original_header, re.S)[0]
    alias = re.search(r"using ManagedProfileCreationControllerCallback = .*?;", original_header, re.S)[0]
    check = source_data["sources"]["base/check_is_test.h"]["text"]
    macro = re.search(r"#define CHECK_IS_TEST\(.*?\n.*?\n", check)[0]
    shim = (ROOT / "tools/tests/fixtures/linux155_managed_profile_factory_shim.h").read_text()
    (tmp_path / "contract.h").write_text(shim.replace("@CALLBACK_ALIAS@", alias)
                                        .replace("@CHECK_IS_TEST@", macro).replace("@DECLARATION@", declaration))
    (tmp_path / "factory.cc").write_text('#include "contract.h"\n' + definition)
    consumer = ROOT / "tools/tests/fixtures/linux155_managed_profile_factory_consumer.cc"
    for bound, linux, test_only in itertools.product((0, 1), repeat=3):
        flags = [f"-DENABLE_BOUND_SESSION_CREDENTIALS={bound}", f"-DIS_LINUX={linux}", f"-DTEST_ONLY={test_only}"]
        def run(args):
            return subprocess.run([compiler, "-std=c++23", "-Wall", "-Wextra", "-Werror", *flags,
                                   "-I", str(tmp_path), *args], cwd=tmp_path,
                                  capture_output=True, text=True, timeout=60)
        result = run(["-c", "factory.cc", "-o", "factory.o"])
        assert result.returncode == 0, result.stdout + result.stderr
        result = run(["-c", str(consumer), "-o", "consumer.o"])
        assert result.returncode == 0, result.stdout + result.stderr
        missing = run(["consumer.o", "-o", "missing"])
        assert missing.returncode != 0 and FACTORY in missing.stderr
        linked = run(["consumer.o", "factory.o", "-o", "isolated"])
        assert linked.returncode == 0, linked.stdout + linked.stderr
        result = subprocess.run([str(tmp_path / "isolated")], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("substituted", [False, True])
def test_optional_independent_linux155_225_full_stack(tmp_path, source_data, substituted):
    root = os.environ.get("CHROMIX_155_SOURCES")
    if not root:
        pytest.skip("set CHROMIX_155_SOURCES to independent pristine/core/linux directories")
    donor = Path(root).resolve()
    pristine, core, tooling = donor / "pristine", donor / "core", donor / "linux"
    manifest_raw = (donor / "source-manifest.json").read_bytes()
    assert selection.digest(manifest_raw) == MANIFEST
    manifest = json.loads(manifest_raw)
    assert manifest["version"] == selection.VERSION155 and not manifest["errors"]
    assert manifest["revisions"] == {
        "v8": "56832c166a78bd1eb61068ac69876d6880eaabdf",
        "webrtc": "5bd86a2f17a35dfc986b87d058f511e45805bd1d",
    }
    assert len(manifest["files"]) == 150 and BUILD not in manifest["files"]
    for name, entry in manifest["files"].items():
        assert selection.digest((pristine / name).read_bytes()) == entry["sha256"]
    for name in ("core", "linux"):
        authenticate_donor(donor, name)
    assert (core / "chromium_version.txt").read_text().strip() == selection.VERSION155
    assert (core / "revision.txt").read_text().strip() == "1"

    repo = clone_repo(tmp_path, additions=True)
    set_platform_pins(repo, "linux", selection.VERSION155, selection.CORE155,
                      selection.PLATFORM155_COMMITS["linux"])
    src = tmp_path / "src"
    shutil.copytree(pristine, src)
    # Supplement the authenticated donor in scratch; its original manifest remains unchanged.
    supplement = source_data["sources"][BUILD]
    path = src / BUILD
    if path.exists():
        assert path.read_bytes() == supplement["text"].encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(supplement["text"].encode())
    provenance = {
        "donor_manifest_sha256": MANIFEST,
        "chromium_commit": source_data["chromium_commit"],
        "supplement": {"path": BUILD, "sha256": supplement["sha256"],
                       "git_blob": supplement["git_blob"],
                       "fixture_sha256": FIXTURE_SHA256},
    }
    (tmp_path / "source-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    selected, patches = selection.select(repo, "linux", src=src, core=core)
    assert len(patches) == 225 and patches[-1][0] == PATCH
    assert selected["selection"]["additions"]["manifest"]["sha256"] == selection.LINUX155_ADDITIONS_SHA256
    entries = [entry for _, raw in patches for entry in arp.transform_patch(raw, set(), [])[1]]
    targets = {entry[0] for entry in entries}
    assert len(targets) == 168 and BUILD in targets
    first_actions = {}
    for target, action, _ in entries:
        first_actions.setdefault(target, action)
    supplied = set(manifest["files"]) | {BUILD}
    lite = {path.relative_to(repo / arp.LITE).as_posix()
            for path in (repo / arp.LITE).rglob("*") if path.is_file()}
    core_sections = list(predecessor_sections(core, targets))
    platform_sections = list(predecessor_sections(tooling, targets))
    created_by_predecessors = {target for _, target, section in [*core_sections, *platform_sections]
                               if section.startswith("--- /dev/null\n")}
    assert created_by_predecessors == {"components/ungoogled/BUILD.gn"}
    assert {target for target, action in first_actions.items() if action != "create"} <= (
        supplied | lite | created_by_predecessors)
    assert len(core_sections) == 23
    assert [(name, target) for name, target, _ in core_sections if target == BUILD] == [
        ("core/ungoogled-chromium/fix-building-without-safebrowsing.patch", BUILD),
        ("core/ungoogled-chromium/remove-unused-preferences-fields.patch", BUILD),
    ]
    assert [(name, target) for name, target, _ in platform_sections] == [
        ("ungoogled-chromium/portablelinux/revert-crubit-changes.patch",
         "third_party/blink/renderer/platform/BUILD.gn"),
    ]
    log = []
    for name, target, section in [*core_sections, *platform_sections]:
        predecessor = tmp_path / "predecessor.patch"
        predecessor.write_text(section)
        result = run_patch(src, predecessor)
        log.append(name + ":" + target + "\n" + result.stdout + result.stderr)
        assert result.returncode == 0, log[-1]
        assert not re.search(r"fuzz|FAILED", log[-1], re.I)
    assert SOURCE_LINE not in (src / BUILD).read_text()

    if substituted:
        rules = arp._rules((core / "domain_regex.list").read_bytes())
        listed = set((core / "domain_substitution.list").read_text().splitlines())
        for name in targets & listed:
            path = src / name
            if path.is_file():
                text, encoding = arp._decode(path.read_bytes())
                path.write_bytes(arp._substitute(text, rules).encode(encoding))
        program = shutil.which("gpatch") or "patch"
        applied = arp.run_apply(src, repo, core, tooling, "linux", program)
        assert applied["status"] == "applied" and applied["patch_count"] == 225
        checked = arp.run_apply(src, repo, core, tooling, "linux", program, check=True)
        assert checked["status"] == "checked" and checked["patch_count"] == 225
        assert checked["identity_sha256"] == applied["identity_sha256"]
        (tmp_path / "checked.json").write_text(json.dumps(checked, indent=2) + "\n")
        (src / ".chromix-domain-substituted").write_text(selection.CORE155)
    else:
        shutil.copytree(repo / arp.LITE, src, dirs_exist_ok=True)
        for name, _ in patches:
            result = run_patch(src, repo / name)
            log.append(name + "\n" + result.stdout + result.stderr)
            assert result.returncode == 0, log[-1]
            assert not re.search(r"fuzz|FAILED", log[-1], re.I)
        applied = {"status": "applied", "patch_count": len(patches), "identity": selected}
    (tmp_path / "applied.json").write_text(json.dumps(applied, indent=2) + "\n")
    (tmp_path / "apply.log").write_text("\n".join(log))
    assert (src / BUILD).read_text() == supplement["text"].replace(
        '    "data_protection/view_source_navigation_throttle.cc",\n', "")
    before = {name: ((src / name).read_bytes(), (src / name).stat().st_mtime_ns)
              if (src / name).exists() else None for name in targets}
    output = tmp_path / "verified.json"
    result = subprocess.run([
        sys.executable, str(ROOT / "tools/verify_patch_stack.py"),
        "--src", str(src), "--repo", str(repo), "--core", str(core),
        "--platform-tooling", str(tooling), "--platform", "linux", "--output", str(output)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    verified = json.loads(output.read_text())
    assert verified["status"] == "verified" and verified["patch_count"] == 225
    assert verified["method"] == "reverse-forward-in-scratch"
    assert verified["domain_substituted"] == substituted
    assert verified["identity"]["selection"] == selected["selection"]
    stack = verified["identity"]["series"] if substituted else verified["identity"]
    assert stack["patches"] == selected["patches"]
    assert set(verified["outputs"]) == targets
    assert verified["outputs"][BUILD] == selection.digest((src / BUILD).read_bytes())
    assert before == {name: ((src / name).read_bytes(), (src / name).stat().st_mtime_ns)
                      if (src / name).exists() else None for name in targets}
    assert (donor / "source-manifest.json").read_bytes() == manifest_raw
    for name, entry in manifest["files"].items():
        assert selection.digest((pristine / name).read_bytes()) == entry["sha256"]
