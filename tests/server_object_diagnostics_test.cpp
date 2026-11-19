#include "q4t/server/object_diagnostics.h"

#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
std::string Read(FILE* file) {
  std::rewind(file);
  std::string result;
  std::array<char, 1024> bytes;
  while (const auto count = std::fread(bytes.data(), 1, bytes.size(), file))
    result.append(bytes.data(), count);
  return result;
}
}  // namespace

Q4T_TEST(object_diagnostics_success_and_quota_preserve_result_and_control) {
  for (bool success : {true, false}) {
    FILE* file = std::tmpfile();
    Q4T_CHECK(file);
    RequestControl control("item_observed", "",
                           Clock::now() + std::chrono::seconds(5));
    {
      RequestDiagnostics diagnostic(file);
      bool observed = false;
      auto reply = RunDiagnosedObjectRequest(
          diagnostic, control,
          [&] {
            ObjectReply result;
            if (success)
              result.value =
                  Object({{"text", String("actual result fixture")}});
            else
              result = Error(429, "client_rpm_exhausted", "quota fixture");
            return result;
          },
          &observed);
      Q4T_CHECK(reply.ok() == success && observed == success &&
                !control.Cancelled());
      Q4T_CHECK(success
                    ? reply.value.GetString("text") == "actual result fixture"
                    : reply.ClientCode() == "client_rpm_exhausted");
      // Observation must not finish the owner before final protocol emission.
      Q4T_CHECK(control.Cancel(RequestControl::State::kExplicit));
    }
    const auto text = Read(file);
    std::fclose(file);
    Q4T_CHECK(text.find(success
                            ? "outcome=success reason=execution_complete "
                            : "outcome=error reason=client_rpm_exhausted ") !=
              text.npos);
  }
  return true;
}

Q4T_TEST(object_diagnostics_cancellation_and_failure_are_distinct) {
  for (auto state :
       {RequestControl::State::kDisconnect, RequestControl::State::kDeadline,
        RequestControl::State::kFailure}) {
    FILE* file = std::tmpfile();
    Q4T_CHECK(file);
    RequestControl control("item_observed", "",
                           Clock::now() + std::chrono::seconds(5));
    {
      RequestDiagnostics diagnostic(file);
      bool observed = true;
      const auto reply = RunDiagnosedObjectRequest(
          diagnostic, control,
          [&] {
            control.Cancel(state);
            return Error(409, control.Reason(), "cancel fixture");
          },
          &observed);
      Q4T_CHECK(!reply.ok() && reply.http_status == 409 && !observed);
      Q4T_CHECK(control.Cancelled() && !control.Finish());
    }
    const auto text = Read(file);
    std::fclose(file);
    const std::string label = std::string("outcome=") +
                              (control.Failed() ? "error" : "aborted") +
                              " reason=" + control.Reason() + " ";
    Q4T_CHECK(text.find(label) != text.npos);
  }
  return true;
}

Q4T_TEST(object_diagnostics_exception_has_no_fake_completion_or_cancellation) {
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  RequestControl control("item_observed", "",
                         Clock::now() + std::chrono::seconds(5));
  bool caught = false;
  try {
    RequestDiagnostics diagnostic(file);
    RunDiagnosedObjectRequest(diagnostic, control, []() -> ObjectReply {
      throw std::runtime_error("executor fixture");
    });
  } catch (const std::runtime_error&) {
    caught = true;
  }
  const auto text = Read(file);
  std::fclose(file);
  Q4T_CHECK(caught && !control.Cancelled() && control.Finish());
  Q4T_CHECK(text.find("outcome=error reason=handler_exception ") != text.npos);
  return true;
}
