#!/usr/bin/env python3
"""Bind helper-only recovery to the failed macOS 154 Intel stage-2 checkpoint."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

try:
    from . import patch_selection, validate_posix_snapshot
    from .platform_pins import load_pins
    from .restore_upstream_cache import gn_assignments_last_wins
except ImportError:
    import patch_selection
    import validate_posix_snapshot
    from platform_pins import load_pins
    from restore_upstream_cache import gn_assignments_last_wins

REPOSITORY = "xiaozhou26/Chromix"
RUN = 38165640844
JOB = 114600052208
STAGE = 2
ATTEMPT = 1
SHA = "4632c980d1154ff59243b97fa1b0b816d2440eaa"
BRANCH = "build/macos154-donor-37654894671"
ARTIFACT_IDS = (11719847390, 11719568394)
FAILED_ARTIFACT = 11718979926
VERSION = "154.0.8037.97"
RECIPE_FILES = ("build/prepare-ungoogled.sh", "build/apply-patches.sh",
                "build/macos/build.sh", "build/args.macos.gn")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_metadata(client, repository, artifact_ids):
    require(repository == REPOSITORY, "recovery is restricted to the recorded repository")
    require(len(artifact_ids) == len(ARTIFACT_IDS) and set(artifact_ids) == set(ARTIFACT_IDS),
            "only the exact recorded stage-2 artifact IDs are accepted")
    report = validate_posix_snapshot.validate(
        client, repository, RUN, STAGE, ATTEMPT, "x64", artifact_ids,
        platform="macos", recovery_branch=BRANCH)
    require(report["head_sha"] == SHA and report["job_id"] == JOB,
            "checkpoint SHA or selected stage job differs from the recorded producer")
    run = client.get(f"/actions/runs/{RUN}")
    require(run.get("head_branch") == BRANCH and run.get("head_sha") == SHA
            and run.get("event") == "workflow_dispatch" and run.get("run_attempt") == ATTEMPT
            and run.get("status") == "completed" and run.get("conclusion") == "failure",
            "expected the exact completed failed manual producer attempt")
    job = client.get(f"/actions/jobs/{JOB}")
    require(job.get("id") == JOB and job.get("run_id") == RUN
            and job.get("head_sha") == SHA and job.get("run_attempt") == ATTEMPT
            and job.get("status") == "completed" and job.get("conclusion") == "failure",
            "recorded stage-2 job is not the completed failed producer")
    steps = {step["name"]: step.get("conclusion") for step in job.get("steps", [])}
    for name, conclusion in {
        "Run stage 2": "failure", "Upload failed macOS runtime bundle": "success",
        "Preserve failed macOS runtime checkpoint": "success", "Verify handoff snapshot": "success",
        **{f"Upload tree part {index}": "success" for index in range(1, 5)},
    }.items():
        require(steps.get(name) == conclusion, f"producer step mismatch: {name}")
    artifact = client.get(f"/actions/artifacts/{FAILED_ARTIFACT}")
    require(artifact.get("id") == FAILED_ARTIFACT
            and artifact.get("name") == "chromix-mac-x64-failed-runtime-s2-attempt-1"
            and artifact.get("expired") is False and artifact.get("size_in_bytes", 0) > 0
            and artifact.get("workflow_run", {}).get("id") == RUN
            and artifact.get("workflow_run", {}).get("head_sha") == SHA
            and isinstance(artifact.get("digest"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]),
            "failed-runtime evidence artifact identity or digest mismatch")
    report.update(source_branch=BRANCH, failed_runtime_artifact=artifact,
                  producer_run=run, producer_job=job)
    return report


def read(root, relative):
    return patch_selection.read(root, relative)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify_recipe(repo, donor, work):
    src = work / "src"
    require(subprocess.check_output(["git", "-C", str(donor), "rev-parse", "HEAD"],
                                    text=True).strip() == SHA, "donor checkout SHA mismatch")
    require(not subprocess.check_output(["git", "-C", str(donor), "status", "--porcelain",
                                         "--untracked-files=all"], text=True),
            "donor definitions checkout is not clean")
    recipes = {}
    for name in RECIPE_FILES:
        target, original = read(repo, name), read(donor, name)
        require(target == original, f"preparation/build recipe changed: {name}")
        recipes[name] = sha256(target)
    pins = load_pins(repo, "macos")
    require(pins["ChromiumVersion"] == VERSION and load_pins(donor, "macos") == pins,
            "donor and target macOS pins must be identical")
    core = work / "tooling/ungoogled-chromium"
    identity, patches = patch_selection.select(repo, "macos", src=src, core=core)
    donor_identity, _ = patch_selection.select(donor, "macos", src=src, core=core)
    require(len(patches) == 224 and identity == donor_identity,
            "target is not the exact donor macOS 154 224-patch identity")
    key = patch_selection.preparation_key(repo, "macos", "posix")
    require(key == patch_selection.preparation_key(donor, "macos", "posix"),
            "preparation key changed; migration is forbidden")
    expected = f"macos|x64|{VERSION}|{pins['UngoogledCommit']}|{pins['UngoogledMacOSCommit']}|{key}"
    require(read(src, ".chromix-source-ready").decode().strip() == expected,
            "restored preparation key differs; never rewrite the readiness stamp")
    for name in (".chromix-upstream-restored.json", ".chromix-restored-build-prepared.json",
                 "out/Default/build.ninja", "out/Default/.ninja_log", "out/Default/.ninja_deps"):
        path = src / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0,
                f"missing restored incremental state: {name}")
    args = gn_assignments_last_wins(src / "out/Default/args.gn")
    for name, value in {"target_cpu": '"x64"', "v8_target_cpu": '"x64"',
                        "target_os": '"mac"', "thin_lto_enable_optimizations": "false"}.items():
        require(args.get(name) == value, f"donor fast GN identity mismatch: {name}")
    return {"identity": identity, "preparation_key": expected, "recipes": recipes,
            "args_sha256": sha256(read(src, "out/Default/args.gn"))}


def verify_source(repo, donor, work, report_path, *, baseline=None):
    result = verify_recipe(repo, donor, work)
    if baseline is not None:
        before = json.loads(baseline.read_text())
        require(before.get("status") == "verified", "baseline source admission did not pass")
        for field in ("identity", "preparation_key", "recipes", "args_sha256"):
            require(before.get(field) == result[field], f"admitted source recipe changed: {field}")
        prepared = json.loads(read(work, "upstream-cache-preparation.json"))
        counters = prepared.get("counters", {})
        require(prepared.get("ready_for_gn") is True
                and counters.get("first_finish") == 0
                and counters.get("tool_swap_invalidations") == 0
                and counters.get("toolchain_invalidated_outputs") == 0,
                "helper recovery cannot fall back to full toolchain/output invalidation")
    src = work / "src"
    producer_bytes = read(work, "fingerprint-diagnostics/source-final.json")
    producer = json.loads(producer_bytes)
    receipts = []
    for label, definitions in (("donor", donor), ("target", repo)):
        output = report_path.with_name(report_path.stem + f"-{label}.json")
        subprocess.run([sys.executable, str(repo / "tools/verify_patch_stack.py"),
                        "--src", str(src), "--repo", str(definitions),
                        "--core", str(work / "tooling/ungoogled-chromium"),
                        "--platform-tooling", str(work / "tooling/ungoogled-chromium-macos"),
                        "--platform", "macos", "--output", str(output)], check=True)
        receipts.append(json.loads(output.read_text()))
    for receipt in (producer, *receipts):
        require(receipt.get("status") == "verified" and receipt.get("patch_count") == 224
                and receipt.get("identity") == receipts[0]["identity"]
                and receipt.get("outputs") == receipts[0]["outputs"] and receipt.get("outputs"),
                "producer/donor/target source receipt or live source hashes differ")
    result.update(status="verified", source_sha=SHA, producer_receipt_sha256=sha256(producer_bytes),
                  outputs=receipts[-1]["outputs"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    metadata = sub.add_parser("metadata")
    metadata.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    metadata.add_argument("--artifact-ids", required=True)
    source = sub.add_parser("source")
    source.add_argument("--repo", type=Path, required=True)
    source.add_argument("--donor-repo", type=Path, required=True)
    source.add_argument("--workdir", type=Path, required=True)
    source.add_argument("--baseline", type=Path)
    for command in (metadata, source):
        command.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    require(not args.report.exists(), "report must be a new file")
    try:
        if args.phase == "metadata":
            client = validate_posix_snapshot.Client(args.repository, os.environ.get("GH_TOKEN", ""))
            ids = [validate_posix_snapshot.positive(value.strip(), "artifact ID")
                   for value in args.artifact_ids.split(",")]
            result = validate_metadata(client, args.repository, ids)
        else:
            result = verify_source(args.repo.resolve(), args.donor_repo.resolve(),
                                   args.workdir.resolve(), args.report, baseline=args.baseline)
        result["status"] = "verified"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {"status": "failed", "error": str(error)}
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] != "verified")


if __name__ == "__main__":
    raise SystemExit(main())
