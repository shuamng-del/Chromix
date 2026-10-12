#!/usr/bin/env python3
"""Verify Chromium macOS bundle structure and exact target slices without native tools."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

CPUS = {"x64": 0x01000007, "arm64": 0x0100000C}
THIN = {b"\xce\xfa\xed\xfe": ("<", 28), b"\xfe\xed\xfa\xce": (">", 28),
        b"\xcf\xfa\xed\xfe": ("<", 32), b"\xfe\xed\xfa\xcf": (">", 32)}
FAT = {b"\xca\xfe\xba\xbe": (">", False), b"\xbe\xba\xfe\xca": ("<", False),
       b"\xca\xfe\xba\xbf": (">", True), b"\xbf\xba\xfe\xca": ("<", True)}
MAGICS = THIN.keys() | FAT.keys()


class BundleError(ValueError):
    pass


def _name(name: str) -> None:
    if not name or name in (".", "..") or any(c in name for c in "\\/:") or any(
            ord(c) < 32 or ord(c) == 127 for c in name):
        raise BundleError(f"unsafe path component: {name!r}")


def _mode(path, mode: int, *, executable=False) -> None:
    if mode & 0o6002 or mode & 0o444 != 0o444:
        raise BundleError(f"unsafe or unreadable permissions: {path}: {mode:o}")
    if executable and mode & 0o111 != 0o111:
        raise BundleError(f"missing executable/search permissions: {path}: {mode:o}")


def macho_slices(path: Path) -> list[tuple[int, int]]:
    """Return (CPU type, file type), validating every thin header in a fat file."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        def read(offset, length):
            if offset < 0 or length < 0 or offset + length > size:
                raise BundleError(f"truncated Mach-O: {path}")
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise BundleError(f"truncated Mach-O: {path}")
            return data

        def thin(offset, length):
            magic = read(offset, 4)
            if magic not in THIN:
                raise BundleError(f"invalid thin Mach-O slice: {path}")
            endian, header_size = THIN[magic]
            if length < header_size:
                raise BundleError(f"truncated Mach-O header: {path}")
            header = read(offset, header_size)
            cpu, subtype, kind, count, commands_size = struct.unpack_from(endian + "IIIII", header, 4)
            if bool(cpu & 0x01000000) != (header_size == 32):
                raise BundleError(f"Mach-O CPU/header width mismatch: {path}")
            if commands_size > length - header_size or count > commands_size // 8:
                raise BundleError(f"invalid Mach-O load command bounds: {path}")
            cursor, end = offset + header_size, offset + header_size + commands_size
            for _ in range(count):
                if cursor + 8 > end:
                    raise BundleError(f"truncated Mach-O load command: {path}")
                _, command_size = struct.unpack(endian + "II", read(cursor, 8))
                if command_size < 8 or command_size % 4 or cursor + command_size > end:
                    raise BundleError(f"invalid Mach-O load command: {path}")
                cursor += command_size
            if cursor != end:
                raise BundleError(f"inconsistent Mach-O load command size: {path}")
            return cpu, subtype, kind

        magic = read(0, 4)
        if magic in THIN:
            cpu, _, kind = thin(0, size)
            return [(cpu, kind)]
        if magic not in FAT:
            raise BundleError(f"not a Mach-O executable: {path}")
        endian, wide = FAT[magic]
        count = struct.unpack(endian + "I", read(4, 4))[0]
        width = 32 if wide else 20
        table_end = 8 + width * count
        if not 1 <= count <= 64 or table_end > size:
            raise BundleError(f"invalid fat Mach-O table: {path}")
        ranges, identities, result = [], set(), []
        for index in range(count):
            entry = read(8 + index * width, width)
            if wide:
                cpu, subtype, offset, length, align, reserved = struct.unpack(endian + "IIQQII", entry)
                if reserved:
                    raise BundleError(f"invalid fat Mach-O reserved field: {path}")
            else:
                cpu, subtype, offset, length, align = struct.unpack(endian + "IIIII", entry)
            if (align > 63 or offset % (1 << align) or offset < table_end or length < 28
                    or offset + length > size or (cpu, subtype) in identities
                    or any(offset < end and start < offset + length for start, end in ranges)):
                raise BundleError(f"invalid fat Mach-O slice bounds/identity: {path}")
            actual_cpu, actual_subtype, kind = thin(offset, length)
            if (cpu, subtype) != (actual_cpu, actual_subtype):
                raise BundleError(f"fat Mach-O table/header architecture mismatch: {path}")
            identities.add((cpu, subtype))
            ranges.append((offset, offset + length))
            result.append((cpu, kind))
        return result


