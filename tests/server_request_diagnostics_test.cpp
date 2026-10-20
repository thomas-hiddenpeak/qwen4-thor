#include "q4t/server/request_diagnostics.h"
#include "q4t/test.h"

namespace {
std::string Read(FILE* file) {
  std::rewind(file);
  std::string output;
  char buffer[1024];
  while (std::fgets(buffer, sizeof(buffer), file)) output += buffer;
  return output;
}
}  // namespace

Q4T_TEST(request_diagnostics_terminal_and_reuse) {
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  for (int i = 0; i < 2; ++i) {
    q4t::server::RequestDiagnostics d(file);
    d.Bind("reused-id");
    d.Admitted(0);
    d.Stage("prefill");
    d.Released();
    d.Outcome(i ? "cancelled" : "success",
              i ? "explicit_cancel" : "completed");
  }
  const auto text = Read(file);
  std::fclose(file);
  Q4T_CHECK(text.find("released=1 outcome=success") != std::string::npos);
  Q4T_CHECK(text.find("released=1 outcome=cancelled") != std::string::npos);
  const auto first = text.find("instance=");
  const auto next = text.find("event=begin", text.find("event=begin") + 1);
  Q4T_CHECK(next != std::string::npos && first != std::string::npos);
  const auto second = text.rfind("instance=", next);
  Q4T_CHECK(text.substr(first, text.find(' ', first) - first) !=
            text.substr(second, text.find(' ', second) - second));
  return true;
}

Q4T_TEST(request_diagnostics_wait_accounting) {
  using D = q4t::server::RequestDiagnostics;
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  {
    D d(file);
    d.Observe(D::Wait::kSlot, std::chrono::milliseconds(7));
    d.Observe(D::Wait::kScheduler, std::chrono::milliseconds(11));
    d.Observe(D::Wait::kScheduler, std::chrono::milliseconds(13));
    Q4T_CHECK(d.Total(D::Wait::kSlot) == 7);
    Q4T_CHECK(d.Total(D::Wait::kScheduler) == 24);
    std::mutex mutex;
    { auto lock = d.Lock(mutex, D::Wait::kModel); }
    Q4T_CHECK(mutex.try_lock());
    mutex.unlock();
  }
  const auto text = Read(file);
  std::fclose(file);
  Q4T_CHECK(text.find("scheduler_waits=2") != std::string::npos);
  Q4T_CHECK(text.find("outcome=error") != std::string::npos);
  Q4T_CHECK(text.find("id=unregistered") != std::string::npos);
  return true;
}
