// Real pinned Skia API test; requires matching Skia/Rust PNG link dependencies.
#include "include/codec/SkCodec.h"
#include "include/core/SkBitmap.h"
#include "include/core/SkColorSpace.h"
#include "include/core/SkData.h"
#include "include/core/SkImage.h"
#include "include/core/SkPixmap.h"
#include "include/core/SkStream.h"
#include "include/encode/SkEncoder.h"
#include "include/encode/SkPngRustEncoder.h"

#include <array>
#include <cassert>
#include <cstring>
#include <iostream>
#include <utility>
#include <vector>

namespace {
enum Mime { kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp };
bool Before(Mime mime_type_, const SkColorInfo& target_color_info,
            const SkImage* skia_image_) {
  static_cast<void>(mime_type_);
  return @BEFORE_CONDITION@;
}
bool After(Mime mime_type_, const SkColorInfo& target_color_info,
           const SkImage* skia_image_) {
  return @AFTER_CONDITION@;
}

std::vector<uint8_t> Encode(const SkPixmap& pixels, bool progressive) {
  SkPngRustEncoder::Options options = {
      .fCompressionLevel = SkPngRustEncoder::CompressionLevel::kLow};
  SkDynamicMemoryWStream stream;
  if (progressive) {
    auto encoder = SkPngRustEncoder::Make(&stream, pixels, options);
    assert(encoder);
    for (int y = 0; y < pixels.height(); ++y) assert(encoder->encodeRows(1));
  } else {
    assert(SkPngRustEncoder::Encode(&stream, pixels, options));
  }
  auto data = stream.detachAsData();
  return {data->bytes(), data->bytes() + data->size()};
}

std::vector<uint8_t> Decode(const std::vector<uint8_t>& png) {
  auto codec = SkCodec::MakeFromData(SkData::MakeWithCopy(png.data(), png.size()));
  assert(codec);
  const auto info = codec->getInfo().makeColorType(kRGBA_8888_SkColorType)
                       .makeAlphaType(kUnpremul_SkAlphaType);
  std::vector<uint8_t> bytes(info.computeMinByteSize());
  assert(codec->getPixels(info, bytes.data(), info.minRowBytes()) == SkCodec::kSuccess);
  return bytes;
}
}  // namespace

int main() {
  const sk_sp<SkColorSpace> spaces[] = {
      SkColorSpace::MakeSRGB(),
      SkColorSpace::MakeRGB(SkNamedTransferFn::kSRGB, SkNamedGamut::kDisplayP3)};
  size_t cases = 0;
  for (const auto& space : spaces) {
    for (SkColorType ct : {kRGBA_8888_SkColorType, kBGRA_8888_SkColorType}) {
      for (SkAlphaType at : {kOpaque_SkAlphaType, kPremul_SkAlphaType,
                             kUnpremul_SkAlphaType}) {
        SkBitmap original;
        assert(original.tryAllocPixels(SkImageInfo::Make(4, 2, ct, at, space)));
        for (int y = 0; y < 2; ++y) {
          for (int x = 0; x < 4; ++x) {
            const uint8_t alpha = at == kOpaque_SkAlphaType ? 255 :
                std::array<uint8_t, 4>{0, 1, 64, 128}[x];
            auto* pixel = static_cast<uint8_t*>(original.getPixels()) +
                          y * original.rowBytes() + x * 4;
            pixel[0] = at == kPremul_SkAlphaType ? alpha / 4 : 48;
            pixel[1] = at == kPremul_SkAlphaType ? alpha / 2 : 96;
            pixel[2] = at == kPremul_SkAlphaType ? alpha : 160;
            pixel[3] = alpha;
            if (ct == kBGRA_8888_SkColorType) std::swap(pixel[0], pixel[2]);
          }
        }
        const std::vector<uint8_t> original_bytes(
            static_cast<const uint8_t*>(original.getPixels()),
            static_cast<const uint8_t*>(original.getPixels()) + original.computeByteSize());
        original.setImmutable();
        auto image = original.asImage();
        assert(image);
        const auto target = image->imageInfo().colorInfo().makeAlphaType(kUnpremul_SkAlphaType);
        assert(!Before(kMimeTypePng, target, image.get()));
        assert(After(kMimeTypePng, target, image.get()) == (at == kOpaque_SkAlphaType));
        for (Mime mime : {kMimeTypeJpeg, kMimeTypeWebp}) {
          assert(Before(mime, target, image.get()) == After(mime, target, image.get()));
        }
        SkBitmap synchronous;
        assert(synchronous.tryAllocPixels(SkImageInfo::Make(image->dimensions(), target)));
        assert(image->readPixels(synchronous.pixmap(), 0, 0));
        SkBitmap asynchronous;
        SkPixmap chosen = original.pixmap();
        if (After(kMimeTypePng, target, image.get())) {
          assert(asynchronous.tryAllocPixels(SkImageInfo::Make(image->dimensions(), target)));
          assert(image->readPixels(asynchronous.pixmap(), 0, 0));
          chosen = asynchronous.pixmap();
        }
        assert(chosen.alphaType() == (at == kOpaque_SkAlphaType ? kUnpremul_SkAlphaType : at));
        assert(SkColorSpace::Equals(chosen.colorSpace(), space.get()));
        const auto old_png = Encode(original.pixmap(), false);
        const auto sync_png = Encode(synchronous.pixmap(), false);
        const auto new_png = Encode(chosen, false);
        assert(new_png == Encode(chosen, true));
        if (at == kOpaque_SkAlphaType) {
          assert(old_png.at(25) == 2 && new_png.at(25) == 6);
          assert(old_png != sync_png && new_png == sync_png);
          assert(Decode(old_png) == Decode(new_png));
        } else {
          assert(old_png == new_png);
        }
        assert(std::memcmp(original.getPixels(), original_bytes.data(), original_bytes.size()) == 0);
        ++cases;
      }
    }
  }
  assert(cases == 12);
  std::cout << "PASS real-Skia cases=" << cases << '\n';
}
