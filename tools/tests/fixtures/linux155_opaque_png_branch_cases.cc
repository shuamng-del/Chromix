// Extracted constructor conditions with real SkColorInfo methods and branch shims.
#include <cassert>
#include <iostream>

namespace {
enum Mime { kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp };
struct ImageInfo {
  SkColorInfo value;
  SkAlphaType alphaType() const { return value.alphaType(); }
  const SkColorInfo& colorInfo() const { return value; }
};
struct Image {
  ImageInfo value;
  const ImageInfo& imageInfo() const { return value; }
};

bool Before(Mime mime_type_, const SkColorInfo& target_color_info,
            const Image* skia_image_) {
  static_cast<void>(mime_type_);
  return @BEFORE_CONDITION@;
}

bool After(Mime mime_type_, const SkColorInfo& target_color_info,
           const Image* skia_image_) {
  return @AFTER_CONDITION@;
}
}  // namespace

int main() {
  const sk_sp<SkColorSpace> spaces[] = {
      nullptr, std::make_shared<SkColorSpace>(SkColorSpace{1}),
      std::make_shared<SkColorSpace>(SkColorSpace{1}),
      std::make_shared<SkColorSpace>(SkColorSpace{2}),
      std::make_shared<SkColorSpace>(SkColorSpace{3})};
  size_t cases = 0, changed = 0;
  for (Mime mime : {kMimeTypePng, kMimeTypeJpeg, kMimeTypeWebp}) {
    for (int alpha = kUnknown_SkAlphaType; alpha <= kLastEnum_SkAlphaType; ++alpha) {
      for (int src = kUnknown_SkColorType; src <= kLastEnum_SkColorType; ++src) {
        for (int dst = kUnknown_SkColorType; dst <= kLastEnum_SkColorType; ++dst) {
          for (const auto& source_space : spaces) {
            for (const auto& target_space : spaces) {
              const auto source_alpha = static_cast<SkAlphaType>(alpha);
              Image image{{SkColorInfo(static_cast<SkColorType>(src), source_alpha,
                                      source_space)}};
              const SkColorInfo target(static_cast<SkColorType>(dst),
                                       kUnpremul_SkAlphaType, target_space);
              const bool different_space =
                  bool(source_space) != bool(target_space) ||
                  (source_space && target_space &&
                   source_space->identity != target_space->identity);
              const bool existing_conversion = src != dst || different_space;
              const bool opaque_png = mime == kMimeTypePng &&
                                      source_alpha == kOpaque_SkAlphaType;
              const bool before = Before(mime, target, &image);
              const bool after = After(mime, target, &image);
              assert(before == existing_conversion);
              assert(after == (existing_conversion || opaque_png));
              assert((before != after) == (opaque_png && !existing_conversion));
              assert(target.alphaType() == kUnpremul_SkAlphaType);
              assert(target.colorType() == static_cast<SkColorType>(dst));
              assert(target.refColorSpace() == target_space);
              changed += before != after;
              ++cases;
            }
          }
        }
      }
    }
  }
  assert(changed > 0);
  std::cout << "PASS branch-only cases=" << cases << " changed=" << changed << '\n';
}
