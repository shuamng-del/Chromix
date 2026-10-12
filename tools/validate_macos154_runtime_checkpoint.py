#!/usr/bin/env python3
"""Verify exact macOS 154 donors and phase-specific incremental runtime recovery."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    from . import migrate_restored_snapshot as migration
    from . import patch_selection, validate_posix_snapshot
    from . import validate_macos_helper_checkpoint as helper
except ImportError:
    import migrate_restored_snapshot as migration
    import patch_selection
    import validate_posix_snapshot
    import validate_macos_helper_checkpoint as helper

PROFILES = {
    "macos154-arm64-runtime-v1": {
        "arch": "arm64", "run": 38070853641, "job": 114390279353, "stage": 4,
        "artifacts": (
            (11695671646, 9663676674, "c7e1feceba51d78e41554b5cb3d280df12ad98246ddf81b7d2777027f3bf5ac3"),
            (11695277300, 944487037, "2112ca26df6c21b427682b05eea4d1e30964f83f651c8aeae45fc9a5f8f359b8"),
        ),
    },
    "macos154-x64-runtime-v1": {
        "arch": "x64", "run": 38165640844, "job": 114600052208, "stage": 2,
        "artifacts": (
            (11719847390, 9663676674, "af3d0d24d052c2976989da49db1ebd960f42f7182bb4534a2b9bcb0d201c0913"),
            (11719568394, 86344721, "bf83d2f0ac0aa1d564e164920f06f11dd1e482ac1ed939fb0efd8d4ed534f0d8"),
        ),
    },
}
PATCH = "patches/chromium154-macos/0226-headless-command-explicit-quit.patch"
PATCH_SHA256 = "ec1868b516e0ffea9366952f1e2d9c424aa2d5555b0e12d2bc456fd631d0ab73"
STATE_FILES = ("out/Default/args.gn", "out/Default/build.ninja",
               "out/Default/.ninja_log", "out/Default/.ninja_deps")
require = helper.require
read = helper.read
sha256 = helper.sha256


def validate_metadata(client, repository, artifact_ids, profile):
    selected = PROFILES[profile]
    expected = selected["artifacts"]
    require(repository == helper.REPOSITORY, "recorded repository required")
    require(len(artifact_ids) == len(expected)
            and set(artifact_ids) == {item[0] for item in expected},
            "complete exact recorded artifact ID set required")
    report = validate_posix_snapshot.validate(
        client, repository, selected["run"], selected["stage"], 1, selected["arch"],
        artifact_ids, platform="macos", recovery_branch=helper.BRANCH)
    require(report["head_sha"] == helper.SHA and report["job_id"] == selected["job"],
            "recorded producer SHA/job mismatch")
    run = client.get(f"/actions/runs/{selected['run']}")
    require(run.get("head_branch") == helper.BRANCH and run.get("event") == "workflow_dispatch"
            and run.get("run_attempt") == 1 and run.get("conclusion") == "failure",
            "exact failed manual producer attempt required")
    job = client.get(f"/actions/jobs/{selected['job']}")
    require(job.get("id") == selected["job"] and job.get("run_id") == selected["run"]
            and job.get("head_sha") == helper.SHA and job.get("run_attempt") == 1
            and job.get("status") == "completed" and job.get("conclusion") == "failure",
            "recorded failed stage job required")
    steps = {step["name"]: step.get("conclusion") for step in job.get("steps", [])}
    for name, conclusion in {
        f"Run stage {selected['stage']}": "failure",
        "Upload failed macOS runtime bundle": "success",
        "Preserve failed macOS runtime checkpoint": "success",
        "Verify handoff snapshot": "success",
        **{f"Upload tree part {index}": "success" for index in range(1, 5)},
    }.items():
        require(steps.get(name) == conclusion, f"producer step mismatch: {name}")
    actual = [(item["id"], item["size_in_bytes"], item["digest"])
              for item in report["artifacts"]]
    require(actual == [(identifier, size, "sha256:" + digest)
                       for identifier, size, digest in expected],
            "recorded artifact part/size/SHA-256 mismatch")
    report.update(profile=profile, source_branch=helper.BRANCH, producer_run=run, producer_job=job)
    return report


def recipe(repo, donor, work, profile):
    arch = PROFILES[profile]["arch"]
    require(migration._donor_git(donor, repo, "rev-parse", "HEAD").decode().strip() == helper.SHA,
            "exact donor checkout required")
    require(not migration._donor_git(donor, repo, "status", "--porcelain", "--untracked-files=all"),
            "donor definitions checkout must be clean")
    # The migration policy owns any exact selector transition, never this scaffold.
    migration._same_inputs(donor, repo, "macos", arch)
    recipes = {}
    for name in helper.RECIPE_FILES:
        original = read(donor, name)
        require(read(repo, name) == original, f"preparation/build recipe changed: {name}")
        recipes[name] = sha256(original)
    pins = helper.load_pins(repo, "macos")
    require(pins["ChromiumVersion"] == helper.VERSION and helper.load_pins(donor, "macos") == pins,
            "macOS donor pins changed")
    src, core = work / "src", work / "tooling/ungoogled-chromium"
    old_identity, old_patches = patch_selection.select(donor, "macos", src=src, core=core)
    identity, patches = patch_selection.select(repo, "macos", src=src, core=core)
    require(len(old_patches) == 224 and len(patches) == 225
            and patches[:224] == old_patches,
            "target must preserve the exact 224-patch donor prefix and select 225 patches")
    require(patches[-1][0] == PATCH and sha256(patches[-1][1]) == PATCH_SHA256,
            "only the official normal-quit patch is admitted")
    state = {}
    for name in (*STATE_FILES, ".chromix-upstream-restored.json",
                 ".chromix-restored-build-prepared.json", ".chromix-toolchain-ready"):
        path = src / name
        require(path.is_file() and not path.is_symlink(), f"missing restored state: {name}")
        data = read(src, name)
        require(data or name == ".chromix-toolchain-ready", f"empty restored state: {name}")
        if name in STATE_FILES:
            state[name] = sha256(data)
    args = helper.gn_assignments_last_wins(src / "out/Default/args.gn")
    for name, value in {"target_cpu": f'"{arch}"', "v8_target_cpu": f'"{arch}"',
                        "target_os": '"mac"', "thin_lto_enable_optimizations": "false"}.items():
        require(args.get(name) == value, f"restored fast GN identity mismatch: {name}")
    tooling = work / "tooling/ungoogled-chromium-macos"
    substituted = (src / ".chromix-domain-substituted").exists()
    donor_receipt_identity, receipt_identity = old_identity, identity
    if substituted:
        donor_receipt_identity, _, _ = migration.arp._load(donor, core, tooling, "macos", src=src)
        receipt_identity, _, _ = migration.arp._load(repo, core, tooling, "macos", src=src)
    return dict(profile=profile, source_sha=helper.SHA, recipes=recipes, state=state,
                donor_identity=donor_receipt_identity, identity=receipt_identity, patch_count=len(patches),
                previous_key=migration.source_ready_key(donor, "macos", arch),
                key=migration.source_ready_key(repo, "macos", arch),
                migration_required=identity != old_identity)


def live_receipt(repo, definitions, work, report_path):
    require(not report_path.exists(), "live source report must be new")
    subprocess.run([sys.executable, str(repo / "tools/verify_patch_stack.py"),
                    "--src", str(work / "src"), "--repo", str(definitions),
                    "--core", str(work / "tooling/ungoogled-chromium"),
                    "--platform-tooling", str(work / "tooling/ungoogled-chromium-macos"),
                    "--platform", "macos", "--output", str(report_path)], check=True)
    receipt = json.loads(report_path.read_text())
    require(receipt.get("status") == "verified" and receipt.get("outputs"),
            "live source receipt must verify nonempty output hashes")
    return receipt


def verify_source(repo, donor, work, profile, report_path, *, baseline=None, prepared=False,
                  build_phase="migration"):
    require(build_phase in ("migration", "graph", "compiled"), "unknown build phase")
    require(build_phase == "migration" or baseline is not None and prepared,
            "graph/compiled checks require prepared target and original baseline")
    result = recipe(repo, donor, work, profile)
    producer_bytes = read(work, "fingerprint-diagnostics/source-final.json")
    producer = json.loads(producer_bytes)
    result["producer_receipt_sha256"] = sha256(producer_bytes)
    if baseline is None:
        require(not prepared, "prepared source check requires baseline")
        migration._ready(work / "src", result["previous_key"])
        receipt = live_receipt(repo, donor, work, report_path.with_name(report_path.stem + "-donor.json"))
        require(receipt.get("patch_count") == 224
                and receipt.get("identity") == result["donor_identity"], "donor source identity mismatch")
        require(producer.get("status") == "verified" and producer.get("patch_count") == 224
                and producer.get("identity") == receipt["identity"]
                and producer.get("outputs") == receipt["outputs"],
                "producer/donor source receipt or live source hashes differ")
        result["donor_outputs"] = receipt["outputs"]
    else:
        before = json.loads(baseline.read_text())
        require(before.get("status") == "verified" and before.get("phase") == "source-before",
                "verified original source admission required")
        for field in result:
            if field != "state":
                require(before.get(field) == result[field], f"admitted source inputs/state changed: {field}")
        old_state, state = before.get("state", {}), result["state"]
        require(set(old_state) == set(state), "incremental state fields changed")
        mutable = set()
        if build_phase in ("graph", "compiled"):
            mutable.add("out/Default/build.ninja")
        if build_phase == "compiled":
            mutable.update(("out/Default/.ninja_log", "out/Default/.ninja_deps"))
        for name in state.keys() - mutable:
            require(state[name] == old_state[name], f"admitted source inputs/state changed: {name}")
        result["state_transition"] = {
            "phase": build_phase, "mutable_fields": sorted(mutable),
            "changed_fields": sorted(name for name in state if state[name] != old_state[name]),
            "before": old_state, "after": state,
        }
        migration._ready(work / "src", result["key"])
        receipt = live_receipt(repo, repo, work, report_path.with_name(report_path.stem + "-target.json"))
        require(receipt.get("patch_count") == result["patch_count"]
                and receipt.get("identity") == result["identity"], "target source identity mismatch")
        if not result["migration_required"]:
            require(receipt["outputs"] == before.get("donor_outputs"), "unchanged source outputs differ")
        if prepared:
            preparation = json.loads(read(work, "upstream-cache-preparation.json"))
            counters = preparation.get("counters", {})
            require(preparation.get("ready_for_gn") is True
                    and all(counters.get(name) == 0 for name in
                            ("first_finish", "tool_swap_invalidations", "toolchain_invalidated_outputs")),
                    "runtime recovery forbids full toolchain/output invalidation")
    result.update(status="verified", outputs=receipt["outputs"],
                  phase="source-before" if baseline is None else "source-after")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("metadata", "source-before", "source-after"))
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--artifact-ids")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--donor-repo", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--prepared", action="store_true")
    parser.add_argument("--build-phase", choices=("migration", "graph", "compiled"), default="migration")
    args = parser.parse_args(argv)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    require(not args.report.exists(), "report must be a new file")
    try:
        if args.phase == "metadata":
            require(bool(args.artifact_ids), "artifact IDs required")
            client = validate_posix_snapshot.Client(args.repository, os.environ.get("GH_TOKEN", ""))
            ids = [validate_posix_snapshot.positive(value.strip(), "artifact ID")
                   for value in args.artifact_ids.split(",")]
            result = validate_metadata(client, args.repository, ids, args.profile)
        else:
            require(all((args.repo, args.donor_repo, args.workdir)), "source paths required")
            require((args.phase == "source-after") == (args.baseline is not None),
                    "only source-after requires a baseline")
            result = verify_source(args.repo.resolve(), args.donor_repo.resolve(), args.workdir.resolve(),
                                   args.profile, args.report, baseline=args.baseline, prepared=args.prepared,
                                   build_phase=args.build_phase)
        result["status"] = "verified"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result = {"status": "failed", "error": str(error), "profile": args.profile}
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if result["status"] == "verified" and args.phase == "source-before" and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("migration_required=" + str(result["migration_required"]).lower() + "\n")
    print(json.dumps(result, sort_keys=True))
    return int(result["status"] != "verified")


if __name__ == "__main__":
    raise SystemExit(main())
