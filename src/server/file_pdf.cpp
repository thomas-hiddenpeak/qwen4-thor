#include "q4t/server/file_pdf.h"

#include <sys/wait.h>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <utility>

#include "file_pdf_protocol.h"
#include "object_protocol.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
namespace {
using object_detail::Digest;
using object_detail::Error;
using namespace pdf_detail;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "PDF extraction cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded", "PDF extraction timed out");
  if (failure == GrammarFailure::kResource)
    return Error(413, "file_document_capacity", "PDF output capacity exceeded");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_document_worker_capacity",
                 "Document extraction capacity occupied");
  return Error(503, "file_pdf_worker_failed", "PDF extraction worker failed");
}
ObjectReply Run(const FileInputContent& input, Operation operation,
                uint64_t page, uint64_t edge, size_t max_bytes,
                const BinaryAccess& access, GrammarControl control,
                std::string* output) {
  output->clear();
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= access.deadline) return Failed(GrammarFailure::kTimeout);
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline = access.deadline;
  GrammarWorkerReport report;
  auto finished = control.worker_finished;
  control.worker_finished = [&](const auto& value) {
    report = value;
    if (finished) finished(value);
  };
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  const char* entry = "--file-pdf-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
    return Failed(failure);
  Request request{kMagic, operation, input.bytes.size(), page, edge};
  auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, access.deadline,
                             access.cancelled);
  };
  Ready ready{};
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(input.bytes.data()), input.bytes.size(),
                true) ||
      !transfer(&ready, sizeof(ready), false))
    return Failed(process->Failure());
  if (ready == Ready::kDependency)
    return Error(503, "file_pdf_dependency_unavailable",
                 "PDF codec or input isolation unavailable");
  if (ready == Ready::kSandbox)
    return Error(503, "file_pdf_isolation_unavailable",
                 "PDF codec isolation unavailable");
  if (ready != Ready::kReady) return Failed(GrammarFailure::kWorker);
  if (control.worker_ready) control.worker_ready();
  if (!process->CollectOutput(output, max_bytes, access.deadline,
                              access.cancelled)) {
    if (report.reaped && WIFEXITED(report.exit_status) &&
        WEXITSTATUS(report.exit_status) == 127)
      return Error(503, "file_pdf_dependency_unavailable",
                   "PDF codec cannot execute");
    if (process->Failure() == GrammarFailure::kWorker && report.reaped &&
        WIFEXITED(report.exit_status) && WEXITSTATUS(report.exit_status) > 0)
      return Error(400, "invalid_file_document", "PDF could not be decoded");
    return Failed(process->Failure());
  }
  return {};
}
bool Pages(std::string_view text, uint64_t* output) {
  size_t found = 0;
  while (!text.empty()) {
    const auto end = text.find('\n');
    auto line = text.substr(0, end);
    if (line.starts_with("Pages:")) {
      if (++found != 1) return false;
      line.remove_prefix(6);
      while (!line.empty() && (line.front() == ' ' || line.front() == '\t'))
        line.remove_prefix(1);
      while (!line.empty() && (line.back() == ' ' || line.back() == '\r'))
        line.remove_suffix(1);
      const auto parsed =
          std::from_chars(line.data(), line.data() + line.size(), *output);
      if (line.empty() || parsed.ec != std::errc{} ||
          parsed.ptr != line.data() + line.size() || !*output)
        return false;
    }
    if (end == std::string_view::npos) break;
    text.remove_prefix(end + 1);
  }
  return found == 1;
}
}  // namespace

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetPdfWorkerEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif

