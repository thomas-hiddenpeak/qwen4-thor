#include "q4t/server/file_document_worker.h"

#include <sys/resource.h>

#include <algorithm>
#include <cstring>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
constexpr uint64_t kRequestBytes = 8192;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
bool Native(FileDocumentFormat format) {
  return format == FileDocumentFormat::kText ||
         format == FileDocumentFormat::kCsv ||
         format == FileDocumentFormat::kTsv ||
         format == FileDocumentFormat::kIif;
}
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Document extraction cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Document extraction timed out");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_document_worker_capacity",
                 "Document extraction capacity occupied");
  return Error(503, "file_document_worker_failed",
               "Document extraction worker unavailable");
}
Json Wire(const FileDocument& document) {
  auto sheets = Array();
  for (const auto& sheet : document.sheets) {
    auto rows = Array();
    for (const auto& row : sheet.rows) {
      auto cells = Array();
      for (const auto& cell : row) cells.array.push_back(String(cell));
      rows.array.push_back(std::move(cells));
    }
    sheets.array.push_back(Object({{"name", String(sheet.name)},
                                   {"rows", std::move(rows)},
                                   {"source_rows", Number(sheet.source_rows)},
                                   {"columns", Number(sheet.columns)}}));
  }
  return Object({{"text", String(document.text)},
                 {"encoding", String(document.encoding)},
                 {"source_sha256", String(document.source_sha256)},
                 {"sheets", std::move(sheets)}});
}
bool Decode(const Json& value, const FileInputContent& input,
            const FileDocumentLimits& limits, FileDocument* output) {
  const auto* text = value.Find("text");
  const auto* sheets = value.GetArray("sheets");
  if (!text || !text->IsString() || text->str.size() > limits.max_text_bytes ||
      !sheets || sheets->array.size() > 1 ||
      value.GetString("source_sha256") != Digest(input.bytes))
    return false;
  auto encoding = value.GetString("encoding");
  if (encoding != "utf-8" && encoding != "utf-16le" && encoding != "utf-16be" &&
      encoding != "utf-32le" && encoding != "utf-32be")
    return false;
  const bool is_text = ClassifyFileDocument(input) == FileDocumentFormat::kText;
  if ((is_text && !sheets->array.empty()) ||
      (!is_text && (sheets->array.size() != 1 || !text->str.empty())))
    return false;
  FileDocument next;
  next.text = text->str;
  next.encoding = std::move(encoding);
  next.source_sha256 = value.GetString("source_sha256");
  uint64_t bytes = next.text.size();
  size_t cells = 0;
  for (const auto& item : sheets->array) {
    const auto* rows = item.GetArray("rows");
    const auto source_rows = item.GetInt("source_rows", -1);
    const auto columns = item.GetInt("columns", -1);
    if (!rows || rows->array.size() > FileDocumentLimits::kSheetRows ||
        source_rows < 0 ||
        source_rows > static_cast<int64_t>(input.bytes.size()) + 1 ||
        static_cast<uint64_t>(source_rows) < rows->array.size() ||
        columns < 0 || static_cast<uint64_t>(columns) > limits.max_columns ||
        item.GetString("name") != input.filename)
      return false;
    FileDocumentSheet sheet;
    sheet.name = input.filename;
    sheet.source_rows = source_rows;
    sheet.columns = columns;
    for (const auto& row : rows->array) {
      if (!row.IsArray() || row.array.size() > sheet.columns) return false;
      std::vector<std::string> values;
      for (const auto& cell : row.array) {
        if (!cell.IsString() || cell.str.size() > limits.max_cell_bytes ||
            cell.str.size() > limits.max_text_bytes - bytes ||
            ++cells > limits.max_cells)
          return false;
        bytes += cell.str.size();
        values.push_back(cell.str);
      }
      sheet.rows.push_back(std::move(values));
    }
    next.sheets.push_back(std::move(sheet));
  }
  *output = std::move(next);
  return true;
}
}  // namespace

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetDocumentWorkerEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif

