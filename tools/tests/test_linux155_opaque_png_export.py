"""PNG wire normalization on pinned 155 sources; no browser acceptance claim.

Default C++ coverage extracts real conditions and SkColorInfo methods but uses
branch-only shims: it does not execute Skia conversion, ICC or PNG encoding.
CHROMIX_LINUX155_PNG_SKIA_ROOT plus CHROMIX_LINUX155_PNG_SKIA_FLAGS (a JSON array
of compiler/linker arguments) enables the separate real pinned Skia API fixture.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name('fixtures')
PATCH = ROOT / 'patches/chromium155-linux/0227-opaque-png-export-consistency.patch'
TARGET = 'third_party/blink/renderer/core/html/canvas/canvas_async_blob_creator.cc'
BEFORE = 'b544a82ed5ab6f125d68604312f214135200e0ce89c0c46f61dee9cc9d6a199d'
AFTER = '7d78a117324c383003bcda13cd61161cc336d8f498e246f621019ed2f7406945'
FIXTURE_HASHES = {
    'sources': '5febb57490ec56c67070bc6aff4daf90a41f94073e09e910233ee719c57efbef',
    'payloads': 'bbecd0b71b464a48743c94e117a2d7035a41f1870eb8a89405bc98ec724071a4',
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def evidence(name):
    raw = (FIXTURES / f'linux155_opaque_png_{name}.json.gz').read_bytes()
    assert digest(raw) == FIXTURE_HASHES[name]
    return json.loads(gzip.decompress(raw))


def function(source, signature):
    start = source.index(signature)
    first = source.index('{', start)
    depth, end = 1, first + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def condition(source):
    start = source.index('    if (', source.index('// https://crbug.com/557350372'))
    end = source.index(' {\n      SkBitmap bitmap;', start)
    return source[start + len('    if ('):end - 1]


def apply(directory, patch, *, reverse=False, offsets=False):
    program = shutil.which('gpatch') or shutil.which('patch')
    if not program:
        pytest.skip('GNU patch required')
    result = subprocess.run([program, '-p1', '--fuzz=0', '--batch', '--binary', '--get=0',
        '--no-backup-if-mismatch', '--reject-file=-', '--reverse' if reverse else '--forward',
        '-i', str(patch)], cwd=directory, capture_output=True, text=True, timeout=30,
        env={**os.environ, 'LC_ALL': 'C', 'PATCH_GET': '0'})
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not re.search(r'fuzz|FAILED|Reversed patch', output, re.I), output
    assert offsets or 'offset' not in output, output


@pytest.fixture(scope='module')
def sources(tmp_path_factory):
    data = evidence('sources')
    assert data['target'] == TARGET
    assert data['chromium_version'] == '155.0.8059.39'
    assert data['skia_revision'] == '29ed1e87a0a50f3d8347e988842d9d59e7573efa'
    entries = [data['pristine'], data['before'], data['image_encoder_utils'], *data['skia'].values()]
    for entry in entries:
        raw = entry['text'].encode()
        assert digest(raw) == entry['sha256']
        assert hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest() == entry['git_blob']
    assert data['before']['sha256'] == BEFORE
    assert data['after_sha256'] == AFTER
    assert digest(PATCH.read_bytes()) == data['patch_sha256']
    predecessor = ROOT / data['predecessor']['path']
    assert digest(predecessor.read_bytes()) == data['predecessor']['sha256']
    directory = tmp_path_factory.mktemp('opaque-png-source')
    path = directory / TARGET
    path.parent.mkdir(parents=True)
    path.write_text(data['pristine']['text'])
    apply(directory, predecessor, offsets=True)
    assert path.read_text() == data['before']['text']
    apply(directory, PATCH)
    after = path.read_text()
    assert digest(path.read_bytes()) == AFTER
    apply(directory, PATCH, reverse=True)
    assert digest(path.read_bytes()) == BEFORE
    return data, after


def test_exact_full_source_roundtrip_and_only_conversion_condition_changes(sources):
    data, after = sources
    before = data['before']['text']
    old, new = condition(before), condition(after)
    comment = "    // Normalize opaque PNGs to match the synchronous export's wire format.\n"
    assert after.replace(comment, '', 1).replace(new, old, 1) == before
    assert after.index('mime_type_ = ImageEncoderUtils::ToEncodingMimeType(options->type());') < after.index(new)
    block = after[after.index('      SkBitmap bitmap;', after.index(new)):
                  after.index('    // If image is lazy decoded')]
    assert block == before[before.index('      SkBitmap bitmap;', before.index(old)):
                           before.index('    // If image is lazy decoded')]
    assert 'bitmap.tryAllocPixels(SkImageInfo::Make(skia_image_->dimensions(),\n' in block
    assert 'target_color_info)) &&\n          skia_image_->readPixels(bitmap.pixmap(), 0, 0)' in block
    assert 'bitmap.setImmutable();\n        skia_image_ = bitmap.asImage();' in block
    assert 'SkAlphaType alpha_type = kUnpremul_SkAlphaType;' in data['image_encoder_utils']['text']
    assert 'color_info.refColorSpace()' in data['image_encoder_utils']['text']
    patch = PATCH.read_text()
    assert patch.count('diff --git ') == 1
    additions = '\n'.join(line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))
    assert not any(token in additions for token in ('UxrConfig', 'seed', 'noise', 'setAlphaType', 'makeColorSpace'))


def render_cases(sources, name):
    data, after = sources
    return (FIXTURES / name).read_text().replace('@BEFORE_CONDITION@', condition(data['before']['text'])).replace(
        '@AFTER_CONDITION@', condition(after))


def compile_and_run(tmp_path, source, flags=()):
    compiler = os.environ.get('CXX') or shutil.which('clang++') or shutil.which('g++')
    if not compiler:
        pytest.skip('C++20 compiler required')
    unit = tmp_path / 'opaque_png.cc'
    binary = tmp_path / ('opaque_png.exe' if os.name == 'nt' else 'opaque_png')
    unit.write_text(source)
    result = subprocess.run([compiler, '-std=c++20', '-O1', '-Wall', '-Wextra', '-Werror',
        str(unit), *flags, '-UNDEBUG', '-o', str(binary)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_extracted_condition_all_alpha_color_types_spaces_branch_shims_only(sources, tmp_path):
    data, _ = sources
    skia = {name: value['text'] for name, value in data['skia'].items()}
    alpha = re.search(r'enum SkAlphaType : int \{.*?\n\};', skia['include/core/SkAlphaType.h'], re.S)[0]
    colors = skia['include/core/SkColorType.h']
    start = re.search(r'^enum SkColorType\s*:\s*int\s*\{', colors, re.M).start()
    colors = colors[start:colors.index('#if SK_PMCOLOR_BYTE_ORDER')].rstrip() + '\n};'
    methods = skia['src/core/SkImageInfo.cpp']
    extracted = '\n'.join(function(methods, signature) for signature in (
        'SkColorInfo::SkColorInfo(SkColorType', 'bool SkColorInfo::operator==',
        'bool SkColorInfo::operator!=', 'SkColorInfo SkColorInfo::makeAlphaType'))
    shim = (FIXTURES / 'linux155_opaque_png_branch_shim.h').read_text()
    shim = shim.replace('#pragma once\n', '').replace('@ALPHA_ENUM@', alpha).replace('@COLOR_ENUM@', colors)
    shim = shim.replace('@SKIA_COLOR_INFO_METHODS@', extracted)
    output = compile_and_run(tmp_path, shim + render_cases(sources, 'linux155_opaque_png_branch_cases.cc'))
    types = len(re.findall(r'^\s+k\w+_SkColorType\s*,', colors, re.M))
    assert types > 20
    assert output == f'PASS branch-only cases={3 * 4 * types * types * 25} changed={types * 7}\n'


def test_pinned_skia_encoder_uses_alpha_type_not_pixel_opacity(sources):
    data, _ = sources
    base = data['skia']['src/encode/SkPngEncoderBase.cpp']['text']
    rust = data['skia']['src/encode/SkPngRustEncoderImpl.cpp']['text']
    assert 'if (srcAT == kOpaque_SkAlphaType)' in base
    assert re.search(r'kRGB_888x_SkColorType,\s+kOpaque_SkAlphaType', base)
    assert re.search(r'kRGBA_8888_SkColorType,\s+kUnpremul_SkAlphaType', base)
    assert 'if (maybeDstRowInfo->isOpaque()) {\n' in rust
    assert 'rustEncoderColorType = rust_png::ColorType::Rgb;' in rust
    assert 'extraRowTransform = kRgba8ToRgb8_ExtraRowTransform;' in rust


def test_optional_real_pinned_skia_conversion_and_png_bytes(sources, tmp_path):
    root = os.environ.get('CHROMIX_LINUX155_PNG_SKIA_ROOT')
    flags = os.environ.get('CHROMIX_LINUX155_PNG_SKIA_FLAGS')
    if not root and not flags:
        pytest.skip('matching pinned Skia headers and real Skia/Rust PNG link flags required; shims are not native evidence')
    assert root and flags, 'both Skia root and JSON compiler/link flags are required'
    flags = json.loads(flags)
    assert isinstance(flags, list) and all(isinstance(flag, str) for flag in flags)
    data, _ = sources
    for path, entry in data['skia'].items():
        assert digest((Path(root) / path).read_bytes()) == entry['sha256']
    output = compile_and_run(tmp_path, render_cases(sources, 'linux155_opaque_png_skia_api.cc'),
                             ['-I', root, *flags])
    assert output == 'PASS real-Skia cases=12\n'


def png_chunks(raw):
    assert raw[:8] == b'\x89PNG\r\n\x1a\n'
    offset, chunks = 8, []
    while offset < len(raw):
        length = struct.unpack_from('>I', raw, offset)[0]
        tag = raw[offset + 4:offset + 8]
        body = raw[offset + 8:offset + 8 + length]
        assert zlib.crc32(tag + body) == struct.unpack_from('>I', raw, offset + 8 + length)[0]
        chunks.append((tag, body))
        offset += 12 + length
    assert offset == len(raw) and chunks[-1] == (b'IEND', b'')
    return chunks


@pytest.mark.parametrize('index,lengths', [(0, (152, 141)), (1, (459, 448))])
def test_original_complete_payloads_equal_pixels_still_fail_unchanged_wire_gate(index, lengths):
    pytest.importorskip('PIL.Image')
    spec = importlib.util.spec_from_file_location('opaque_png_canvas_chain', ROOT / 'sdk/python/chromix/_canvas_chain.py')
    chain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chain)
    payloads = evidence('payloads')
    assert payloads['probe_sha256'] == digest((ROOT / 'sdk/python/chromix/canvas_chain_probe.js').read_bytes())
    sample = payloads['samples'][index]
    item = sample['export']
    assert item['repeat'] is True and item['urlRepeat'] is True
    assert item['urlBlobMatches'] is False and item['urlMatches'] is False
    assert item['bytes'] == item['repeatBytes']
    assert item['dataURL'] == item['repeatDataURL']
    url = item['dataURL'].removeprefix('data:image/png;base64,')
    blob_raw, url_raw = (base64.b64decode(value, validate=True) for value in (item['bytes'], url))
    assert (len(blob_raw), len(url_raw)) == lengths and blob_raw != url_raw
    blob_chunks, url_chunks = png_chunks(blob_raw), png_chunks(url_raw)
    assert blob_chunks[0][1][9] == 2 and url_chunks[0][1][9] == 6
    blob_icc = [body for tag, body in blob_chunks if tag == b'iCCP']
    url_icc = [body for tag, body in url_chunks if tag == b'iCCP']
    assert blob_icc == url_icc and bool(blob_icc) == (sample['colorSpace'] == 'display-p3')
    blob = chain.decode(item['bytes'], 'image/png')
    decoded_url = chain.decode(url, 'image/png')
    assert blob['rgba'] == decoded_url['rgba'] == sample['reference'] == item['decoded']
    assert blob['srgb'] == decoded_url['srgb']
    assert blob['rgba'][3::4] == [255] * (sample['width'] * sample['height'])
    assert chain.export_evidence_errors(item, 'html', raw_payloads=True) == [
        'DataURL repeat/Blob mismatch', 'DataURL/Blob payload mismatch']


def test_real_skia_fixture_has_conversion_and_low_alpha_controls(sources):
    source = render_cases(sources, 'linux155_opaque_png_skia_api.cc')
    assert '@' not in source
    assert 'image->readPixels(asynchronous.pixmap(), 0, 0)' in source
    assert 'SkPngRustEncoder::Encode' in source and 'SkPngRustEncoder::Make' in source
    assert 'SkCodec::MakeFromData' in source
    assert 'std::array<uint8_t, 4>{0, 1, 64, 128}' in source
    assert 'old_png != sync_png && new_png == sync_png' in source
    assert 'assert(old_png == new_png);' in source
