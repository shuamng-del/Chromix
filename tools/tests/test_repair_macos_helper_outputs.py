"""Default-helper repair uses declared copies, never sibling launchers or objects."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repair_macos_helper_outputs as repair


class HelperRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="helper repair ")
        self.addCleanup(self.temp.cleanup)
        self.src = Path(self.temp.name).resolve() / "src"
        self.out = self.src / "out/Default"
        self.out.mkdir(parents=True)
        self.write(self.src / "chrome/VERSION", "MAJOR=154\nMINOR=0\nBUILD=8037\nPATCH=97\n")
        self.write(self.out / "args.gn", 'target_cpu = "x64"\nis_component_build = false\n')
        self.write(self.out / "build.ninja", "# graph fixture\n")
        self.graph = {
            repair.EXECUTABLE: ("copy_bundle_data", [repair.LINKED]),
            repair.LINKED: ("link", ["obj/chrome/default_main.o"]),
            repair.TARGET: ("phony", ["obj/chrome/chrome_helper_app_default.stamp"]),
            "obj/chrome/chrome_helper_app_default.stamp": ("stamp", [repair.EXECUTABLE]),
            repair.HELPER: ("phony", ["obj/chrome/chrome_helper_app_default.stamp"]),
            repair.FRAMEWORK_HELPER: ("copy_bundle_data", [repair.HELPER]),
            repair.FRAMEWORK: ("phony", ["obj/chrome/chrome_framework.stamp"]),
            "obj/chrome/chrome_framework.stamp": ("stamp", [repair.FRAMEWORK_HELPER]),
            repair.APP_FRAMEWORK: ("copy_bundle_data", [repair.FRAMEWORK]),
            "chrome": ("phony", ["obj/chrome/chrome.stamp"]),
            "obj/chrome/chrome.stamp": ("stamp", ["obj/chrome/chrome_app.stamp"]),
            "obj/chrome/chrome_app.stamp": ("stamp", [repair.APP_FRAMEWORK]),
        }
        self.suffix = "/Contents/MacOS/Chromium Helper"
        for name in (repair.HELPER, repair.FRAMEWORK_HELPER, repair.APP_HELPER):
            self.write(self.out / name / "Contents/Info.plist", "plist")
            (self.out / name / "Contents/MacOS").mkdir()
        self.kept = [self.src / "chrome/VERSION", self.out / "args.gn", self.out / "build.ninja"]
        for name in ("obj/chrome/default_main.o", "obj/unrelated.o", ".ninja_log", ".ninja_deps",
                     "gen/untouched.h", "obj/chrome/unrelated.stamp", repair.LINKED,
                     repair.FRAMEWORK + "/Versions/" + repair.VERSION + "/Chromium Framework"):
            self.write(self.out / name, "retained " + name)
            self.kept.append(self.out / name)
        for variant in ("GPU", "Renderer", "Alerts", "Aperitif", "Aperitif GPU", "Aperitif Renderer", "Aperitif Alerts"):
            for prefix in ("", repair.FRAMEWORK + "/Versions/" + repair.VERSION + "/Helpers/"):
                name = f"{prefix}Chromium Helper ({variant}).app/Contents/MacOS/Chromium Helper ({variant})"
                self.executable(self.out / name)
                self.kept.append(self.out / name)
        self.before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in self.kept}
        self.runner = patch.object(repair.subprocess, "run", side_effect=self.ninja)
        self.mock_run = self.runner.start()
        self.addCleanup(self.runner.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def executable(self, path):
        self.write(path, "default launcher fixture")
        path.chmod(0o755)

    def ninja(self, command, **kwargs):
        if "-t" in command:
            name = command[-1]
            if name not in self.graph:
                return subprocess.CompletedProcess(command, 1, "", f"ninja: error: unknown target '{name}'")
            rule, deps = self.graph[name]
            text = name + ":\n  input: " + rule + "\n"
            text += "".join("    " + dep + "\n" for dep in deps) + "  outputs:\n"
            return subprocess.CompletedProcess(command, 0, text, "")
        self.assertIn("-n", command)
        return subprocess.CompletedProcess(command, 0, "[1/2] COPY default helper\n[2/2] COPY framework\n", "ninja explain: output is missing\n")

    def apply(self, **kwargs):
        return repair.repair(self.src, ninja="fixture-ninja", arch="x64", apply=True, **kwargs)

    def assert_kept(self):
        self.assertEqual(self.before, {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in self.kept})

    def complete(self):
        for name in (repair.EXECUTABLE, repair.FRAMEWORK_HELPER + self.suffix, repair.APP_HELPER + self.suffix):
            self.executable(self.out / name)

    def test_missing_default_invalidates_only_exact_directory_copy_outputs(self):
        result = self.apply()
        self.assertEqual(result["status"], "invalidated")
        self.assertEqual(result["invalidate_outputs"], [repair.EXECUTABLE, repair.FRAMEWORK_HELPER, repair.APP_FRAMEWORK])
        self.assertTrue((self.out / repair.HELPER / "Contents/Info.plist").is_file())
        self.assertFalse((self.out / repair.FRAMEWORK_HELPER).exists())
        self.assertFalse((self.out / repair.APP_FRAMEWORK).exists())
        self.assertEqual(self.mock_run.call_args_list[0].args[0][-3:], ["-t", "query", repair.EXECUTABLE])
        self.assert_kept()

    def test_plan_does_not_write_receipt_or_remove_partial_directories(self):
        result = repair.repair(self.src, ninja="fixture-ninja", arch="x64")
        self.assertEqual(result["status"], "planned")
        self.assertFalse((self.out / repair.RECORD).exists())
        self.assertTrue((self.out / repair.APP_HELPER / "Contents/Info.plist").exists())
        self.assertFalse(any("-n" in c.args[0] for c in self.mock_run.call_args_list))
        self.assert_kept()

    def test_complete_bundle_needs_no_invalidation(self):
        self.complete()
        result = self.apply()
        self.assertEqual(result["status"], "not_needed")
        self.assertEqual(result["invalidate_outputs"], [])
        self.assertFalse((self.out / repair.RECORD).exists())
        self.assert_kept()

    def test_missing_only_final_copy_preserves_default_and_source_framework(self):
        self.complete()
        (self.out / (repair.APP_HELPER + self.suffix)).unlink()
        result = self.apply()
        self.assertEqual(result["invalidate_outputs"], [repair.APP_FRAMEWORK])
        self.assertTrue((self.out / repair.EXECUTABLE).is_file())
        self.assertTrue((self.out / (repair.FRAMEWORK_HELPER + self.suffix)).is_file())
        self.assert_kept()

    def test_missing_undeclared_nested_executable_removes_declared_parent(self):
        self.executable(self.out / repair.EXECUTABLE)
        result = self.apply()
        self.assertEqual(result["invalidate_outputs"], [repair.FRAMEWORK_HELPER, repair.APP_FRAMEWORK])
        self.assertNotIn(repair.FRAMEWORK_HELPER + self.suffix, result["queries"])
        self.assert_kept()

    def test_idempotent_before_and_after_rebuild(self):
        self.apply()
        record = self.out / repair.RECORD
        before = record.read_bytes(), record.stat().st_mtime_ns
        self.assertEqual(self.apply()["status"], "already_invalidated")
        self.complete()
        result = self.apply()
        self.assertEqual(result["remaining_missing_executables"], [])
        self.assertEqual(before, (record.read_bytes(), record.stat().st_mtime_ns))
        self.assertTrue((self.out / (repair.APP_HELPER + self.suffix)).is_file())
        self.assert_kept()

    def test_wrong_arch_version_source_or_args_fail_before_query(self):
        for kwargs in ({"arch": "arm64"}, {"version": "155.0.8059.39"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                repair.repair(self.src, ninja="fixture-ninja", **({"arch": "x64"} | kwargs), apply=True)
        self.write(self.out / "args.gn", 'target_cpu = "arm64"\n')
        with self.assertRaisesRegex(ValueError, "target_cpu"):
            self.apply()
        self.write(self.out / "args.gn", 'target_cpu = "x64"\n')
        self.write(self.src / "chrome/VERSION", "MAJOR=155\nMINOR=0\nBUILD=8059\nPATCH=39\n")
        with self.assertRaisesRegex(ValueError, "VERSION"):
            self.apply()
        self.mock_run.assert_not_called()
        self.assertFalse((self.out / repair.RECORD).exists())

    def test_unknown_or_wrong_producer_fails_closed_without_deletion(self):
        for name, value in ((repair.EXECUTABLE, None),
                            (repair.EXECUTABLE, ("copy_bundle_data", ["Chromium Helper (GPU).app"])),
                            (repair.FRAMEWORK_HELPER, ("phony", [repair.HELPER])),
                            (repair.TARGET, ("phony", [])),
                            (repair.APP_FRAMEWORK, ("copy_bundle_data", ["other.framework"]))):
            old = self.graph[name]
            if value is None:
                del self.graph[name]
            else:
                self.graph[name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.apply()
            self.graph[name] = old
            self.assertTrue((self.out / repair.APP_HELPER).is_dir())
            self.assertFalse((self.out / repair.RECORD).exists())
        self.assert_kept()

    def test_receipt_rejects_changed_inputs_or_graph(self):
        self.apply()
        self.graph[repair.LINKED] = ("link", ["obj/chrome/other_main.o"])
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.apply()
        self.graph[repair.LINKED] = ("link", ["obj/chrome/default_main.o"])
        self.write(self.out / "args.gn", 'target_cpu = "x64"\n# changed\n')
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.apply()

    def test_symlinked_output_or_receipt_rejected(self):
        outside = self.src / "retained"
        self.write(outside / "keep", "retained")
        for name in (repair.FRAMEWORK_HELPER, repair.APP_FRAMEWORK, repair.RECORD):
            path = self.out / name
            if path.exists():
                shutil.rmtree(path)
            path.symlink_to(outside, target_is_directory=True)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "symlink"):
                self.apply()
            path.unlink()
        self.assertEqual((outside / "keep").read_text(), "retained")
        self.assert_kept()

    def test_links_inside_deleted_bundle_are_not_followed(self):
        outside = self.src / "retained"
        self.write(outside / "keep", "retained")
        (self.out / repair.APP_FRAMEWORK / "external").symlink_to(outside, target_is_directory=True)
        self.apply()
        self.assertEqual((outside / "keep").read_text(), "retained")
        self.assert_kept()

    def test_bad_receipt_cannot_delete_objects(self):
        result = self.apply()
        result["status"] = "pending"
        result["invalidate_outputs"] = ["obj", repair.APP_FRAMEWORK]
        (self.out / repair.RECORD).write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "unsafe.*receipt"):
            self.apply()
        self.assert_kept()

    def test_dry_run_failure_retains_resumable_pending_receipt(self):
        original = self.mock_run.side_effect
        def failed(command, **kwargs):
            if "-n" in command:
                return subprocess.CompletedProcess(command, 1, "", "missing build input")
            return original(command, **kwargs)
        self.mock_run.side_effect = failed
        with self.assertRaisesRegex(ValueError, "pending receipt retained"):
            self.apply()
        self.assertEqual(json.loads((self.out / repair.RECORD).read_text())["status"], "pending")
        self.mock_run.side_effect = original
        self.assertEqual(self.apply()["status"], "invalidated")
        self.assert_kept()

    def test_ninja_no_work_is_not_success(self):
        original = self.mock_run.side_effect
        def noop(command, **kwargs):
            if "-n" in command:
                return subprocess.CompletedProcess(command, 0, "ninja: no work to do.\n", "")
            return original(command, **kwargs)
        self.mock_run.side_effect = noop
        with self.assertRaisesRegex(ValueError, "did not schedule"):
            self.apply()

    def test_non_executable_or_empty_present_helper_is_not_silently_replaced(self):
        for text, mode in (("", 0o755), ("not executable", 0o644)):
            self.write(self.out / repair.EXECUTABLE, text)
            (self.out / repair.EXECUTABLE).chmod(mode)
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "nonempty executable"):
                self.apply()
            self.assertFalse((self.out / repair.RECORD).exists())

    def test_unsafe_paths(self):
        for name in ("../obj", "/obj", "obj/../args.gn", "./obj", "obj//x", "obj\\x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                repair.local(self.out, name)


NINJA = os.environ.get("CHROMIX_TEST_NINJA") or shutil.which("ninja")


@unittest.skipUnless(NINJA, "Ninja required for real incremental graph test")
class NinjaIntegrationTest(unittest.TestCase):
    def test_partial_directory_cache_is_reassembled_without_compilation(self):
        self.reassemble()

    def test_missing_default_copy_reuses_linked_launcher(self):
        self.reassemble(missing_default=True)

    def test_missing_linked_launcher_relinks_from_preserved_object(self):
        self.reassemble(missing_default=True, missing_linked=True)

    def reassemble(self, missing_default=False, missing_linked=False):
        with tempfile.TemporaryDirectory(prefix="real helper ninja ") as temp:
            src = Path(temp).resolve() / "src"
            out = src / "out/Default"
            out.mkdir(parents=True)
            (src / "chrome").mkdir()
            (src / "chrome/VERSION").write_text("MAJOR=154\nMINOR=0\nBUILD=8037\nPATCH=97\n")
            (out / "args.gn").write_text('target_cpu = "x64"\n')
            obj = out / "obj/chrome/launcher.o"
            obj.parent.mkdir(parents=True)
            obj.write_text("preserved object")
            def esc(value):
                return value.replace("$", "$$").replace(" ", "$ ").replace(":", "$:")
            lines = ["rule link", "  command = mkdir -p obj/chrome/chrome_helper_app_default_executable && cp $in $out && chmod +x $out",
                     "rule copy_bundle_data", "  command = python3 copy.py $in $out"]
            def edge(output, rule, inputs):
                lines.append("build " + esc(output) + ": " + rule + " " + " ".join(map(esc, inputs)))
            edge(repair.LINKED, "link", ["obj/chrome/launcher.o"])
            edge(repair.EXECUTABLE, "copy_bundle_data", [repair.LINKED])
            edge("obj/chrome/chrome_helper_app_default.stamp", "phony", [repair.EXECUTABLE])
            edge(repair.HELPER, "phony", ["obj/chrome/chrome_helper_app_default.stamp"])
            edge(repair.TARGET, "phony", ["obj/chrome/chrome_helper_app_default.stamp"])
            edge(repair.FRAMEWORK_HELPER, "copy_bundle_data", [repair.HELPER])
            edge("obj/chrome/chrome_framework.stamp", "phony", [repair.FRAMEWORK_HELPER])
            edge(repair.FRAMEWORK, "phony", ["obj/chrome/chrome_framework.stamp"])
            edge(repair.APP_FRAMEWORK, "copy_bundle_data", [repair.FRAMEWORK])
            edge("chrome", "phony", [repair.APP_FRAMEWORK])
            (out / "build.ninja").write_text("\n".join(lines) + "\n")
            (out / "copy.py").write_text(
                "import pathlib,shutil,sys\ns,d=map(pathlib.Path,sys.argv[1:])\n"
                "d.parent.mkdir(parents=True,exist_ok=True)\n"
                "if s.is_dir():\n"
                " if d.exists(): shutil.rmtree(d)\n"
                " shutil.copytree(s,d,symlinks=True)\n"
                "else: shutil.copy2(s,d)\n")
            def ninja(*args):
                return subprocess.run([NINJA, "-C", str(out), *args], text=True, capture_output=True, check=True)
            ninja("chrome")
            # Directory mtimes and Ninja logs still say the copy is complete.
            suffix = "/Contents/MacOS/Chromium Helper"
            for name in (repair.FRAMEWORK_HELPER, repair.APP_HELPER):
                (out / (name + suffix)).unlink()
            self.assertIn("no work to do", ninja("-n", "chrome").stdout)
            if missing_default:
                (out / repair.EXECUTABLE).unlink()
            if missing_linked:
                (out / repair.LINKED).unlink()
            protected = [obj, out / ".ninja_log", out / "args.gn", src / "chrome/VERSION"]
            if not missing_linked:
                protected.append(out / repair.LINKED)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected}
            result = repair.repair(src, ninja=NINJA, arch="x64", apply=True)
            expected = ([repair.EXECUTABLE] if missing_default else []) + [repair.FRAMEWORK_HELPER, repair.APP_FRAMEWORK]
            self.assertEqual(result["invalidate_outputs"], expected)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in protected})
            rebuilt = ninja(repair.TARGET, "chrome")
            self.assertEqual("chmod" in rebuilt.stdout, missing_linked)
            self.assertEqual(before[obj], (obj.read_bytes(), obj.stat().st_mtime_ns))
            for name in (repair.FRAMEWORK_HELPER, repair.APP_HELPER):
                self.assertEqual((out / (name + suffix)).read_bytes(), obj.read_bytes())
            self.assertEqual(repair.repair(src, ninja=NINJA, arch="x64", apply=True)["status"], "already_invalidated")
            self.assertIn("no work to do", ninja("-n", "chrome").stdout)


if __name__ == "__main__":
    unittest.main()
