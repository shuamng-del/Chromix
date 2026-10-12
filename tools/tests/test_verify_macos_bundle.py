"""Synthetic Mach-O fixtures; no browser, Apple tools, or signing identity required."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import verify_macos_bundle as verify

REPO = Path(__file__).resolve().parents[2]
HELPERS = ("", " (GPU)", " (Renderer)", " (Alerts)", " (Aperitif)",
           " (Aperitif GPU)", " (Aperitif Renderer)", " (Aperitif Alerts)")


def thin(arch="x64", kind=2, endian="<", subtype=3):
    return struct.pack(endian + "IIIIIIII", 0xFEEDFACF, verify.CPUS[arch], subtype,
                       kind, 0, 0, 0, 0)


def fat(architectures, *, wide=False, endian=">"):
    width = 32 if wide else 20
    offset = 8 + width * len(architectures)
    header = struct.pack(endian + "II", 0xCAFEBABF if wide else 0xCAFEBABE, len(architectures))
    bodies = []
    for index, arch in enumerate(architectures):
        body = thin(arch, subtype=index + 3)
        values = (verify.CPUS[arch], index + 3, offset, len(body), 0)
        header += struct.pack(endian + ("IIQQII" if wide else "IIIII"),
                              *(values + (0,) if wide else values))
        bodies.append(body)
        offset += len(body)
    return header + b"".join(bodies)


class MacOSBundleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="mac bundle ' ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.package = self.root / "chromix"
        self.app = self.package / "Chromium.app"
        self.framework = self.app / "Contents/Frameworks/Chromium Framework.framework"
        self.version = self.framework / "Versions/154.0.8037.97"
        self.normal = self.version / "Helpers/Chromium Helper.app/Contents/MacOS/Chromium Helper"
        self.layout()

    def put(self, path, data, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def plist(self, path, executable):
        self.put(path, plistlib.dumps({"CFBundleExecutable": executable}))

    def layout(self, arch="x64"):
        self.put(self.app / "Contents/MacOS/Chromium", thin(arch), 0o755)
        self.plist(self.app / "Contents/Info.plist", "Chromium")
        self.put(self.version / "Chromium Framework", thin(arch, 6), 0o755)
        self.plist(self.version / "Resources/Info.plist", "Chromium Framework")
        for suffix in HELPERS:
            name = "Chromium Helper" + suffix
            helper = self.version / "Helpers" / (name + ".app")
            self.put(helper / "Contents/MacOS" / name, thin(arch), 0o755)
            self.plist(helper / "Contents/Info.plist", name)
        for name in ("libaperitif.dylib", "libvk_swiftshader.dylib", "libvulkan.dylib"):
            self.put(self.version / "Libraries" / name, thin(arch, 6), 0o755)
        for name in ("app_mode_loader", "chrome_crashpad_handler", "web_app_shortcut_copier"):
            self.put(self.version / "Helpers" / name, thin(arch), 0o755)
        for name in ("Helpers", "Resources", "Libraries", "Chromium Framework"):
            path = self.framework / name
            if not path.is_symlink():
                path.symlink_to("Versions/Current/" + name)
        current = self.framework / "Versions/Current"
        if not current.is_symlink():
            current.symlink_to("154.0.8037.97")
        self.put(self.package / "chromix", b"#!/bin/sh\nexit 0\n", 0o755)
        for path in self.package.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        self.package.chmod(0o755)

    def archive(self, overrides=None, extra=()):
        overrides = overrides or {}
        dest = self.root / "bundle.zip"
        with zipfile.ZipFile(dest, "w") as out:
            for path in [self.package, *self.package.rglob("*")]:
                mode = path.lstat().st_mode
                name = path.relative_to(self.root).as_posix()
                if stat.S_ISDIR(mode):
                    name += "/"
                    data = b""
                elif path.is_symlink():
                    data = os.readlink(path).encode()
                else:
                    data = path.read_bytes()
                mode, data = overrides.get(name, (mode, data))
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                out.writestr(info, data)
            for name, mode, data in extra:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                out.writestr(info, data)
        return dest

    def fail(self, pattern, arch="x64"):
        with self.assertRaisesRegex(verify.BundleError, pattern):
            verify.verify_bundle(self.app, arch)

    def test_complete_unsigned_eight_helpers_both_architectures(self):
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                self.layout(arch)
                with mock.patch.object(verify.subprocess, "run", side_effect=AssertionError("native tool")):
                    self.assertEqual(verify.verify_bundle(self.app, arch), 16)
                    self.assertEqual(verify.verify_zip(self.archive(), arch), 16)

    def test_only_normal_helper_is_mandatory_for_older_layouts(self):
        for suffix in HELPERS[1:]:
            shutil.rmtree(self.version / "Helpers" / ("Chromium Helper" + suffix + ".app"))
        self.assertEqual(verify.verify_bundle(self.app, "x64"), 9)

    def test_missing_normal_executable_is_first_failure(self):
        self.normal.unlink()
        self.put(self.app / "Contents/MacOS/Chromium", b"wrong header", 0o755)
        self.fail("missing required normal Helper executable.*Chromium Helper")
        with self.assertRaisesRegex(verify.BundleError, "missing required normal Helper executable"):
            verify.verify_zip(self.archive(), "x64")

    def test_missing_entire_normal_helper_app(self):
        shutil.rmtree(self.normal.parents[2])
        self.fail("missing required normal Helper executable")

    def test_discovered_optional_helper_missing_executable_or_plist(self):
        for suffix in HELPERS[1:]:
            with self.subTest(suffix=suffix):
                path = self.version / "Helpers" / ("Chromium Helper" + suffix + ".app")
                binary = path / "Contents/MacOS" / ("Chromium Helper" + suffix)
                binary.unlink()
                self.fail("missing bundle path")
                self.layout()
                (path / "Contents/Info.plist").unlink()
                self.fail("missing bundle path")
                self.layout()

    def test_all_plist_executables_are_checked(self):
        helper = self.version / "Helpers/Other.app"
        self.plist(helper / "Contents/Info.plist", "absent")
        self.fail("missing bundle path")

    def test_plist_path_injection_and_malformed_data(self):
        path = self.app / "Contents/Info.plist"
        for name in ("../Chromium", "/Chromium", "C:evil", "", "a\\b"):
            with self.subTest(name=name):
                self.plist(path, name)
                self.fail("unsafe path component")
        self.put(path, b"not plist")
        self.fail("invalid Info.plist")
        self.put(path, plistlib.dumps({}))
        self.fail("missing CFBundleExecutable")

    def test_framework_executable_and_plist(self):
        for path in (self.version / "Chromium Framework", self.version / "Resources/Info.plist"):
            original = path.read_bytes()
            path.unlink()
            self.fail("missing bundle path")
            self.put(path, original, 0o755 if path.name == "Chromium Framework" else 0o644)

    def test_framework_filetype_is_dylib(self):
        self.put(self.version / "Chromium Framework", thin(kind=2), 0o755)
        self.fail("wrong Mach-O file type")

    def test_noncurrent_framework_version_checked(self):
        old = self.framework / "Versions/152.0.0.0"
        self.plist(old / "Resources/Info.plist", "Chromium Framework")
        self.put(old / "Chromium Framework", thin("arm64", 6), 0o755)
        self.fail("architecture mismatch")

    def test_all_macho_headers_even_unlisted_libraries_checked(self):
        for relative in ("Libraries/libaperitif.dylib", "Helpers/chrome_crashpad_handler",
                         "Resources/unlisted-binary"):
            with self.subTest(relative=relative):
                path = self.version / relative
                self.put(path, thin("arm64"), 0o755)
                self.fail("architecture mismatch")
                path.unlink()
                self.layout()

    def test_dylib_without_macho_header_rejected(self):
        self.put(self.version / "Libraries/broken.dylib", b"bad", 0o755)
        self.fail("not a Mach-O dylib")

    def test_thin_endianness_and_unknown_cpu(self):
        self.put(self.normal, thin(endian=">"), 0o755)
        self.assertEqual(verify.verify_bundle(self.app, "x64"), 16)
        data = bytearray(thin())
        struct.pack_into("<I", data, 4, 0x01000012)
        self.put(self.normal, data, 0o755)
        self.fail("architecture mismatch")

    def test_32bit_header_cannot_claim_64bit_cpu(self):
        data = struct.pack("<IIIIIII", 0xFEEDFACE, verify.CPUS["x64"], 3, 2, 0, 0, 0)
        self.put(self.normal, data, 0o755)
        self.fail("CPU/header width mismatch")

    def test_fat_all_slices_must_match_not_merely_contain_target(self):
        for wide in (False, True):
            for endian in ("<", ">"):
                with self.subTest(wide=wide, endian=endian):
                    self.put(self.normal, fat(["x64", "x64"], wide=wide, endian=endian), 0o755)
                    self.assertEqual(verify.verify_bundle(self.app, "x64"), 16)
                    self.put(self.normal, fat(["x64", "arm64"], wide=wide, endian=endian), 0o755)
                    self.fail("expected only x64, got x64, arm64")
                    self.fail("expected only arm64", "arm64")

    def test_fat_table_header_mismatch(self):
        data = bytearray(fat(["x64"]))
        struct.pack_into(">I", data, 8, verify.CPUS["arm64"])
        self.put(self.normal, data, 0o755)
        self.fail("table/header architecture mismatch")

    def test_fat_invalid_bounds_overlap_duplicate_alignment_and_count(self):
        cases = []
        for offset, value in ((20, 999999), (16, 0), (24, 65), (4, 0), (4, 65)):
            data = bytearray(fat(["x64"]))
            struct.pack_into(">I", data, offset, value)
            cases.append(data)
        data = bytearray(fat(["x64", "x64"]))
        struct.pack_into(">I", data, 36, 48)
        cases.append(data)
        data = bytearray(fat(["x64", "x64"]))
        struct.pack_into(">I", data, 32, 3)
        cases.append(data)
        for data in cases:
            with self.subTest(data=data):
                self.put(self.normal, data, 0o755)
                self.fail("invalid fat Mach-O")

    def test_truncated_headers_and_bad_load_commands(self):
        cases = [b"", thin()[:4], thin()[:24], fat(["x64"])[:-1]]
        data = bytearray(thin())
        struct.pack_into("<II", data, 16, 1, 8)
        cases.extend([data, data + struct.pack("<II", 1, 4)])
        for data in cases:
            with self.subTest(data=data):
                self.put(self.normal, data, 0o755)
                self.fail("Mach-O")

    def test_shell_is_not_a_native_executable(self):
        self.put(self.normal, b"#!/bin/sh\nexit 0\n", 0o755)
        self.fail("not a Mach-O")

    def test_executable_directory_and_special_permissions(self):
        for mode in (0o644, 0o700, 0o777, 0o4755, 0o2755):
            with self.subTest(mode=mode):
                self.normal.chmod(mode)
                actual = stat.S_IMODE(self.normal.lstat().st_mode)
                self.assertEqual(actual & 0o777, mode & 0o777)
                if mode & 0o6000 and actual != mode:
                    # Some filesystems strip special bits even when chmod succeeds.
                    self.assertEqual(actual, mode & 0o777)
                    original_lstat = Path.lstat
                    normal = self.normal.resolve()

                    def lstat(path, *args, **kwargs):
                        result = original_lstat(path, *args, **kwargs)
                        if path == normal:
                            fields = list(result)
                            fields[0] = stat.S_IFREG | mode
                            return os.stat_result(fields)
                        return result

                    with mock.patch.object(Path, "lstat", lstat):
                        self.fail("permissions")
                else:
                    self.assertEqual(actual, mode)
                    self.fail("permissions")
        self.normal.chmod(0o755)
        self.normal.parent.chmod(0o744)
        self.fail("search permissions")
        self.normal.parent.chmod(0o755)

    def test_helper_mode_rejects_special_bits_independent_of_filesystem(self):
        for mode in (0o4755, 0o2755, 0o6755):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(verify.BundleError, "unsafe or unreadable permissions"):
                    verify._mode(self.normal, stat.S_IFREG | mode, executable=True)
        verify._mode(self.normal, stat.S_IFREG | 0o755, executable=True)

    def test_symlink_missing_cycle_absolute_and_escape(self):
        path = self.version / "Resources/link"
        for target, message in (("missing", "missing bundle path"), ("link", "cycle"),
                                ("/etc/passwd", "unsafe symlink"),
                                ("../../../../../../../../outside", "escapes bundle root")):
            with self.subTest(target=target):
                path.symlink_to(target)
                self.fail(message)
                path.unlink()

    def test_symlink_chain_with_dotdot_resolves_without_lexical_shortcuts(self):
        directory = self.version / "Resources/deeper"
        directory.mkdir()
        first = self.version / "Resources/first"
        second = self.version / "Resources/second"
        first.symlink_to("deeper")
        second.symlink_to("first/../Info.plist")
        self.assertEqual(verify.verify_bundle(self.app, "x64"), 16)
        first.unlink()
        first.symlink_to("second")
        self.fail("cycle|non-directory")

    def test_symlink_may_not_escape_app_even_inside_zip(self):
        (self.package / "outside").write_text("outside")
        (self.app / "external").symlink_to("../outside")
        self.fail("escapes bundle root")
        with self.assertRaisesRegex(verify.BundleError, "escapes bundle root"):
            verify.verify_zip(self.archive(), "x64")

    def test_zip_unsafe_paths_and_types(self):
        for name in ("../escape", "/escape", "chromix/../escape", "chromix/a\\b", "chromix//bad",
                     "chromix/C:bad", "other/file", "chromix/Chromium.app/CONTENTS/"):
            with self.subTest(name=name):
                mode = stat.S_IFDIR | 0o755 if name.endswith("/") else stat.S_IFREG | 0o644
                with self.assertRaises(verify.BundleError):
                    verify.verify_zip(self.archive(extra=[(name, mode, b"")]), "x64")
        with self.assertRaisesRegex(verify.BundleError, "unsupported ZIP file type"):
            verify.verify_zip(self.archive(extra=[("chromix/fifo", stat.S_IFIFO | 0o644, b"")]), "x64")

    def test_zip_rejects_symlink_ancestor_before_writing(self):
        extras = [("chromix/link", stat.S_IFLNK | 0o755, b"Chromium.app"),
                  ("chromix/link/overwrite", stat.S_IFREG | 0o644, b"bad")]
        with self.assertRaisesRegex(verify.BundleError, "non-directory ZIP ancestor"):
            verify.verify_zip(self.archive(extra=extras), "x64")

    def test_zip_permissions_preserved_and_validated(self):
        name = self.normal.relative_to(self.root).as_posix()
        with self.assertRaisesRegex(verify.BundleError, "executable/search permissions"):
            verify.verify_zip(self.archive({name: (stat.S_IFREG | 0o644, thin())}), "x64")
        with self.assertRaisesRegex(verify.BundleError, "permissions"):
            verify.verify_zip(self.archive({"chromix/chromix": (stat.S_IFREG | 0o644, b"script")}), "x64")

    def test_zip_external_symlinks_and_cycles(self):
        for target in (b"../../../outside", b"/etc/passwd", b"cycle", b"missing"):
            with self.subTest(target=target), self.assertRaises(verify.BundleError):
                verify.verify_zip(self.archive(extra=[("chromix/cycle", stat.S_IFLNK | 0o755, target)]), "x64")

    def test_optional_codesign_only_on_darwin_and_failure_distinguished(self):
        with mock.patch.object(verify.sys, "platform", "linux"), self.assertRaisesRegex(
                verify.BundleError, "requires Darwin"):
            verify.verify_bundle(self.app, "x64", codesign_check=True)
        with mock.patch.object(verify.sys, "platform", "darwin"), mock.patch.object(
                verify.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "unsigned")) as run:
            with self.assertRaisesRegex(verify.BundleError, "codesign verification failed.*unsigned"):
                verify.verify_bundle(self.app, "x64", codesign_check=True)
            self.assertEqual(run.call_args.args[0][:6], ["codesign", "--verify", "--deep", "--strict", "-vv", str(self.app)])

    def test_cli_beforecase_and_success(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(verify.main(["--app", str(self.app), "--arch", "x64"]), 0)
            self.normal.unlink()
            self.assertEqual(verify.main(["--zip", str(self.archive()), "--arch", "x64"]), 1)
        self.assertIn("16 Mach-O files, only x64", stdout.getvalue())
        self.assertIn("missing required normal Helper executable", stderr.getvalue())
        self.assertNotIn("codesign", stderr.getvalue())

    @unittest.skipUnless(os.name == "posix" and shutil.which("zip") and shutil.which("shasum"),
                         "POSIX packaging tools required")
    def test_packager_preflight_and_post_zip_both_architectures(self):
        fonts = self.root / "fonts"
        fonts.mkdir()
        for name in ("font.ttf", "fonts.conf.template", "NOTICE", "SOURCE.md"):
            (fonts / name).write_text("fixture")
        license_path = self.root / "LICENSE"
        license_path.write_text("fixture license")
        env = {**os.environ, "CHROMIX_FONTS_DIR": str(fonts),
               "CHROMIX_CHROMIUM_LICENSE": str(license_path)}
        dest = self.root / "dist"
        script = REPO / "build/macos/package-macos.sh"
        for arch in ("x64", "arm64"):
            self.layout(arch)
            result = subprocess.run(["bash", str(script), str(self.app), str(dest), arch],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count("macOS bundle verified:"), 2)
            archive = dest / f"chromix-mac-{arch}.zip"
            self.assertIn(hashlib.sha256(archive.read_bytes()).hexdigest(), (dest / "SHA256SUMS").read_text())
        wrapper_dir = self.root / "bin"
        wrapper_dir.mkdir()
        name = self.normal.relative_to(self.root).as_posix()
        bad_archive = self.archive({name: (stat.S_IFREG | 0o644, thin("arm64"))})
        self.put(wrapper_dir / "zip", b'#!/bin/sh\ncp "$BAD_ARCHIVE" "$5"\n', 0o755)
        corrupt_dest = self.root / "corrupt-dist"
        result = subprocess.run(["bash", str(script), str(self.app), str(corrupt_dest), "arm64"],
                                capture_output=True, text=True,
                                env={**env, "PATH": str(wrapper_dir) + os.pathsep + env["PATH"],
                                     "BAD_ARCHIVE": str(bad_archive)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("macOS bundle verified:"), 1)
        self.assertIn("executable/search permissions", result.stderr)
        self.assertFalse((corrupt_dest / "SHA256SUMS").exists())
        self.normal.unlink()
        failed_dest = self.root / "failed-dist"
        result = subprocess.run(["bash", str(script), str(self.app), str(failed_dest), "arm64"],
                                capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing required normal Helper executable", result.stderr)
        self.assertFalse(failed_dest.exists())


if __name__ == "__main__":
    unittest.main()
