#include "q4t/server/program_worker.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include "object_protocol.h"
#include "program_vm.h"
#include "native_python.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kPythonBytes = 32 * 1024 * 1024;
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
enum class Program { kJavaScript, kPython, kMeteor };

ObjectReply Execute(const io::Json& input, const BinaryAccess& access,
                    GrammarControl control, Program kind,
                    const detail::ProgramFrameSink& progress = {}) {
  const bool python = kind != Program::kJavaScript;
  const bool meteor = kind == Program::kMeteor;
  const char* entry = python ? "--hosted-python-worker" : "--program-worker";
  if (progress) entry = "--chatkit-action-worker";
  if (meteor) entry = "--eval-meteor-worker";
  const auto input_limit = python ? kPythonBytes : kInputBytes;
  const auto output_limit = python ? kPythonBytes : kOutputBytes;
  const auto bytes = OutputJson(input);
  if (bytes.size() > input_limit) return Failure(GrammarFailure::kResource);
  control.domain = IsolatedWorkerDomain::kPrograms;
  control.address_space_bytes =
      std::min<uint64_t>(control.address_space_bytes, 1024ULL * 1024 * 1024);
  control.compile_deadline =
      std::min({control.compile_deadline, access.deadline,
                Clock::now() + std::chrono::seconds(python ? 10 : 2)});
  const auto cancelled = control.cancelled;
  control.cancelled = [&] {
    return (cancelled && cancelled()) ||
           (access.cancelled && access.cancelled());
  };
  if (control.cancelled()) return Failure(GrammarFailure::kCancelled);
  GrammarFailure failure;
  std::unique_ptr<detail::GrammarProcess> worker;
  if (!detail::GrammarProcess::Start(entry, control, &worker, &failure))
    return Failure(failure);
  uint64_t size = bytes.size();
  if (!worker->Transfer(&size, sizeof(size), true, control.compile_deadline,
                        control.cancelled) ||
      !worker->Transfer(const_cast<char*>(bytes.data()), size, true,
                        control.compile_deadline, control.cancelled))
    return Failure(worker->Failure());
  if (progress) {
    // A zero length ends the provisional frames. CollectOutput still verifies
    // EOF and normal exit before exposing the final program response.
    size_t stream_bytes = 0;
    for (unsigned count = 0;; ++count) {
      uint32_t length = 0;
      if (!worker->Transfer(&length, sizeof(length), false,
                            control.compile_deadline, control.cancelled))
        return Failure(worker->Failure());
      if (length == 0) break;
      if (length > detail::kMaxActionFrameBytes ||
          length > detail::kMaxActionStreamBytes - stream_bytes ||
          count >= detail::kMaxActionProgressFrames)
        return Error(502, "chatkit_action_invalid_result",
                     "The workflow exceeded its streaming limit");
      stream_bytes += length;
      std::string encoded(length, '\0');
      if (!worker->Transfer(encoded.data(), length, false,
                            control.compile_deadline, control.cancelled))
        return Failure(worker->Failure());
      Json frame;
      if (!io::ParseJson(encoded, &frame, true, kApplicationJsonLimits) ||
          (!frame.IsObject() &&
           (!frame.IsString() ||
            frame.str.size() > detail::kMaxActionProgressBytes)))
        return Error(502, "chatkit_action_invalid_result",
                     "The workflow returned an invalid streaming frame");
      if (!progress(frame)) return Failure(GrammarFailure::kCancelled);
      uint8_t accepted = 1;
      if (!worker->Transfer(&accepted, sizeof(accepted), true,
                            control.compile_deadline, control.cancelled))
        return Failure(worker->Failure());
    }
  }
  std::string output;
  if (!worker->CollectOutput(&output, output_limit, control.compile_deadline,
                             control.cancelled))
    return Failure(worker->Failure());
  Json value;
  auto limits = kSchemaJsonLimits;
  limits.max_string_bytes = input_limit;
  limits.max_values = 512 * 1024;
  if (!io::ParseJson(output, &value, true, limits) || !value.IsObject())
    return Failure(GrammarFailure::kWorker);
  if (meteor) {
    const auto* over = value.Find("over_budget");
    const auto* score = value.Find("score");
    const auto* work = value.Find("work");
    if (!over || !over->IsBool() || !score || !work || !work->IsNumber() ||
        value.object.size() != 3 ||
        work->number != static_cast<double>(value.GetInt("work", -1)) ||
        work->number < 0 ||
        work->number > input.GetNumber("meteor_work_budget") ||
        (value.GetBool("over_budget")
             ? !score->IsNull()
             : !score->IsNumber() ||
                   !(score->number >= 0 && score->number <= 1)))
      return Failure(GrammarFailure::kWorker);
  } else if (python && input.Has("grader")) {
    const auto* score = value.Find("score");
    const auto* error = value.Find("error");
    if (!error || !error->IsBool() || !score || value.object.size() != 2 ||
        (value.GetBool("error") ? !score->IsNull() : !score->IsNumber()))
      return Failure(GrammarFailure::kWorker);
  } else if (python) {
    if (!value.GetArray("files") || !value.Find("logs") ||
        !value.Find("logs")->IsString() || !value.Find("error") ||
        !value.Find("error")->IsBool() || value.object.size() != 3)
      return Failure(GrammarFailure::kWorker);
  } else if (!value.GetArray("calls") || !value.GetArray("output") ||
             (value.GetString("status") != "completed" &&
              value.GetString("status") != "suspended" &&
              value.GetString("status") != "incomplete")) {
    return Failure(GrammarFailure::kWorker);
  }
  ObjectReply reply;
  reply.value = std::move(value);
  return reply;
}

}  // namespace

