#include "q4t/server/file_extract.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"

namespace q4t::server {
namespace {
bool AppleMetadata(const FileInputContent& input) {
  const auto dot = input.filename.rfind('.');
  const auto extension =
      dot == input.filename.npos ? "" : http::Lower(input.filename.substr(dot));
  const auto mime = http::Lower(http::Trim(
      std::string_view(input.mime_type).substr(0, input.mime_type.find(';'))));
  return extension == ".pages" || extension == ".numbers" ||
         extension == ".key" || mime == "application/vnd.apple.pages" ||
         mime == "application/vnd.apple.numbers" ||
         mime == "application/vnd.apple.keynote";
}
ObjectReply Decode(const FileInputContent& input,
                   const FileExtractOptions& options, std::string_view detail,
                   const BinaryAccess& access, FileDocument* output,
                   GrammarControl control) {
  const auto format = ClassifyFileDocument(input);
  if (format == FileDocumentFormat::kPdf)
    return ParsePdfFileDocument(input, options.document, options.pdf, detail,
                                access, output, control);
  if (format == FileDocumentFormat::kOfficeBinary)
    return ParseLegacyFileDocument(input, options.document, options.legacy,
                                   access, output, control);
  // Older iWork documents can be a standalone XML or compressed XML stream.
  // A filename only selects a decoder; that decoder validates actual content.
  if (format == FileDocumentFormat::kOfficeZip && AppleMetadata(input) &&
      !std::string_view(input.bytes).starts_with("PK"))
    return ParseLegacyFileDocument(input, options.document, options.legacy,
                                   access, output, control);
  auto reply =
      ParseNativeFileDocument(input, options.document, access, output, control);
  if (reply.code == "file_document_codec_required")
    return ParseLegacyFileDocument(input, options.document, options.legacy,
                                   access, output, control);
  return reply;
}
ObjectReply Capacity() {
  return object_detail::Error(413, "file_document_capacity",
                              "Document tree exceeds extraction capacity");
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return object_detail::Error(499, "request_cancelled",
                                "Extraction cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return object_detail::Error(408, "request_deadline_exceeded",
                                "Extraction timed out");
  return {};
}
struct Remaining {
  uint64_t text = 0, pixels = 0, images = 0, source = kFileInputBytes;
  size_t cells = 0, pages = 0, embedded = 0;
};
bool Consume(uint64_t used, uint64_t* remaining) {
  if (used > *remaining) return false;
  *remaining -= used;
  return true;
}
ObjectReply Charge(const FileDocument& document, Remaining* remaining) {
  if (!Consume(document.text.size(), &remaining->text)) return Capacity();
  for (const auto& sheet : document.sheets)
    for (const auto& row : sheet.rows) {
      if (row.size() > remaining->cells) return Capacity();
      remaining->cells -= row.size();
      for (const auto& cell : row)
        if (!Consume(cell.size(), &remaining->text)) return Capacity();
    }
  if (document.pages.size() > remaining->pages) return Capacity();
  remaining->pages -= document.pages.size();
  for (const auto& page : document.pages)
    if (!Consume(static_cast<uint64_t>(page.width) * page.height,
                 &remaining->pixels) ||
        !Consume(page.png.size(), &remaining->images))
      return Capacity();
  const auto count = document.embedded.size() + document.pending.size();
  if (count > remaining->embedded) return Capacity();
  remaining->embedded -= count;
  return {};
}
ObjectReply Tree(const FileInputContent& input,
                 const FileExtractOptions& options, std::string_view detail,
                 const BinaryAccess& access, GrammarControl control,
                 size_t depth, Remaining* remaining, FileDocument* output) {
  auto reply = Active(access);
  if (!reply.ok()) return reply;
  if (depth > options.document.max_nested_documents || !remaining->text)
    return Capacity();
  auto selected = options;
  selected.document.max_nested_documents -= depth;
  selected.document.max_text_bytes = remaining->text;
  selected.document.max_cells = std::max<size_t>(1, remaining->cells);
  if (ClassifyFileDocument(input) == FileDocumentFormat::kPdf) {
    if (!remaining->pages || !remaining->pixels || !remaining->images)
      return Capacity();
    selected.pdf.max_pages = remaining->pages;
    selected.pdf.max_pixels = remaining->pixels;
    selected.pdf.max_image_bytes = remaining->images;
  }
  FileDocument next;
  reply = Decode(input, selected, detail, access, &next, control);
  if (!reply.ok()) return reply;
  reply = Charge(next, remaining);
  if (!reply.ok()) return reply;
  std::string combined;
  size_t previous = 0;
  for (const auto& pending : next.pending) {
    reply = Active(access);
    if (!reply.ok()) return reply;
    if (!Consume(pending.source.bytes.size(), &remaining->source))
      return Capacity();
    FileDocument child;
    reply = Tree(pending.source, options, detail, access, control,
                 depth + pending.depth, remaining, &child);
    if (!reply.ok()) return reply;
    // Offsets are validated against immutable original text by the IPC reader.
    // Never search for text markers that a document could imitate or collide.
    combined.append(next.text, previous, pending.text_offset - previous);
    auto decoration = [&](std::string_view text) {
      if (!Consume(text.size(), &remaining->text)) return false;
      combined += text;
      return true;
    };
    auto path = object_detail::Array();
    for (const auto& name : pending.path)
      path.array.push_back(object_detail::String(name));
    std::string heading = "\n[Embedded document " +
                          OutputJson(path) + "; SHA256 " +
                          child.source_sha256;
    if (!child.conversion.codec.empty())
      heading += "; converted with " + child.conversion.codec + " " +
                 child.conversion.version;
    heading += "]\n";
    if (!decoration(heading)) return Capacity();
    combined += child.text;
    for (const auto& sheet : child.sheets) {
      if (!decoration("\n[Sheet " + sheet.name + "]\n")) return Capacity();
      for (const auto& row : sheet.rows) {
        for (size_t i = 0; i < row.size(); ++i) {
          if (i && !decoration("\t")) return Capacity();
          combined += row[i];
        }
        if (!decoration("\n")) return Capacity();
      }
    }
    if (!decoration("\n[End embedded document]\n")) return Capacity();
    next.embedded.push_back(
        {pending.path, child.source_sha256, child.conversion});
    for (auto& item : child.embedded) {
      item.path.insert(item.path.begin(), pending.path.begin(),
                       pending.path.end());
      next.embedded.push_back(std::move(item));
    }
    for (auto& page : child.pages) {
      page.source_path.insert(page.source_path.begin(), pending.path.begin(),
                              pending.path.end());
      next.pages.push_back(std::move(page));
    }
    previous = pending.text_offset;
  }
  if (!next.pending.empty()) {
    combined.append(next.text, previous);
    next.text = std::move(combined);
    next.pending.clear();
  }
  reply = Active(access);
  if (!reply.ok()) return reply;
  *output = std::move(next);
  return {};
}
}  // namespace

ObjectReply ExtractFileDocument(const FileInputContent& input,
                                const FileExtractOptions& options,
                                bool pdf_only, std::string_view detail,
                                const BinaryAccess& access,
                                FileDocument* output, GrammarControl control) {
  if (!output)
    return object_detail::Error(500, "file_document_state_invalid",
                                "Missing output");
  *output = {};
  if (!ValidFileDocumentLimits(options.document) || !options.timeout_ms ||
      options.timeout_ms > 120000 || !options.pdf.max_pages ||
      options.pdf.max_pages > 256 || !options.pdf.max_pixels ||
      options.pdf.max_pixels > (256ULL << 20) || !options.pdf.max_image_bytes ||
      options.pdf.max_image_bytes > (256ULL << 20) ||
      options.pdf.timeout_ms <= 0 || options.pdf.timeout_ms > 120000)
    return object_detail::Error(500, "file_document_budget_invalid",
                                "Invalid extraction budget");
  if (pdf_only && ClassifyFileDocument(input) != FileDocumentFormat::kPdf)
    return object_detail::Error(400, "invalid_file_format",
                                "Chat file input requires a PDF");
  auto bounded = access;
  bounded.deadline = std::min(
      access.deadline, std::chrono::steady_clock::now() +
                           std::chrono::milliseconds(options.timeout_ms));
  bounded.cancelled = [&] {
    return (access.cancelled && access.cancelled()) ||
           (control.cancelled && control.cancelled());
  };
  Remaining remaining{options.document.max_text_bytes,
                      options.pdf.max_pixels,
                      options.pdf.max_image_bytes,
                      kFileInputBytes,
                      options.document.max_cells,
                      options.pdf.max_pages,
                      options.document.max_embedded_documents};
  return Tree(input, options, detail, bounded, control, 0, &remaining, output);
}
}  // namespace q4t::server
