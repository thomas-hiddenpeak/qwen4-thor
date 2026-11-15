#include "q4t/server/program_worker.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include "object_protocol.h"
#include "program_vm.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kInputBytes = 8 * 1024 * 1024;
constexpr size_t kOutputBytes = 10 * 1024 * 1024;
ObjectReply Failure(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Program execution cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded", "Program execution expired");
  return Error(503,
               failure == GrammarFailure::kResource
                   ? "program_execution_capacity"
                   : "program_execution_unavailable",
               "Program executor unavailable");
}
}  // namespace

ObjectReply ExecuteProgramWorker(const io::Json& input,
                                 const BinaryAccess& access,
                                 GrammarControl control) {
  const auto bytes = OutputJson(input);
  if (bytes.size() > kInputBytes) return Failure(GrammarFailure::kResource);
  control.domain = IsolatedWorkerDomain::kPrograms;
  control.address_space_bytes =
      std::min<uint64_t>(control.address_space_bytes, 1024ULL * 1024 * 1024);
  control.compile_deadline =
      std::min({control.compile_deadline, access.deadline,
                Clock::now() + std::chrono::seconds(2)});
  const auto cancelled = control.cancelled;
  control.cancelled = [&] {
    return (cancelled && cancelled()) ||
           (access.cancelled && access.cancelled());
  };
  if (control.cancelled()) return Failure(GrammarFailure::kCancelled);
  GrammarFailure failure;
  std::unique_ptr<detail::GrammarProcess> worker;
  if (!detail::GrammarProcess::Start("--program-worker", control, &worker,
                                     &failure))
    return Failure(failure);
  uint64_t size = bytes.size();
  if (!worker->Transfer(&size, sizeof(size), true, control.compile_deadline,
                        control.cancelled) ||
      !worker->Transfer(const_cast<char*>(bytes.data()), size, true,
                        control.compile_deadline, control.cancelled))
    return Failure(worker->Failure());
  std::string output;
  if (!worker->CollectOutput(&output, kOutputBytes, control.compile_deadline,
                             control.cancelled))
    return Failure(worker->Failure());
  Json value;
  auto limits = kSchemaJsonLimits;
  limits.max_string_bytes = kInputBytes;
  limits.max_values = 512 * 1024;
  if (!io::ParseJson(output, &value, true, limits) || !value.IsObject() ||
      !value.GetArray("calls") || !value.GetArray("output") ||
      (value.GetString("status") != "completed" &&
       value.GetString("status") != "suspended" &&
       value.GetString("status") != "incomplete"))
    return Failure(GrammarFailure::kWorker);
  ObjectReply reply;
  reply.value = std::move(value);
  return reply;
}

int MaybeRunProgramWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--program-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    uint64_t size = 0;
    if (!detail::WorkerTransfer(&size, sizeof(size), false) || !size ||
        size > kInputBytes)
      return 3;
    std::string bytes(size, '\0');
    if (!detail::WorkerTransfer(bytes.data(), bytes.size(), false)) return 3;
    Json input;
    auto limits = kSchemaJsonLimits;
    limits.max_string_bytes = kInputBytes;
    limits.max_values = 512 * 1024;
    if (!io::ParseJson(bytes, &input, true, limits)) return 3;
    // Fix timezone before disabling filesystem access. No inherited host
    // environment, server descriptor or GPU allocation reaches the VM.
    if (setenv("TZ", "UTC0", 1) != 0) return 4;
    tzset();
    if (!detail::EnterNativeDocumentSandbox()) return 4;
    const auto output = OutputJson(detail::EvaluateProgram(input));
    if (output.size() > kOutputBytes) return 3;
    return detail::WorkerTransfer(const_cast<char*>(output.data()),
                                  output.size(), true)
               ? 0
               : 3;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
