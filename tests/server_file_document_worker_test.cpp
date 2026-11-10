#include "q4t/server/file_document_worker.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstring>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() {
  return {"client:native-documents", Clock::now() + 3s, {}};
}
FileInputContent Input(std::string text,
                       std::string name = "../display-only.md") {
  FileInputContent input;
  input.bytes = std::move(text);
  input.filename = std::move(name);
  return input;
}
GrammarControl Control() {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  return control;
}
bool Clean(const GrammarControl& control) {
  return control.counters->started == 1 && control.counters->reaped == 1 &&
         control.counters->active == 0;
}
}  // namespace

int DocumentSandboxFixture(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--document-sandbox-fixture")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv) ||
      !detail::EnterNativeDocumentSandbox())
    return 2;
  uint64_t result[7]{};
  int file = open("/etc/hostname", O_RDONLY | O_CLOEXEC);
  result[0] = file < 0 && errno == EPERM;
  if (file >= 0) close(file);
  int network = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  result[1] = network < 0 && errno == EPERM;
  if (network >= 0) close(network);
  char missing[] = "/q4t-fixture-path-that-does-not-exist";
  char* args[] = {missing, nullptr};
  char* env[] = {nullptr};
  result[2] = execve(missing, args, env) < 0 && errno == EPERM;
  result[3] = kill(getpid(), 0) < 0 && errno == EPERM;
  void* memory = mmap(nullptr, 4096, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  result[4] = memory != MAP_FAILED;
  if (memory != MAP_FAILED) {
    result[5] =
        mprotect(memory, 4096, PROT_READ | PROT_EXEC) < 0 && errno == EPERM;
    result[6] = munmap(memory, 4096) == 0;
  }
  return detail::WorkerTransfer(result, sizeof(result), true) ? 0 : 3;
}

Q4T_TEST(file_document_worker_actual_text_and_sheet_match_original_data) {
  auto control = Control();
  FileDocument document;
  auto input = Input("# 真正文档\r\nUnicode 😀\n");
  Q4T_CHECK(
      ParseNativeFileDocument(input, {}, Access(), &document, control).ok() &&
      document.text == input.bytes &&
      document.source_sha256 == Digest(input.bytes) && Clean(control));
  control = Control();
  input = Input("column,value\r\n\"a,b\",42\r\n", "data.csv");
  Q4T_CHECK(
      ParseNativeFileDocument(input, {}, Access(), &document, control).ok() &&
      document.sheets.size() == 1 && document.sheets[0].source_rows == 2 &&
      document.sheets[0].rows[1] == std::vector<std::string>({"a,b", "42"}) &&
      Clean(control));
  return true;
}
Q4T_TEST(file_document_worker_sandbox_denies_external_capabilities) {
  auto control = Control();
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> worker;
  Q4T_CHECK(detail::GrammarProcess::Start("--document-sandbox-fixture", control,
                                          &worker));
  uint64_t report[7]{};
  Q4T_CHECK(worker->Transfer(report, sizeof(report), false, Clock::now() + 2s));
  for (auto passed : report) Q4T_CHECK(passed == 1);
  worker.reset();
  Q4T_CHECK(Clean(control));
  return true;
}
Q4T_TEST(
    file_document_worker_bad_documents_and_budgets_do_not_poison_next_request) {
  auto control = Control();
  FileDocument document;
  auto input = Input("\"broken", "broken.csv");
  Q4T_CHECK(ParseNativeFileDocument(input, {}, Access(), &document, control)
                    .http_status == 400 &&
            document.sheets.empty() && Clean(control));
  control = Control();
  FileDocumentLimits limit;
  limit.max_text_bytes = 3;
  Q4T_CHECK(ParseNativeFileDocument(Input("larger"), limit, Access(), &document,
                                    control)
                    .http_status == 413 &&
            document.text.empty() && Clean(control));
  control = Control();
  Q4T_CHECK(ParseNativeFileDocument(Input("healthy"), {}, Access(), &document,
                                    control)
                .ok() &&
            document.text == "healthy" && Clean(control));
  return true;
}
Q4T_TEST(file_document_worker_cancel_during_actual_raw_input_transfer) {
  auto control = Control();
  FileDocument document;
  auto access = Access();
  int checks = 0;
  access.cancelled = [&] { return ++checks >= 6; };
  const auto input = Input(std::string(1 << 20, 'x'));
  Q4T_CHECK(ParseNativeFileDocument(input, {}, access, &document, control)
                    .http_status == 499 &&
            document.text.empty() && document.source_sha256.empty() &&
            Clean(control));
  return true;
}
Q4T_TEST(file_document_worker_stall_and_crash_have_bounded_owned_recovery) {
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting(""); });
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  auto control = Control();
  auto access = Access();
  access.deadline = Clock::now() + 100ms;
  FileDocument document;
  const auto started = Clock::now();
  Q4T_CHECK(
      ParseNativeFileDocument(Input("input"), {}, access, &document, control)
              .http_status == 408 &&
      Clock::now() - started < 1s && Clean(control));
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-short");
  control = Control();
  Q4T_CHECK(
      ParseNativeFileDocument(Input("input"), {}, Access(), &document, control)
              .http_status == 503 &&
      document.text.empty() && Clean(control));
  detail::SetDocumentWorkerEntryForTesting("");
  control = Control();
  Q4T_CHECK(ParseNativeFileDocument(Input("continued"), {}, Access(), &document,
                                    control)
                .ok() &&
            document.text == "continued" && Clean(control));
  return true;
}
Q4T_TEST(file_document_worker_deferred_exit_keeps_slot_and_loop_reaps) {
  auto control = Control();
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> held, fetch, grammar;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", control,
                                          &held));
  detail::DeferGrammarReapingForTesting(true);
  ScopeExit reset([&] {
    detail::DeferGrammarReapingForTesting(false);
    const auto until = Clock::now() + 300ms;
    while (control.counters->reaped == 0 && Clock::now() < until) {
      detail::PollRequestWorkers();
      std::this_thread::sleep_for(1ms);
    }
  });
  held.reset();
  FileDocument document;
  auto reply =
      ParseNativeFileDocument(Input("denied"), {}, Access(), &document);
  Q4T_CHECK(reply.code == "file_document_worker_capacity");
  GrammarControl other;
  other.domain = IsolatedWorkerDomain::kFileFetch;
  Q4T_CHECK(
      detail::GrammarProcess::Start("--grammar-fixture-silent", other, &fetch));
  Q4T_CHECK(
      detail::GrammarProcess::Start("--grammar-fixture-silent", {}, &grammar));
  detail::DeferGrammarReapingForTesting(false);
  fetch.reset();
  grammar.reset();
  const auto until = Clock::now() + 300ms;
  while (control.counters->reaped == 0 && Clock::now() < until) {
    detail::PollRequestWorkers();
    std::this_thread::sleep_for(1ms);
  }
  Q4T_CHECK(Clean(control));
  return true;
}
Q4T_TEST(file_document_worker_invalid_owner_and_config_launch_nothing) {
  auto control = Control();
  FileDocument document;
  auto access = Access();
  access.scope.clear();
  Q4T_CHECK(ParseNativeFileDocument(Input("a"), {}, access, &document, control)
                .http_status == 503);
  FileDocumentLimits limits;
  limits.max_columns = 0;
  Q4T_CHECK(
      ParseNativeFileDocument(Input("a"), limits, Access(), &document, control)
          .http_status == 500);
  Q4T_CHECK(control.counters->started == 0 && document.text.empty());
  return true;
}
