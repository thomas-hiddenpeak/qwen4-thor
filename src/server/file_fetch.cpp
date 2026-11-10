#include "q4t/server/file_fetch.h"

#include <signal.h>
#include <sys/resource.h>

#include <algorithm>
#include <cstring>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
constexpr uint64_t kControlBytes = 128 * 1024;
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "File acquisition cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "File acquisition timed out");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_fetch_capacity",
                 "File acquisition capacity occupied");
  return Error(503, "file_fetch_worker_failed",
               "File acquisition worker unavailable");
}
}  // namespace

ObjectReply FetchFileInput(const BinaryAccess& access,
                           const FileInputSource& source,
                           const FileFetchOptions& options,
                           FileInputBudget* budget, FileFetchReceipt* result,
                           GrammarControl control) {
  if (!result) return Error(500, "file_input_state_invalid", "Missing output");
  *result = {};
  auto at = [&](ObjectReply reply) {
    if (!reply.ok()) {
      if (!reply.value.IsObject()) reply.value = Object();
      Set(&reply.value, "param", String(source.param));
    }
    return reply;
  };
  if (access.scope.empty())
    return at(
        Error(503, "object_scope_unavailable", "Authenticated scope required"));
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return at(Failed(GrammarFailure::kCancelled));
  if (Clock::now() >= access.deadline)
    return at(Failed(GrammarFailure::kTimeout));
  if (!budget || budget->max_bytes > kFileInputBytes || !budget->max_files ||
      budget->max_files > 4096 || budget->used_bytes > budget->max_bytes ||
      budget->used_files > budget->max_files)
    return at(
        Error(500, "file_input_budget_invalid", "Invalid file input budget"));
  if (budget->used_files == budget->max_files)
    return at(Error(413, "file_input_capacity_exceeded",
                    "File input budget exceeded"));
  file_detail::Url url;
  if (source.kind != FileInputSource::Kind::kUrl ||
      !file_detail::ParseUrl(source.value, &url))
    return at(Error(400, "invalid_request", "Invalid HTTP(S) file URL"));
  if (!file_detail::ValidOptions(options))
    return at(
        Error(500, "file_fetch_policy_invalid", "Invalid file fetch policy"));
  if (!file_detail::AllowedUrl(url, options))
    return at(
        Error(403, "file_fetch_target_denied", "File target is not permitted"));
  const uint64_t available = budget->max_bytes - budget->used_bytes;
  control.compile_deadline =
      std::min(access.deadline,
               Clock::now() + std::chrono::milliseconds(options.timeout_ms));
  control.domain = IsolatedWorkerDomain::kFileFetch;
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  if (!detail::GrammarProcess::Start("--file-fetch-worker", control, &process,
                                     &failure))
    return at(Failed(failure));
  // A failed or unconfirmed transfer retains its attempted byte reservation.
  // Success replaces that conservative reservation with actual received bytes.
  const uint64_t previous = budget->used_bytes;
  ++budget->used_files;
  budget->used_bytes += available;
  auto request = Object({{"url", String(url.Text())},
                         {"options", file_detail::OptionsJson(options)},
                         {"max_bytes", Number(available)},
                         {"timeout_ms", Number(options.timeout_ms)}});
  auto text = OutputJson(request);
  Frame frame{kVersion, 1, text.size()}, response;
  auto transfer = [&](void* bytes, size_t size, bool write) {
    return process->Transfer(bytes, size, write, control.compile_deadline,
                             access.cancelled);
  };
  if (text.size() > kControlBytes)
    return at(Error(500, "file_fetch_policy_invalid",
                    "File control capacity exceeded"));
  if (!transfer(&frame, sizeof(frame), true) ||
      !transfer(text.data(), text.size(), true) ||
      !transfer(&response, sizeof(response), false))
    return at(Failed(process->Failure()));
  if (response.version != kVersion || response.sequence != 1 ||
      !response.bytes || response.bytes > kControlBytes)
    return at(Failed(GrammarFailure::kWorker));
  text.assign(response.bytes, '\0');
  if (!transfer(text.data(), text.size(), false))
    return at(Failed(process->Failure()));
  Json wire;
  if (!Parse(text, &wire)) return at(Failed(GrammarFailure::kWorker));
  auto reply = DecodeReply(wire);
  if (!reply.ok()) return at(std::move(reply));
  const auto bytes = reply.value.GetInt("bytes", -1);
  const auto* urls = reply.value.GetArray("urls");
  const auto* mime = reply.value.Find("mime_type");
  const auto* filename = reply.value.Find("filename");
  if (bytes < 0 || static_cast<uint64_t>(bytes) > available || !urls ||
      urls->array.empty() || urls->array.size() > 11 || !mime ||
      !mime->IsString() || mime->str.size() > 255 || !filename ||
      !filename->IsString() || filename->str.size() > 1024)
    return at(Failed(GrammarFailure::kWorker));
  FileFetchReceipt next;
  for (const auto& item : urls->array) {
    file_detail::Url parsed;
    if (!item.IsString() || !file_detail::ParseUrl(item.str, &parsed) ||
        !file_detail::AllowedUrl(parsed, options))
      return at(Failed(GrammarFailure::kWorker));
    next.urls.push_back(item.str);
  }
  if (next.urls.front() != url.Text())
    return at(Failed(GrammarFailure::kWorker));
  next.content.bytes.resize(bytes);
  if (!transfer(next.content.bytes.data(), bytes, false))
    return at(Failed(process->Failure()));
  next.content.mime_type = mime->str;
  next.content.filename =
      source.filename.empty() ? filename->str : source.filename;
  next.content.sha256 = Digest(next.content.bytes);
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return at(Failed(GrammarFailure::kCancelled));
  if (Clock::now() >= control.compile_deadline)
    return at(Failed(GrammarFailure::kTimeout));
  budget->used_bytes = previous + bytes;
  *result = std::move(next);
  return {};
}

int MaybeRunFileFetchWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--file-fetch-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  // SSL's socket BIO does not expose MSG_NOSIGNAL. This disposition is local
  // to the exec-only child and never changes the serving process.
  struct sigaction ignored {};
  ignored.sa_handler = SIG_IGN;
  sigemptyset(&ignored.sa_mask);
  if (sigaction(SIGPIPE, &ignored, nullptr)) return 2;
  const rlimit cpu{35, 35};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  try {
    Frame frame;
    if (!detail::WorkerTransfer(&frame, sizeof(frame), false) ||
        frame.version != kVersion || frame.sequence != 1 || !frame.bytes ||
        frame.bytes > kControlBytes)
      return 3;
    std::string text(frame.bytes, '\0');
    if (!detail::WorkerTransfer(text.data(), text.size(), false)) return 3;
    Json request;
    if (!Parse(text, &request)) return 3;
    file_detail::Url url;
    FileFetchOptions options;
    const auto* policy = request.Find("options");
    const auto bytes = request.GetInt("max_bytes", -1);
    const auto timeout = request.GetInt("timeout_ms", -1);
    if (!policy || !ParseFileFetchOptions(*policy, &options).ok() ||
        bytes < 0 || static_cast<uint64_t>(bytes) > kFileInputBytes ||
        timeout < 1 || timeout > 30000 ||
        !file_detail::ParseUrl(request.GetString("url"), &url))
      return 3;
    FileFetchReceipt fetched;
    ObjectReply reply;
    try {
      reply = file_detail::Fetch(
          url, options, bytes,
          Clock::now() + std::chrono::milliseconds(timeout), &fetched);
    } catch (const std::bad_alloc&) {
      reply = Error(503, "file_fetch_memory_capacity",
                    "File acquisition memory limit");
    } catch (...) {
      reply = Error(503, "file_fetch_dependency_failed",
                    "File acquisition dependency unavailable");
    }
    if (reply.ok()) {
      auto urls = Array();
      for (const auto& value : fetched.urls)
        urls.array.push_back(String(value));
      reply.value = Object({{"bytes", Number(fetched.content.bytes.size())},
                            {"filename", String(fetched.content.filename)},
                            {"mime_type", String(fetched.content.mime_type)},
                            {"urls", std::move(urls)}});
    }
    text = OutputJson(WireReply(reply));
    if (text.empty() || text.size() > kControlBytes) return 3;
    frame = {kVersion, 1, text.size()};
    if (!detail::WorkerTransfer(&frame, sizeof(frame), true) ||
        !detail::WorkerTransfer(text.data(), text.size(), true))
      return 3;
    if (reply.ok() &&
        !detail::WorkerTransfer(fetched.content.bytes.data(),
                                fetched.content.bytes.size(), true))
      return 3;
    return 0;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