ObjectReply ParseNativeFileDocument(const FileInputContent& input,
                                    const FileDocumentLimits& limits,
                                    const BinaryAccess& access,
                                    FileDocument* output,
                                    GrammarControl control) {
  if (!output)
    return Error(500, "file_document_state_invalid", "Missing output");
  *output = {};
  if (!ValidFileDocumentLimits(limits))
    return Error(500, "file_document_budget_invalid",
                 "Invalid extraction budget");
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (input.bytes.size() > kFileInputBytes || input.filename.size() > 1024 ||
      input.mime_type.size() > 255)
    return Error(413, "file_document_capacity",
                 "Document source metadata or bytes exceed capacity");
  if (!Native(ClassifyFileDocument(input)))
    return Error(500, "file_document_decoder_mismatch",
                 "Document requires its format decoder");
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= access.deadline) return Failed(GrammarFailure::kTimeout);
  control.compile_deadline = std::min(access.deadline, Clock::now() + 10s);
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  const char* entry = "--file-document-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
    return Failed(failure);
  auto request = Object({{"filename", String(input.filename)},
                         {"mime_type", String(input.mime_type)},
                         {"bytes", Number(input.bytes.size())},
                         {"max_text_bytes", Number(limits.max_text_bytes)},
                         {"max_cell_bytes", Number(limits.max_cell_bytes)},
                         {"max_columns", Number(limits.max_columns)},
                         {"max_cells", Number(limits.max_cells)}});
  auto text = OutputJson(request);
  Frame header{kVersion, 1, text.size()}, response;
  auto transfer = [&](void* bytes, size_t size, bool write) {
    return process->Transfer(bytes, size, write, control.compile_deadline,
                             access.cancelled);
  };
  if (text.size() > kRequestBytes)
    return Error(413, "file_document_capacity",
                 "Document control capacity exceeded");
  if (!transfer(&header, sizeof(header), true) ||
      !transfer(text.data(), text.size(), true) ||
      !transfer(const_cast<char*>(input.bytes.data()), input.bytes.size(),
                true) ||
      !transfer(&response, sizeof(response), false))
    return Failed(process->Failure());
  if (response.version != kVersion || response.sequence != 1 ||
      !response.bytes || response.bytes > kMaxFrame)
    return Failed(GrammarFailure::kWorker);
  text.assign(response.bytes, '\0');
  if (!transfer(text.data(), text.size(), false))
    return Failed(process->Failure());
  Json wire;
  if (!Parse(text, &wire)) return Failed(GrammarFailure::kWorker);
  auto reply = DecodeReply(wire);
  if (!reply.ok()) return reply;
  FileDocument next;
  if (!Decode(reply.value, input, limits, &next))
    return Failed(GrammarFailure::kWorker);
  if ((access.cancelled && access.cancelled()) ||
      (control.cancelled && control.cancelled()))
    return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= control.compile_deadline)
    return Failed(GrammarFailure::kTimeout);
  *output = std::move(next);
  return {};
}

int MaybeRunFileDocumentWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--file-document-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{10, 10};
  if (setrlimit(RLIMIT_CPU, &cpu) || !detail::EnterNativeDocumentSandbox())
    return 2;
  try {
    Frame frame;
    if (!detail::WorkerTransfer(&frame, sizeof(frame), false) ||
        frame.version != kVersion || frame.sequence != 1 || !frame.bytes ||
        frame.bytes > kRequestBytes)
      return 3;
    std::string text(frame.bytes, '\0');
    if (!detail::WorkerTransfer(text.data(), text.size(), false)) return 3;
    Json request;
    if (!Parse(text, &request)) return 3;
    const auto bytes = request.GetInt("bytes", -1);
    if (bytes < 0 || static_cast<uint64_t>(bytes) > kFileInputBytes) return 3;
    FileInputContent input;
    input.filename = request.GetString("filename");
    input.mime_type = request.GetString("mime_type");
    if (input.filename.size() > 1024 || input.mime_type.size() > 255) return 3;
    FileDocumentLimits limits;
    limits.max_text_bytes = request.GetInt("max_text_bytes", 0);
    limits.max_cell_bytes = request.GetInt("max_cell_bytes", 0);
    limits.max_columns = request.GetInt("max_columns", 0);
    limits.max_cells = request.GetInt("max_cells", 0);
    if (!ValidFileDocumentLimits(limits)) return 3;
    input.bytes.resize(bytes);
    if (!detail::WorkerTransfer(input.bytes.data(), input.bytes.size(), false))
      return 3;
    FileDocument document;
    ObjectReply reply;
    try {
      const BinaryAccess access{
          "native-document-worker", Clock::now() + 10s, {}};
      reply = ParsePlainFileDocument(input, ClassifyFileDocument(input), limits,
                                     access, &document);
      if (reply.ok()) reply.value = Wire(document);
    } catch (const std::bad_alloc&) {
      reply = Error(503, "file_document_memory_capacity",
                    "Document extraction memory limit");
    } catch (...) {
      reply = Error(503, "file_document_failed", "Document extraction failed");
    }
    text = OutputJson(WireReply(reply));
    if (text.empty() || text.size() > kMaxFrame) return 3;
    frame = {kVersion, 1, text.size()};
    if (!detail::WorkerTransfer(&frame, sizeof(frame), true) ||
        !detail::WorkerTransfer(text.data(), text.size(), true))
      return 3;
    return 0;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