ObjectReply ExecuteProgramWorker(const io::Json& input,
                                 const BinaryAccess& access,
                                 GrammarControl control) {
  return Execute(input, access, std::move(control), Program::kJavaScript);
}

ObjectReply ExecuteActionWorker(const io::Json& input,
                                const BinaryAccess& access,
                                const detail::ProgramFrameSink& progress) {
  return Execute(input, access, {}, Program::kJavaScript, progress);
}

ObjectReply ExecutePythonWorker(const io::Json& input,
                                const BinaryAccess& access,
                                GrammarControl control) {
  return Execute(input, access, std::move(control), Program::kPython);
}

ObjectReply ExecuteMeteorWorker(const io::Json& input,
                                const BinaryAccess& access) {
  return Execute(input, access, {}, Program::kMeteor);
}

int MaybeRunProgramWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  const bool meteor = std::strcmp(argv[1], "--eval-meteor-worker") == 0;
  const bool python =
      meteor || std::strcmp(argv[1], "--hosted-python-worker") == 0;
  const bool action = std::strcmp(argv[1], "--chatkit-action-worker") == 0;
  if (!python && !action && std::strcmp(argv[1], "--program-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  try {
    uint64_t size = 0;
    if (!detail::WorkerTransfer(&size, sizeof(size), false) || !size ||
        size > (python ? kPythonBytes : kInputBytes))
      return 3;
    std::string bytes(size, '\0');
    if (!detail::WorkerTransfer(bytes.data(), bytes.size(), false)) return 3;
    if (python) {
      std::string result;
      if (!detail::RunIsolatedPython(bytes, &result, meteor)) return 4;
      return detail::WorkerTransfer(result.data(), result.size(), true) ? 0 : 3;
    }
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
    detail::ProgramFrameSink progress;
    if (action) {
      progress = [](const Json& frame) {
        auto encoded = OutputJson(frame);
        uint32_t size = static_cast<uint32_t>(encoded.size());
        uint8_t accepted = 0;
        return detail::WorkerTransfer(&size, sizeof(size), true) &&
               detail::WorkerTransfer(encoded.data(), size, true) &&
               detail::WorkerTransfer(&accepted, sizeof(accepted), false) &&
               accepted == 1;
      };
    }
    const auto output = OutputJson(detail::EvaluateProgram(input, progress));
    if (action) {
      uint32_t end = 0;
      if (!detail::WorkerTransfer(&end, sizeof(end), true)) return 3;
    }
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
