// Branch-only dependencies; no pixel conversion, ICC transform or PNG encoder.
#pragma once
#include <memory>
#include <utility>

template <class T> using sk_sp = std::shared_ptr<T>;

struct SkColorSpace {
  int identity;
  static bool Equals(const SkColorSpace* a, const SkColorSpace* b) {
    return a == b || (a && b && a->identity == b->identity);
  }
};

@ALPHA_ENUM@
@COLOR_ENUM@

class SkColorInfo {
 public:
  SkColorInfo(SkColorType, SkAlphaType, sk_sp<SkColorSpace>);
  SkColorType colorType() const { return fColorType; }
  SkAlphaType alphaType() const { return fAlphaType; }
  sk_sp<SkColorSpace> refColorSpace() const { return fColorSpace; }
  bool operator==(const SkColorInfo&) const;
  bool operator!=(const SkColorInfo&) const;
  SkColorInfo makeAlphaType(SkAlphaType) const;

 private:
  sk_sp<SkColorSpace> fColorSpace;
  SkColorType fColorType;
  SkAlphaType fAlphaType;
};

@SKIA_COLOR_INFO_METHODS@
