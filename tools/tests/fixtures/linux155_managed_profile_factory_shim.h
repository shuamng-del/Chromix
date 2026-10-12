// Isolated factory dependencies; these do not model Chromium Init or signin.
#pragma once

#include <cassert>
#include <expected>
#include <functional>
#include <memory>
#include <optional>
#include <type_traits>
#include <utility>

struct Profile {};
struct AccountInfo {};
namespace signin_metrics {
enum class AccessPoint { kTest };
}
namespace signin {
enum class SigninChoice { kTest };
}
namespace policy {
struct ProfileSeparationPolicies {
  int value = 0;
};
}
enum class ManagedProfileCreationFailureReason { kTest };
namespace base {
template <class T, class E>
using expected = std::expected<T, E>;
template <class Signature>
using OnceCallback = std::function<Signature>;
namespace internal {
inline bool get_is_test_impl() { return true; }
}
}
#define CHECK(value) assert(value)
@CHECK_IS_TEST@
@CALLBACK_ALIAS@

class ManagedProfileCreationController {
 public:
@DECLARATION@

  ManagedProfileCreationController(
      Profile* source_profile, const AccountInfo& account_info,
      signin_metrics::AccessPoint access_point,
      ManagedProfileCreationControllerCallback callback,
      policy::ProfileSeparationPolicies policies)
      : source(source_profile), account(&account_info), access(access_point),
        callback_(std::move(callback)), policies_(policies) { ++live; }
  ~ManagedProfileCreationController() { --live; }
  bool Init() { ++init_calls; return init_result; }

  Profile* source;
  const AccountInfo* account;
  signin_metrics::AccessPoint access;
  ManagedProfileCreationControllerCallback callback_;
  policy::ProfileSeparationPolicies policies_;
  std::optional<signin::SigninChoice> user_choice_for_testing_;
  bool skip_browser_startup_for_testing_ = false;
  inline static bool init_result = true;
  inline static int init_calls = 0;
  inline static int live = 0;
};
