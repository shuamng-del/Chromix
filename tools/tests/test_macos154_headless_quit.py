"""Pinned upstream headless quit backport; callback fixtures are not browser acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tools import apply_restored_patches as arp
from tools import check_patches

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/chromium154-macos/0226-headless-command-explicit-quit.patch"
FIXTURE = json.loads((Path(__file__).with_name("fixtures") /
                      "macos154_headless_quit.json").read_text())
TARGET = FIXTURE["target"]


def source_fixture():
    lines = []
    for first, text in FIXTURE["sections"]:
        lines.extend("// unrelated source line\n" for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    return "".join(lines) + "// not EOF\n"


def run_patch(directory, patch=PATCH, *, reverse=False, dry_run=False):
    program = shutil.which("gpatch") or shutil.which("patch")
    if not program:
        pytest.skip("GNU patch is required")
    options = ["--reverse"] if reverse else []
    if dry_run:
        options.append("--dry-run")
    return subprocess.run(
        [program, *arp.PATCH_OPTIONS, *options, "--input", str(patch)],
        cwd=directory, capture_output=True, text=True, timeout=15,
        env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})


def prepare_source(tmp_path):
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    target.write_text(source_fixture())
    return target


def test_upstream_changes_and_patch_integrity():
    raw = PATCH.read_bytes()
    changes = [line for line in raw.decode().splitlines()
               if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    assert hashlib.sha256(("\n".join(changes) + "\n").encode()).hexdigest() == FIXTURE[
        "change_lines_sha256"]
    assert FIXTURE["upstream_commit"] == "7870c327159042185b6918a1c381775fbae37b56"
    assert arp.transform_patch(raw, set(), [])[1] == [(TARGET, "modify", None)]
    report = check_patches.Report()
    check_patches.check_bodies(report, PATCH.parent, False)
    assert not report.failures


def test_strict_forward_reverse_and_duplicate_rejection(tmp_path):
    target = prepare_source(tmp_path)
    original = target.read_bytes()
    for dry_run in (True, False):
        result = run_patch(tmp_path, dry_run=dry_run)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
        if dry_run:
            assert target.read_bytes() == original
    patched = target.read_bytes()
    result = run_patch(tmp_path, dry_run=True)
    assert result.returncode != 0
    assert target.read_bytes() == patched
    for dry_run in (True, False):
        result = run_patch(tmp_path, reverse=True, dry_run=dry_run)
        assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_bytes() == original


@pytest.mark.parametrize("anchor", [
    '#include "chrome/browser/headless/headless_command_processor.h"',
    "      headless::ProcessHeadlessCommands(",
    "                  browser->GetWindow()->Close();",
])
def test_rejects_changed_preimage(tmp_path, anchor):
    target = prepare_source(tmp_path)
    target.write_text(target.read_text().replace(anchor, "// changed upstream anchor", 1))
    result = run_patch(tmp_path, dry_run=True)
    assert result.returncode != 0
    assert "FAILED" in result.stdout


HARNESS = r'''
#include <cassert>
#include <memory>
#include <vector>
#define BUILDFLAG(flag) flag
namespace base {
template <typename T> using WeakPtr = T*;
}
std::vector<int> events;
bool profile_alive = false;
bool app_keepalive = IS_MAC;
bool background_keepalive = true;
bool trying_to_quit = false;
bool terminated = false;
int windows = 1;
struct ScopedProfileKeepAlive {
  ScopedProfileKeepAlive() { profile_alive = true; }
  ~ScopedProfileKeepAlive() { profile_alive = false; events.push_back(3); }
};
namespace headless {
struct HeadlessCommandHandler { enum class Result { kSuccess, kFailure }; };
}
namespace app_controller_mac {
void AllowApplicationToTerminate() {
  assert(profile_alive);
  app_keepalive = false;
  events.push_back(1);
}
}
namespace chrome {
void CloseAllBrowsersAndQuit() {
  assert(profile_alive);
  assert(!app_keepalive);
  trying_to_quit = true;
  windows = 0;
  terminated = true;
  events.push_back(2);
}
}
struct BrowserWindowInterface {
  BrowserWindowInterface* GetWindow() { return this; }
  void Close() {
    windows = 0;
    terminated = !background_keepalive && !app_keepalive;
    events.push_back(4);
  }
};
int main(int argc, char**) {
  auto done = CALLBACK;
  auto result = argc > 1 ? headless::HeadlessCommandHandler::Result::kFailure
                         : headless::HeadlessCommandHandler::Result::kSuccess;
#if LEGACY
  BrowserWindowInterface browser;
  done(&browser, std::make_unique<ScopedProfileKeepAlive>(), result);
  assert(!terminated);
  assert(!trying_to_quit);
#else
  done(std::make_unique<ScopedProfileKeepAlive>(), result);
  assert(terminated);
  assert(trying_to_quit);
  assert(windows == 0);
#if IS_MAC
  assert((events == std::vector<int>{1, 2, 3}));
#else
  assert((events == std::vector<int>{2, 3}));
#endif
#endif
  assert(!profile_alive);
}
'''


@pytest.mark.parametrize("mac", [0, 1])
@pytest.mark.parametrize("legacy", [False, True])
def test_compiles_actual_callback_with_background_keepalive_fixture(tmp_path, mac, legacy):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        pytest.skip("C++ compiler is required")
    target = prepare_source(tmp_path)
    if not legacy:
        result = run_patch(tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
    text = target.read_text()
    match = re.search(r"base::BindOnce\(\s*(\[\].*?\n              })", text, re.S)
    assert match
    callback = match[1]
    if not legacy:
        assert "WeakPtr" not in callback and "GetWindow" not in callback
        assert "std::unique_ptr<ScopedProfileKeepAlive> profile_keepalive" in callback
        assert "chrome::CloseAllBrowsersAndQuit();" in callback
        assert "exit(" not in callback and "QuitClosure" not in callback
    source = tmp_path / "callback.cc"
    source.write_text(HARNESS.replace("CALLBACK", callback))
    binary = tmp_path / "callback"
    result = subprocess.run([compiler, "-std=c++17", "-Wall", "-Werror",
                             f"-DIS_MAC={mac}", f"-DLEGACY={int(legacy)}",
                             str(source), "-o", str(binary)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    for args in ([], ["failure"]):
        result = subprocess.run([str(binary), *args], capture_output=True,
                                text=True, timeout=5)
        assert result.returncode == 0, result.stdout + result.stderr


def test_optional_complete_pinned_source(tmp_path):
    root = os.environ.get("CHROMIX_HEADLESS154_SOURCE")
    if not root:
        pytest.skip("set CHROMIX_HEADLESS154_SOURCE to the independently fetched source tree")
    original = (Path(root) / TARGET).read_bytes()
    assert hashlib.sha256(original).hexdigest() == FIXTURE["source_sha256"]
    lines = original.decode().splitlines(keepends=True)
    for first, text in FIXTURE["sections"]:
        assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    for reverse in (False, True):
        result = run_patch(tmp_path, reverse=reverse)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr, re.I)
    assert target.read_bytes() == original