ObjectReply ParsePdfFileDocument(const FileInputContent& input,
                                 const FileDocumentLimits& limits,
                                 const FilePdfOptions& options,
                                 std::string_view detail,
                                 const BinaryAccess& access,
                                 FileDocument* output, GrammarControl control) {
  if (!output)
    return Error(500, "file_document_state_invalid", "Missing output");
  *output = {};
  if (!ValidFileDocumentLimits(limits) || !options.max_pages ||
      options.max_pages > 256 || !options.max_pixels ||
      options.max_pixels > (256ULL << 20) || !options.max_image_bytes ||
      options.max_image_bytes > (256ULL << 20) || options.timeout_ms <= 0 ||
      options.timeout_ms > 120000)
    return Error(500, "file_document_budget_invalid",
                 "Invalid PDF extraction budget");
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (detail != "auto" && detail != "low" && detail != "high")
    return Error(400, "invalid_file_detail", "Invalid PDF detail");
  if (input.bytes.size() > kFileInputBytes)
    return Error(413, "file_document_capacity", "PDF input capacity exceeded");
  if (input.bytes.empty() ||
      ClassifyFileDocument(input) != FileDocumentFormat::kPdf)
    return Error(400, "invalid_file_document", "Expected PDF document");
  auto bounded = access;
  bounded.deadline =
      std::min(access.deadline,
               Clock::now() + std::chrono::milliseconds(options.timeout_ms));
  const uint64_t edge = detail == "low" ? 512 : detail == "high" ? 2048 : 1536;
  std::string raw;
  auto reply =
      Run(input, Operation::kInfo, 0, 0, 65536, bounded, control, &raw);
  if (!reply.ok()) return reply;
  uint64_t pages = 0;
  if (!Pages(raw, &pages))
    return Error(400, "invalid_file_document", "PDF page count is ambiguous");
  if (pages > options.max_pages)
    return Error(413, "file_document_capacity", "PDF page capacity exceeded");
  reply = Run(input, Operation::kText, 0, 0, limits.max_text_bytes, bounded,
              control, &raw);
  if (!reply.ok()) return reply;
  FileDocument next;
  FileInputContent extracted;
  extracted.filename = "extracted.txt";
  extracted.bytes = std::move(raw);
  // Validate emitted UTF-8 using the native parser; preserve exact text and
  // never replace invalid code points or silently remove content.
  reply = ParsePlainFileDocument(extracted, FileDocumentFormat::kText, limits,
                                 bounded, &next);
  if (!reply.ok()) return reply;
  next.source_sha256 = Digest(input.bytes);
  uint64_t image_bytes = 0, pixels = 0;
  for (uint64_t page = 1; page <= pages; ++page) {
    reply = Run(
        input, Operation::kPage, page, edge,
        std::min<uint64_t>(16ULL << 20, options.max_image_bytes - image_bytes),
        bounded, control, &raw);
    if (!reply.ok()) return reply;
    const auto bytes =
        std::span(reinterpret_cast<const uint8_t*>(raw.data()), raw.size());
    vision::MediaHeader header;
    // EOF + zero exit certifies decoder completion; header and terminal chunk
    // guard transport shape. Actual compressed pixels are decoded by media
    // admission before inference, not inferred from metadata here.
    constexpr std::string_view end("\0\0\0\0IEND\xae\x42\x60\x82", 12);
    if (!vision::ReadMediaHeader(bytes, &header) ||
        header.format != vision::MediaFormat::kPng || !raw.ends_with(end) ||
        header.width > edge || header.height > edge)
      return Error(503, "file_pdf_worker_failed", "Invalid rendered PDF page");
    // The decoder's intermediate raster is separately bounded by edge^2
    // (at most 2048^2). Charge retained pages by their actual pixel count.
    const uint64_t page_pixels = uint64_t{header.width} * header.height;
    if (page_pixels > options.max_pixels - pixels)
      return Error(413, "file_document_capacity",
                   "PDF pixel capacity exceeded");
    pixels += page_pixels;
    image_bytes += raw.size();
    next.pages.push_back({static_cast<uint32_t>(page), header.width,
                          header.height, std::move(raw)});
  }
  if ((bounded.cancelled && bounded.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= bounded.deadline) return Failed(GrammarFailure::kTimeout);
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server
