"""Exercise the packaging scripts with miniature native bundle layouts."""
import hashlib
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import verify_macos_bundle

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "sdk/python"))
from chromix._binary import _extract_zip


@unittest.skipUnless(shutil.which("zip") and os.name == "posix", "POSIX zip required")
class PosixPackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chromix package ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "src"
        self.out = self.src / "out/Chromix"
        self.out.mkdir(parents=True)
        (self.src / "LICENSE").write_text("Chromium fixture license")
        self.fonts = self.root / "fonts"
        self.fonts.mkdir()
        for name in ("font.ttf", "font.ttc", "fonts.conf.template", "NOTICE", "SOURCE.md"):
            (self.fonts / name).write_text("fixture")
        self.env = {**os.environ, "CHROMIX_FONTS_DIR": str(self.fonts)}

    def run_package(self, platform, source, arch, success=True, dest=None):
        dest = dest or self.root / (platform + "-" + arch)
        result = subprocess.run(
            ["bash", str(REPO / f"build/{platform}/package-{platform}.sh"),
             str(source), str(dest), arch], env=self.env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return dest, result

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\nprintf "fixture:%s\\n" "$*"\n')
        path.chmod(0o755)

    def verify_bundle(self, dest, asset, binary, *, mac_arch=None):
        archive = dest / asset
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertIn(f"{expected}  {asset}", (dest / "SHA256SUMS").read_text())
        extracted = dest / "extracted"
        extracted.mkdir()
        _extract_zip(archive, extracted)
        bundle = extracted / "chromix"
        self.assertEqual((bundle / "LICENSE.chromium").read_text(), "Chromium fixture license")
        self.assertTrue((bundle / "LICENSE.chromix").is_file())
        self.assertTrue((bundle / "fonts/font.ttc").is_file())
        self.assertTrue((bundle / binary).stat().st_mode & stat.S_IXUSR)
        if mac_arch is None:
            result = subprocess.run([str(bundle / "chromix"), "--version"],
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "fixture:--version")
        else:
            self.assertEqual(verify_macos_bundle.verify_bundle(bundle / "Chromium.app", mac_arch), 4)
            self.assertEqual(verify_macos_bundle.verify_zip(archive, mac_arch), 4)
            self.assertTrue((bundle / "chromix").stat().st_mode & stat.S_IXUSR)
            self.assertIn('exec "$HERE/Chromium.app/Contents/MacOS/Chromium" "$@"',
                          (bundle / "chromix").read_text())
        return bundle

    def linux_layout(self):
        for name in ("chrome", "chrome_crashpad_handler", "chrome_sandbox"):
            self.executable(self.out / name)
        for name in ("icudtl.dat", "resources.pak", "v8_context_snapshot.bin"):
            (self.out / name).write_text("fixture")
        (self.out / "locales").mkdir()
        (self.out / "locales/en-US.pak").write_text("fixture")

    def mac_layout(self, arch="x64"):
        cpu = {"x64": 0x01000007, "arm64": 0x0100000C}[arch]

        def macho(path, kind=2):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 3, kind, 0, 0, 0, 0))
            path.chmod(0o755)

        def plist(path, executable):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({"CFBundleExecutable": executable}))
            path.chmod(0o644)

        app = self.out / "Chromium.app"
        macho(app / "Contents/MacOS/Chromium")
        plist(app / "Contents/Info.plist", "Chromium")
        framework = app / "Contents/Frameworks/Chromium Framework.framework"
        version = framework / "Versions/1"
        macho(version / "Chromium Framework", kind=6)
        plist(version / "Resources/Info.plist", "Chromium Framework")
        macho(version / "Helpers/helper")
        helper = version / "Helpers/Chromium Helper.app"
        macho(helper / "Contents/MacOS/Chromium Helper")
        plist(helper / "Contents/Info.plist", "Chromium Helper")
        for path, target in ((framework / "Versions/Current", "1"),
                             (framework / "Helpers", "Versions/Current/Helpers"),
                             (framework / "Resources", "Versions/Current/Resources"),
                             (framework / "Chromium Framework", "Versions/Current/Chromium Framework")):
            if not path.is_symlink():
                path.symlink_to(target)
        return app

    def test_linux_both_architectures_and_missing_runtime(self):
        self.linux_layout()
        for arch in ("x64", "arm64"):
            with self.subTest(arch=arch):
                dest, _ = self.run_package("linux", self.out, arch)
                bundle = self.verify_bundle(dest, f"chromix-linux-{arch}.zip", "chrome")
                self.assertTrue((bundle / "chrome-sandbox").stat().st_mode & stat.S_IXUSR)
        (self.out / "icudtl.dat").unlink()
        _, result = self.run_package("linux", self.out, "x64", success=False)
        self.assertIn("required runtime file", result.stderr)

    def test_macos_both_architectures_symlinks_and_default_license(self):
        for arch in ("x86_64", "arm64"):
            with self.subTest(arch=arch):
                normalized = "x64" if arch == "x86_64" else arch
                app = self.mac_layout(normalized)
                dest, result = self.run_package("macos", app, arch)
                self.assertEqual(result.stdout.count("macOS bundle verified:"), 2)
                bundle = self.verify_bundle(dest, f"chromix-mac-{normalized}.zip",
                                            "Chromium.app/Contents/MacOS/Chromium", mac_arch=normalized)
                helpers = bundle / "Chromium.app/Contents/Frameworks/Chromium Framework.framework/Helpers"
                self.assertTrue(helpers.is_symlink())
                self.assertTrue((helpers / "helper").stat().st_mode & stat.S_IXUSR)
        app = self.mac_layout("x64")
        (self.src / "LICENSE").unlink()
        _, result = self.run_package("macos", app, "x64", success=False)
        self.assertIn("Chromium license is missing", result.stderr)

    def test_linux_rejects_empty_locales(self):
        self.linux_layout()
        (self.out / "locales/en-US.pak").unlink()
        _, result = self.run_package("linux", self.out, "x64", success=False)
        self.assertIn("no locale resource packs", result.stderr)

    def test_shared_destination_keeps_all_checksums(self):
        self.linux_layout()
        app = self.mac_layout()
        dest = self.root / "dist"
        assets = []
        for platform, source in (("linux", self.out), ("macos", app)):
            for arch in ("x64", "arm64"):
                if platform == "macos":
                    source = self.mac_layout(arch)
                self.run_package(platform, source, arch, dest=dest)
                prefix = "mac" if platform == "macos" else platform
                assets.append(f"chromix-{prefix}-{arch}.zip")
        lines = (dest / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(lines), 4)
        for asset in assets:
            digest = hashlib.sha256((dest / asset).read_bytes()).hexdigest()
            self.assertIn(f"{digest}  {asset}", lines)

    def test_macos_rejects_unknown_architecture(self):
        _, result = self.run_package("macos", self.mac_layout(), "ppc", success=False)
        self.assertIn("unsupported macOS package architecture", result.stderr)


if __name__ == "__main__":
    unittest.main()
