#!/usr/bin/env python3
"""Invalidate missing mac154 x64 default-helper copies after workflow admission.

Donor/source receipts are validated by the restoring workflow, not this tool.
Run with no concurrent build, after restore/prepare and before incremental Ninja.
Only --apply mutates outputs; neither mode compiles or changes GN inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

VERSION = "154.0.8037.97"
HELPER = "Chromium Helper.app"
EXECUTABLE = HELPER + "/Contents/MacOS/Chromium Helper"
LINKED = "obj/chrome/chrome_helper_app_default_executable/Chromium Helper"
FRAMEWORK = "Chromium Framework.framework"
FRAMEWORK_HELPER = FRAMEWORK + "/Versions/" + VERSION + "/Helpers/" + HELPER
APP_FRAMEWORK = "Chromium.app/Contents/Frameworks/" + FRAMEWORK
APP_HELPER = APP_FRAMEWORK + "/Versions/" + VERSION + "/Helpers/" + HELPER
TARGET = "chrome_helper_app_default"
RECORD = ".chromix-macos-helper-repair.json"
# Chromium 154 chrome/BUILD.gn:766-915 and build/config/mac/rules.gni:490-510.
# The default uses chrome_exe_main_mac.cc; the aperitif helper uses a different C launcher.
GITILES = "https://chromium.googlesource.com/chromium/src/+/refs/tags/" + VERSION


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local(root: Path, name: str) -> Path:
    path = Path(name)
    if (not name or path.is_absolute() or path.as_posix() != name
            or any(part in (".", "..") for part in path.parts)
            or any(c in name for c in ("\\", "\0", "\n", "\r"))):
        raise ValueError(f"unsafe output path: {name!r}")
    result = root / path
    for parent in [*reversed(result.parents), result]:
        if parent.is_symlink():
            raise ValueError(f"symlink in output path: {parent}")
    return result


def present(root: Path, name: str) -> bool:
    path = local(root, name)
    if not path.exists():
        return False
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_size or not info.st_mode & 0o111:
        raise ValueError(f"helper exists but is not a nonempty executable: {name}")
    return True


class Graph:
    def __init__(self, out: Path, ninja: str):
        self.out, self.ninja = out, ninja
        self.queries = {}

    def query(self, name: str) -> dict | None:
        if name in self.queries:
            return self.queries[name]
        result = subprocess.run([self.ninja, "-C", str(self.out), "-t", "query", name],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            if result.returncode == 1 and f"unknown target '{name}'" in result.stderr:
                self.queries[name] = None
                return None
            raise ValueError(f"Ninja query failed for {name}: {result.stderr.strip()}")
        lines = result.stdout.splitlines()
        if not lines or lines[0] != name + ":":
            raise ValueError(f"unexpected Ninja query response: {name}")
        rule, inputs, section = None, [], None
        for line in lines[1:]:
            if line.startswith("  input: "):
                rule, section = line[len("  input: "):], "inputs"
            elif line in ("  outputs:", "  validations:"):
                section = "other"
            elif line.startswith("    ") and section == "inputs":
                inputs.append(re.sub(r"^\|\|? ", "", line[4:]))
            elif line.strip() and not (section == "other" and line.startswith("    ")):
                raise ValueError(f"unsupported Ninja query line for {name}: {line}")
        node = {"rule": rule, "inputs": inputs}
        self.queries[name] = node
        return node

    def require(self, name: str) -> dict:
        node = self.query(name)
        if node is None or node["rule"] is None:
            raise ValueError(f"missing declared Ninja producer: {name}")
        return node

    def copy(self, name: str, source: str) -> None:
        node = self.require(name)
        if not node["rule"].endswith("copy_bundle_data") or source not in node["inputs"]:
            raise ValueError(f"unexpected bundle copy edge: {name} <- {source}")

    def reaches(self, start: str, end: str, roots: tuple[str, ...]) -> bool:
        pending, seen = [start], set()
        while pending:
            name = pending.pop(0)
            if name == end:
                return True
            if name in seen:
                continue
            seen.add(name)
            if len(seen) > 128:
                raise ValueError("unbounded helper bundle graph")
            node = self.require(name)
            if node["rule"] != "phony" and not node["rule"].endswith("stamp"):
                continue
            if end in node["inputs"]:
                return True
            for dep in node["inputs"]:
                if any(dep.startswith(root) for root in roots):
                    pending.append(dep)
        return False


def admission(src: Path, out: Path, arch: str, version: str) -> dict:
    if arch != "x64" or version != VERSION:
        raise ValueError("repair is restricted to macOS 154.0.8037.97 x64")
    raw = (src / "chrome/VERSION").read_bytes()
    values = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)\s*$", raw.decode(), re.M))
    if ".".join(values.get(key, "") for key in ("MAJOR", "MINOR", "BUILD", "PATCH")) != VERSION:
        raise ValueError("source chrome/VERSION does not match mac154 repair")
    args = local(out, "args.gn").read_bytes()
    cpus = re.findall(r'^\s*target_cpu\s*=\s*"([^"]+)"\s*(?:#.*)?$', args.decode(), re.M)
    if cpus != ["x64"]:
        raise ValueError("args.gn must explicitly select target_cpu x64 exactly once")
    return {"version": version, "arch": arch, "source_version_sha256": digest(raw),
            "args_sha256": digest(args), "build_ninja_sha256": digest(local(out, "build.ninja").read_bytes())}


def plan(src: Path, out: Path, ninja: str, arch: str, version: str) -> dict:
    identity = admission(src, out, arch, version)
    graph = Graph(out, ninja)
    # Query the missing executable before any alias or directory-level output.
    graph.copy(EXECUTABLE, LINKED)
    linked = graph.require(LINKED)
    if not linked["rule"].endswith("link"):
        raise ValueError("default helper launcher has no link producer")
    if not graph.reaches(TARGET, EXECUTABLE, (HELPER, "obj/chrome/" + TARGET, "phony/chrome/" + TARGET)):
        raise ValueError("default helper alias does not own the exact executable")
    if not graph.reaches(HELPER, EXECUTABLE, ("obj/chrome/" + TARGET, "phony/chrome/" + TARGET)):
        raise ValueError("default helper bundle does not depend on its executable")
    graph.copy(FRAMEWORK_HELPER, HELPER)
    if not graph.reaches(FRAMEWORK, FRAMEWORK_HELPER,
                         ("obj/chrome/chrome_framework", "phony/chrome/chrome_framework")):
        raise ValueError("framework bundle does not own the default helper copy")
    graph.copy(APP_FRAMEWORK, FRAMEWORK)
    if not graph.reaches("chrome", APP_FRAMEWORK,
                         ("chrome_app", "Chromium.app", "obj/chrome/chrome", "phony/chrome/chrome")):
        raise ValueError("chrome target does not assemble the final framework copy")
    suffix = "/Contents/MacOS/Chromium Helper"
    missing = [name for name in (EXECUTABLE, FRAMEWORK_HELPER + suffix, APP_HELPER + suffix)
               if not present(out, name)]
    outputs = []
    if EXECUTABLE in missing:
        # The file itself is declared and missing: Ninja recopies/relinks it without deletion.
        outputs.append(EXECUTABLE)
    if EXECUTABLE in missing or FRAMEWORK_HELPER + suffix in missing:
        outputs.append(FRAMEWORK_HELPER)
    if missing:
        # This directory is the declared copy output, not the original linked framework.
        outputs.append(APP_FRAMEWORK)
    for name in outputs:
        local(out, name)
    return {"schema_version": 1, "identity": identity, "queries": graph.queries,
            "missing_executables": missing, "invalidate_outputs": outputs,
            "rebuild_targets": [TARGET, "chrome"], "source_reference": GITILES + "/chrome/BUILD.gn"}


def publish(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".helper-repair-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def repair(src: Path, *, out: Path | None = None, ninja: str, arch: str,
           version: str = VERSION, apply: bool = False) -> dict:
    src = src.resolve(strict=True)
    out = src / "out/Default" if out is None else out.absolute()
    try:
        relative = out.relative_to(src).as_posix()
    except ValueError as exc:
        raise ValueError("out must be inside the admitted source tree") from exc
    local(src, relative)
    current = plan(src, out, ninja, arch, version)
    record = local(out, RECORD)
    if record.exists():
        previous = json.loads(record.read_text())
        if (not isinstance(previous, dict) or previous.get("schema_version") != 1
                or previous.get("identity") != current["identity"]
                or previous.get("queries") != current["queries"]
                or previous.get("status") not in ("pending", "invalidated")
                or previous.get("rebuild_targets") != current["rebuild_targets"]):
            raise ValueError("helper repair receipt does not match the current graph/inputs")
        outputs = previous.get("invalidate_outputs")
        allowed = [EXECUTABLE, FRAMEWORK_HELPER, APP_FRAMEWORK]
        if (not isinstance(outputs, list) or not outputs
                or outputs != [name for name in allowed if name in outputs]
                or APP_FRAMEWORK not in outputs):
            raise ValueError("unsafe helper repair receipt outputs")
        if previous["status"] == "invalidated":
            return {**previous, "status": "already_invalidated",
                    "remaining_missing_executables": current["missing_executables"]}
        current = previous
    if not apply or not current["invalidate_outputs"]:
        return {**current, "status": "planned" if current["invalidate_outputs"] else "not_needed"}
    # Check every deletion before publishing the resumable receipt or removing anything.
    for name in current["invalidate_outputs"]:
        path = local(out, name)
        if path.exists() and not (path.is_file() or path.is_dir()):
            raise ValueError(f"unexpected output type: {name}")
    publish(record, {**current, "status": "pending"})
    for name in current["invalidate_outputs"]:
        path = local(out, name)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    # -n does not run build commands or update .ninja_log/.ninja_deps.
    result = subprocess.run([ninja, "-C", str(out), "-n", "-d", "explain",
                             *current["invalidate_outputs"]],
                            capture_output=True, text=True, check=False)
    if result.returncode or "no work to do" in result.stdout:
        raise ValueError("Ninja did not schedule helper recopy; pending receipt retained: "
                         + result.stdout + result.stderr)
    proof = {**current, "status": "invalidated", "dry_run": {
        "targets": current["invalidate_outputs"], "stdout": result.stdout, "stderr": result.stderr}}
    publish(record, proof)
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--ninja", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = repair(args.src, out=args.out, ninja=args.ninja, arch=args.arch,
                        version=args.version, apply=args.apply)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"helper repair failed: {exc}\n")
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