class Bundle:
    def __init__(self, app: Path):
        app = Path(app).absolute()
        if app.is_symlink() or not app.is_dir() or app.name != "Chromium.app":
            raise BundleError(f"expected real Chromium.app directory: {app}")
        self.root = app.resolve(strict=True)
        self.entries = self.inventory(self.root)

    @staticmethod
    def inventory(root):
        def walk_error(error):
            raise error

        entries = {(): root.lstat()}
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            for name in dirs + files:
                _name(name)
                path = Path(directory) / name
                entries[path.relative_to(root).parts] = path.lstat()
        return entries

    def resolve(self, parts) -> Path:
        pending, resolved, seen = list(parts), [], set()
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise BundleError(f"symlink escapes bundle root: {'/'.join(parts)}")
                resolved.pop()
                continue
            _name(part)
            resolved.append(part)
            key = tuple(resolved)
            info = self.entries.get(key)
            if info is None:
                raise BundleError(f"missing bundle path: {self.root.joinpath(*key)}")
            if stat.S_ISLNK(info.st_mode):
                state = (key, tuple(pending))
                if state in seen or len(seen) >= 128:
                    raise BundleError(f"symlink cycle: {self.root.joinpath(*key)}")
                seen.add(state)
                target = os.readlink(self.root.joinpath(*key))
                if not target or target.startswith("/") or "\\" in target:
                    raise BundleError(f"unsafe symlink target: {self.root.joinpath(*key)} -> {target}")
                resolved.pop()
                pending = target.split("/") + pending
            elif pending and not stat.S_ISDIR(info.st_mode):
                raise BundleError(f"non-directory path ancestor: {self.root.joinpath(*key)}")
        return self.root.joinpath(*resolved)

    def executable(self, base, plist_parts, arch, kind):
        plist = self.resolve(plist_parts)
        try:
            data = plistlib.loads(plist.read_bytes())
        except (ValueError, TypeError, plistlib.InvalidFileException) as error:
            raise BundleError(f"invalid Info.plist: {plist}: {error}") from error
        name = data.get("CFBundleExecutable") if isinstance(data, dict) else None
        if not isinstance(name, str):
            raise BundleError(f"missing CFBundleExecutable: {plist}")
        _name(name)
        executable = self.resolve((*base, name))
        self.check_macho(executable, arch, kind)

    def check_macho(self, path, arch, kind=None):
        if not path.is_file():
            raise BundleError(f"missing executable file: {path}")
        _mode(path, path.stat().st_mode, executable=True)
        slices = macho_slices(path)
        if any(cpu != CPUS[arch] for cpu, _ in slices):
            actual = ", ".join(next((name for name, value in CPUS.items() if value == cpu),
                                    hex(cpu)) for cpu, _ in slices)
            raise BundleError(f"Mach-O architecture mismatch: {path}: expected only {arch}, got {actual}")
        if kind is not None and any(filetype != kind for _, filetype in slices):
            raise BundleError(f"wrong Mach-O file type: {path}: expected {kind}")

    def verify(self, arch):
        framework = ("Contents", "Frameworks", "Chromium Framework.framework")
        helper = (*framework, "Helpers", "Chromium Helper.app", "Contents", "MacOS", "Chromium Helper")
        try:
            normal = self.resolve(helper)
            if not normal.is_file():
                raise BundleError("not a regular file")
        except BundleError as error:
            raise BundleError(f"missing required normal Helper executable: {self.root.joinpath(*helper)}: {error}") from error

        for parts, info in self.entries.items():
            path = self.root.joinpath(*parts)
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                self.resolve(parts)
            elif stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                _mode(path, mode, executable=stat.S_ISDIR(mode))
            else:
                raise BundleError(f"unsupported bundle file type: {path}")

        self.check_macho(normal, arch, 2)
        frameworks = 0
        for parts, info in self.entries.items():
            if not (stat.S_ISDIR(info.st_mode) or
                    stat.S_ISLNK(info.st_mode) and self.resolve(parts).is_dir()):
                continue
            name = parts[-1] if parts else self.root.name
            if name.endswith(".app"):
                self.executable((*parts, "Contents", "MacOS"), (*parts, "Contents", "Info.plist"), arch, 2)
            elif name.endswith(".framework"):
                frameworks += 1
                self.executable(parts, (*parts, "Resources", "Info.plist"), arch, 6)
                versions = (*parts, "Versions")
                for version, entry in self.entries.items():
                    if version[:-1] == versions and stat.S_ISDIR(entry.st_mode):
                        self.executable(version, (*version, "Resources", "Info.plist"), arch, 6)
                        if parts == framework:
                            self.check_macho(self.resolve((*version, "Helpers", "Chromium Helper.app",
                                                          "Contents", "MacOS", "Chromium Helper")), arch, 2)
        if not frameworks:
            raise BundleError(f"missing Chromium Framework: {self.root}")
        count = 0
        for parts, info in self.entries.items():
            if stat.S_ISREG(info.st_mode):
                path = self.root.joinpath(*parts)
                with path.open("rb") as stream:
                    magic = stream.read(4)
                if magic in MAGICS:
                    self.check_macho(path, arch)
                    count += 1
                elif path.suffix == ".dylib":
                    raise BundleError(f"not a Mach-O dylib: {path}")
        return count


