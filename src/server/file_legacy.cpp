#include "q4t/server/file_legacy.h"

#include <sys/random.h>

#include <algorithm>
#include <array>
#include <cstring>

#include "file_legacy_protocol.h"
#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace legacy_detail;
using object_detail::Error;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Document conversion cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Document conversion timed out");
  if (failure == GrammarFailure::kResource)
    return Error(413, "file_document_capacity",
                 "Document conversion capacity exceeded");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_document_worker_capacity",
                 "Document conversion capacity occupied");
  return Error(503, "file_document_worker_failed",
               "Document conversion worker unavailable");
}
ObjectReply FailureReply(Failure failure) {
  if (failure == Failure::kInvalid)
    return Error(400, "invalid_file_document", "Document conversion failed");
  if (failure == Failure::kCapacity) return Failed(GrammarFailure::kResource);
  if (failure == Failure::kDependency)
    return Error(503, "file_document_codec_unavailable",
                 "System document codec unavailable");
  return Error(503, "file_document_storage_unavailable",
               "Private document workspace unavailable");
}
bool Config(const FileLegacyOptions& options) {
  return !options.work_directory.empty() &&
         options.work_directory.front() == '/' &&
         options.work_directory.size() <= 4096 &&
         options.work_directory.find('\0') == std::string::npos &&
         options.timeout_ms > 0 && options.timeout_ms <= 120000 &&
         options.max_converted_bytes > 0 &&
         options.max_converted_bytes <= kFileInputBytes &&
         options.io.write_bytes <= (1ULL << 30) &&
         options.io.file_creations <= 65536 &&
         options.io.thread_creations <= 256 && options.io.notifications > 0 &&
         options.io.notifications <= 2000000;
}
ObjectReply Exchange(const FileInputContent& input,
                     const FileLegacyOptions& options,
                     const BinaryAccess& access, Request request, bool* reaped,
                     FileInputContent* converted, GrammarControl control) {
  *reaped = false;
  const auto original_finished = control.worker_finished;
  control.worker_finished = [&](const auto& report) {
    *reaped = report.reaped;
    if (original_finished) original_finished(report);
  };
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline = access.deadline;
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  if (!detail::GrammarProcess::Start("--file-legacy-worker", control, &process,
                                     &failure))
    return Failed(failure);
  const auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, access.deadline,
                             access.cancelled);
  };
  request.root_bytes = options.work_directory.size();
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(options.work_directory.data()),
                request.root_bytes, true))
    return Failed(process->Failure());
  Ready ready;
  if (!transfer(&ready, sizeof(ready), false))
    return Failed(process->Failure());
  if (request.operation == Operation::kClean) {
    std::string tail;
    if (ready != Ready::kClean) return FailureReply(Failure::kStorage);
    if (!process->CollectOutput(&tail, 0, access.deadline, access.cancelled))
      return Failed(process->Failure());
    return {};
  }
  if (ready == Ready::kStorage) return FailureReply(Failure::kStorage);
  if (ready != Ready::kReady)
    return Error(503, "file_document_isolation_unavailable",
                 "Document isolation unavailable");
  if (!process->ReceiveIoBudget(options.io, access.deadline,
                                access.cancelled) ||
      !transfer(const_cast<char*>(input.bytes.data()), input.bytes.size(),
                true))
    return Failed(process->Failure());
  std::string output;
  if (!process->CollectOutput(&output,
                              options.max_converted_bytes + sizeof(Result),
                              access.deadline, access.cancelled))
    return Failed(process->Failure());
  if (output.size() < sizeof(Result)) return Failed(GrammarFailure::kWorker);
  Result result;
  std::memcpy(&result, output.data(), sizeof(result));
  if (result.magic != kMagic ||
      result.bytes != output.size() - sizeof(Result) ||
      result.bytes > options.max_converted_bytes)
    return Failed(GrammarFailure::kWorker);
  if (result.failure != Failure::kNone) {
    if (result.bytes || result.failure > Failure::kStorage)
      return Failed(GrammarFailure::kWorker);
    return FailureReply(result.failure);
  }
  switch (result.format) {
    case Format::kText:
      converted->filename = "converted.odt";
      break;
    case Format::kSheet:
      converted->filename = "converted.ods";
      break;
    case Format::kSlides:
      converted->filename = "converted.odp";
      break;
    case Format::kDrawing:
      converted->filename = "converted.odg";
      break;
    default:
      return Failed(GrammarFailure::kWorker);
  }
  if (!result.bytes) return Failed(GrammarFailure::kWorker);
  converted->bytes.assign(output, sizeof(Result), result.bytes);
  return {};
}
}  // namespace

ObjectReply ParseLegacyFileDocument(const FileInputContent& input,
                                    const FileDocumentLimits& limits,
                                    const FileLegacyOptions& options,
                                    const BinaryAccess& access,
                                    FileDocument* output,
                                    GrammarControl control) {
  if (!output)
    return Error(500, "file_document_state_invalid", "Missing output");
  *output = {};
  if (!Config(options) || !ValidFileDocumentLimits(limits))
    return Error(500, "file_document_budget_invalid",
                 "Invalid conversion configuration");
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (input.bytes.empty()) return FailureReply(Failure::kInvalid);
  if (input.bytes.size() > kFileInputBytes)
    return Failed(GrammarFailure::kResource);
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Failed(GrammarFailure::kCancelled);
  auto bounded = access;
  bounded.deadline =
      std::min(access.deadline,
               Clock::now() + std::chrono::milliseconds(options.timeout_ms));
  if (Clock::now() >= bounded.deadline) return Failed(GrammarFailure::kTimeout);
  Request request;
  request.source_bytes = input.bytes.size();
  request.output_bytes = options.max_converted_bytes;
  std::array<unsigned char, 16> random{};
  if (getrandom(random.data(), random.size(), GRND_NONBLOCK) !=
      static_cast<ssize_t>(random.size()))
    return Error(503, "file_document_identity_unavailable",
                 "Document workspace identity unavailable");
  constexpr char hex[] = "0123456789abcdef";
  for (size_t i = 0; i < random.size(); ++i) {
    request.token[2 * i] = hex[random[i] >> 4];
    request.token[2 * i + 1] = hex[random[i] & 15];
  }
  bool reaped = false;
  FileInputContent converted;
  auto reply =
      Exchange(input, options, bounded, request, &reaped, &converted, control);
  if (reaped) {
    // Only after confirmed exit can another owned worker remove scratch. The
    // root lease also protects against another service or kernel-stalled owner.
    Request clean;
    clean.operation = Operation::kClean;
    auto cleanup_access = bounded;
    auto cleanup_control = control;
    if (!reply.ok()) {
      cleanup_access.cancelled = {};
      cleanup_control.cancelled = {};
      cleanup_access.deadline = Clock::now() + 100ms;
    }
    bool cleaned_owner = false;
    auto cleanup = Exchange({}, options, cleanup_access, clean, &cleaned_owner,
                            &converted, cleanup_control);
    if (reply.ok() && !cleanup.ok()) reply = cleanup;
  }
  if (!reply.ok()) return reply;
  if (!reaped) return Failed(GrammarFailure::kWorker);
  FileDocument document;
  reply =
      ParseNativeFileDocument(converted, limits, bounded, &document, control);
  if (!reply.ok()) return reply;
  document.source_sha256 = object_detail::Digest(input.bytes);
  *output = std::move(document);
  return {};
}
}  // namespace q4t::server
