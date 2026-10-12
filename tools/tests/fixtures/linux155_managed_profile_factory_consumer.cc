// Isolated signature and real factory-body contract, not a Chromium link test.
#include "contract.h"

using ExpectedFactory = std::unique_ptr<ManagedProfileCreationController> (*)(
    Profile*, const AccountInfo&, signin_metrics::AccessPoint,
    base::OnceCallback<void(
        base::expected<Profile*, ManagedProfileCreationFailureReason>, bool)>,
    std::optional<policy::ProfileSeparationPolicies>,
    std::optional<signin::SigninChoice>);
static_assert(std::is_same_v<
              decltype(&ManagedProfileCreationController::CreateManagedProfileForTesting),
              ExpectedFactory>);

int main() {
  using Controller = ManagedProfileCreationController;
  Profile profile;
  AccountInfo account;
  int callbacks = 0;
  auto callback = [&](base::expected<Profile*, ManagedProfileCreationFailureReason> result,
                      bool required) {
    assert(result.has_value() && result.value() == &profile && !required);
    ++callbacks;
  };
  auto controller = Controller::CreateManagedProfileForTesting(
      &profile, account, signin_metrics::AccessPoint::kTest, callback,
      policy::ProfileSeparationPolicies{42}, signin::SigninChoice::kTest);
  assert(controller && Controller::live == 1 && Controller::init_calls == 1);
  assert(controller->source == &profile && controller->account == &account);
  assert(controller->access == signin_metrics::AccessPoint::kTest);
  assert(controller->policies_.value == 42);
  assert(controller->user_choice_for_testing_ == signin::SigninChoice::kTest);
  assert(controller->skip_browser_startup_for_testing_);
  controller->callback_(&profile, false);
  assert(callbacks == 1);
  controller.reset();
  assert(Controller::live == 0);

  controller = Controller::CreateManagedProfileForTesting(
      &profile, account, signin_metrics::AccessPoint::kTest, callback);
  assert(controller && controller->policies_.value == 0);
  assert(!controller->user_choice_for_testing_.has_value());
  controller.reset();
  Controller::init_result = false;
  controller = Controller::CreateManagedProfileForTesting(
      &profile, account, signin_metrics::AccessPoint::kTest, callback);
  assert(!controller && Controller::live == 0 && Controller::init_calls == 3);
}