def verify_bundle(app: Path, arch: str, *, codesign_check=False) -> int:
    if arch not in CPUS:
        raise BundleError(f"unsupported architecture: {arch}")
    count = Bundle(app).verify(arch)
    if codesign_check:
        if sys.platform != "darwin":
            raise BundleError("--codesign-check requires Darwin")
        result = subprocess.run(["codesign", "--verify", "--deep", "--strict", "-vv", str(app)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise BundleError(f"codesign verification failed: {result.stdout}{result.stderr}")
    return count


def verify_zip(archive: Path, arch: str, *, codesign_check=False) -> int:
    """Extract only confined entries into a fresh directory, preserving UNIX modes."""
    with zipfile.ZipFile(archive) as source, tempfile.TemporaryDirectory(prefix="chromix-bundle-check-") as temp:
        root = Path(temp)
        entries, links, folded = {}, {}, set()
        for entry in source.infolist():
            if entry.orig_filename != entry.filename:
                raise BundleError(f"unsafe ZIP path: {entry.orig_filename!r}")
            raw = entry.filename[:-1] if entry.is_dir() else entry.filename
            parts = tuple(raw.split("/"))
            for part in parts:
                _name(part)
            if parts[0] != "chromix" or raw in entries or raw.casefold() in folded:
                raise BundleError(f"unsafe/duplicate ZIP path: {entry.filename}")
            folded.add(raw.casefold())
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if entry.create_system != 3 or kind not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK):
                raise BundleError(f"missing UNIX mode or unsupported ZIP file type: {raw}")
            if entry.is_dir() != stat.S_ISDIR(mode):
                raise BundleError(f"inconsistent ZIP directory mode: {raw}")
            if not stat.S_ISLNK(mode):
                _mode(raw, mode, executable=stat.S_ISDIR(mode))
            entries[raw] = (entry, mode, parts)
            if stat.S_ISLNK(mode):
                if entry.file_size > 4096:
                    raise BundleError(f"oversized ZIP symlink: {raw}")
                links[raw] = source.read(entry).decode("utf-8")
        for raw, (_, mode, parts) in entries.items():
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if parent in entries and not stat.S_ISDIR(entries[parent][1]):
                    raise BundleError(f"non-directory ZIP ancestor: {raw}")
            if raw in links:
                target = links[raw]
                if not target or target.startswith("/") or "\\" in target or "\0" in target:
                    raise BundleError(f"unsafe ZIP symlink: {raw}")
                resolved = list(parts[:-1])
                for component in target.split("/"):
                    if component in ("", "."):
                        continue
                    if component == "..":
                        if len(resolved) <= 1:
                            raise BundleError(f"ZIP symlink escapes root: {raw}")
                        resolved.pop()
                    else:
                        _name(component)
                        resolved.append(component)
        for raw, (entry, mode, _) in entries.items():
            path = root / raw
            path.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISDIR(mode):
                path.mkdir(exist_ok=True)
            elif stat.S_ISREG(mode):
                with source.open(entry) as incoming, path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                path.chmod(stat.S_IMODE(mode))
        for raw, target in links.items():
            (root / raw).symlink_to(target)
        for raw, (_, mode, _) in entries.items():
            if stat.S_ISDIR(mode):
                (root / raw).chmod(stat.S_IMODE(mode))
        app = root / "chromix/Chromium.app"
        count = verify_bundle(app, arch, codesign_check=codesign_check)
        package = Bundle.__new__(Bundle)
        package.root = root.resolve()
        package.entries = Bundle.inventory(root)
        for parts, info in package.entries.items():
            if not parts:
                continue
            if stat.S_ISLNK(info.st_mode):
                package.resolve(parts)
            else:
                _mode(root.joinpath(*parts), info.st_mode, executable=stat.S_ISDIR(info.st_mode))
        launcher = root / "chromix/chromix"
        if not launcher.is_file() or launcher.is_symlink():
            raise BundleError(f"missing regular package launcher: {launcher}")
        _mode(launcher, launcher.stat().st_mode, executable=True)
        return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", type=Path)
    source.add_argument("--zip", type=Path)
    parser.add_argument("--arch", required=True, choices=tuple(CPUS))
    parser.add_argument("--codesign-check", action="store_true")
    args = parser.parse_args(argv)
    try:
        verify = verify_zip if args.zip else verify_bundle
        count = verify(args.zip or args.app, args.arch, codesign_check=args.codesign_check)
        print(f"macOS bundle verified: {args.zip or args.app}: {count} Mach-O files, only {args.arch}")
        return 0
    except (BundleError, OSError, RuntimeError, UnicodeError, zipfile.BadZipFile,
            subprocess.SubprocessError) as error:
        print(f"macOS bundle verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
