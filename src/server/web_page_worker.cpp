#include "q4t/server/web_search.h"

#include <sys/resource.h>

#include <algorithm>
#include <cstring>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/http_response.h"
#include "web_page_internal.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kControlBytes = 32768, kReplyBytes = 16U << 20;
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Web page extraction cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Web page extraction timed out");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "web_page_worker_capacity",
                 "Web page codec slot occupied");
  return Error(503, "web_page_worker_failed", "Web page codec unavailable");
}
Json Limits(const WebPageLimits& limits) {
  return Object({{"source_bytes", Number(limits.max_source_bytes)},
                 {"text_bytes", Number(limits.max_text_bytes)},
                 {"links", Number(limits.max_links)},
                 {"images", Number(limits.max_images)},
                 {"nodes", Number(limits.max_nodes)},
                 {"depth", Number(limits.max_depth)},
                 {"timeout_ms", Number(limits.timeout_ms)}});
}
bool Limits(const Json& value, WebPageLimits* limits) {
  if (!value.IsObject()) return false;
  for (const auto* name : {"source_bytes", "text_bytes", "links", "images",
                           "nodes", "depth", "timeout_ms"})
    if (value.GetInt(name, -1) < 0) return false;
  limits->max_source_bytes = value.GetInt("source_bytes");
  limits->max_text_bytes = value.GetInt("text_bytes");
  limits->max_links = value.GetInt("links");
  limits->max_images = value.GetInt("images");
  limits->max_nodes = value.GetInt("nodes");
  limits->max_depth = value.GetInt("depth");
  const auto timeout = value.GetInt("timeout_ms");
  if (timeout > 30000) return false;
  limits->timeout_ms = timeout;
  return ValidWebPageLimits(*limits);
}
using web_detail::Page;
using web_detail::Text;
}  // namespace

bool ValidWebPageLimits(const WebPageLimits& limits) {
  return limits.max_source_bytes && limits.max_source_bytes <= (2U << 20) &&
         limits.max_text_bytes && limits.max_text_bytes <= (1U << 20) &&
         limits.max_links <= 256 && limits.max_images <= 128 &&
         limits.max_nodes && limits.max_nodes <= 200000 && limits.max_depth &&
         limits.max_depth <= 128 && limits.timeout_ms >= 1 &&
         limits.timeout_ms <= 30000;
}

ObjectReply ParseWebPage(const FileInputContent& input,
                         std::string_view raw_url, const WebPageLimits& limits,
                         const BinaryAccess& access, WebPage* output,
                         GrammarControl control) {
  if (!output) return Error(500, "web_page_state_invalid", "Missing output");
  *output = {};
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  file_detail::Url url;
  if (!ValidWebPageLimits(limits) || !file_detail::ParseUrl(raw_url, &url) ||
      input.mime_type.size() > 255)
    return Error(400, "web_page_state_invalid", "Invalid page or limits");
  if (input.bytes.size() > limits.max_source_bytes)
    return Error(413, "web_page_capacity", "Web page source limit exceeded");
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Failed(GrammarFailure::kCancelled);
  control.compile_deadline =
      std::min({control.compile_deadline, access.deadline,
                Clock::now() + std::chrono::milliseconds(limits.timeout_ms)});
  if (Clock::now() >= control.compile_deadline)
    return Failed(GrammarFailure::kTimeout);
  const auto first_cancel = control.cancelled;
  control.cancelled = [first_cancel, next = access.cancelled] {
    return (first_cancel && first_cancel()) || (next && next());
  };
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure = GrammarFailure::kNone;
  if (!detail::GrammarProcess::Start("--web-page-worker", control, &process,
                                     &failure))
    return Failed(failure);
  auto request = Object({{"url", String(url.Text())},
                         {"mime_type", String(input.mime_type)},
                         {"bytes", Number(input.bytes.size())},
                         {"limits", Limits(limits)}});
  auto text = OutputJson(request);
  if (text.size() > kControlBytes)
    return Error(413, "web_page_capacity", "Page control limit exceeded");
  Frame frame{kVersion, 1, text.size()}, response;
  auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, control.compile_deadline,
                             control.cancelled);
  };
  if (!transfer(&frame, sizeof(frame), true) ||
      !transfer(text.data(), text.size(), true) ||
      !transfer(const_cast<char*>(input.bytes.data()), input.bytes.size(),
                true) ||
      !transfer(&response, sizeof(response), false))
    return Failed(process->Failure());
  if (response.version != kVersion || response.sequence != 1 ||
      !response.bytes || response.bytes > kReplyBytes)
    return Failed(GrammarFailure::kWorker);
  text.assign(response.bytes, '\0');
  if (!transfer(text.data(), text.size(), false))
    return Failed(process->Failure());
  Json wire;
  if (!Parse(text, &wire)) return Failed(GrammarFailure::kWorker);
  auto reply = DecodeReply(wire);
  if (!reply.ok()) return reply;
  WebPage next;
  if (!Page(reply.value, limits, &next) || next.url != url.Text() ||
      next.source_sha256 != Digest(input.bytes))
    return Failed(GrammarFailure::kWorker);
  if (control.cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= control.compile_deadline)
    return Failed(GrammarFailure::kTimeout);
  *output = std::move(next);
  return {};
}

int MaybeRunWebPageWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--web-page-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
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
    Json value;
    WebPageLimits limits;
    if (!Parse(text, &value) || !value.Has("limits") ||
        !Limits(*value.Find("limits"), &limits))
      return 3;
    const auto bytes = value.GetInt("bytes", -1);
    if (bytes < 0 || static_cast<uint64_t>(bytes) > limits.max_source_bytes)
      return 3;
    FileInputContent input;
    if (!Text(value, "mime_type", 255, &input.mime_type)) return 3;
    const auto runtime = web_detail::LoadPageRuntime(input.mime_type);
    if (!detail::EnterNativeDocumentSandbox()) return 2;
    input.bytes.resize(bytes);
    if (!detail::WorkerTransfer(input.bytes.data(), bytes, false)) return 3;
    WebPage page;
    ObjectReply reply;
    try {
      reply = runtime.ok() ? web_detail::DecodePage(
                                 input, value.GetString("url"), limits, &page)
                           : runtime;
    } catch (const std::bad_alloc&) {
      reply = Error(503, "web_page_memory_capacity", "Web page memory limit");
    } catch (...) {
      reply = Failed(GrammarFailure::kWorker);
    }
    if (reply.ok()) reply.value = Page(page);
    text = OutputJson(WireReply(reply));
    if (text.size() > kReplyBytes) return 3;
    frame = {kVersion, 1, text.size()};
    return detail::WorkerTransfer(&frame, sizeof(frame), true) &&
                   detail::WorkerTransfer(text.data(), text.size(), true)
               ? 0
               : 3;
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
