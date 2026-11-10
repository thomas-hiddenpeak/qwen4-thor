#include "q4t/server/file_snapshot.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Corrupt() {
  return Error(503, "file_snapshot_inconsistent",
               "Stored document input is incomplete or inconsistent");
}
ObjectReply Capacity() {
  return Error(413, "file_document_capacity",
               "Stored document exceeds extraction capacity");
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Document snapshot cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Document snapshot timed out");
  return {};
}
bool Hex(std::string_view text) {
  return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
bool Utf8(const std::string& value, uint64_t max_bytes) {
  return value.size() <= max_bytes && detail::NormalizeUtf8(value) == value;
}
bool Text(const Json& value, const char* name, uint64_t max_bytes,
          std::string* output) {
  const auto* field = value.Find(name);
  if (!field || !field->IsString() || !Utf8(field->str, max_bytes))
    return false;
  *output = field->str;
  return true;
}
bool Path(const std::vector<std::string>& path, size_t depth) {
  if (path.size() > depth) return false;
  for (const auto& part : path) {
    if (part.empty() || !Utf8(part, 1024)) return false;
    for (unsigned char c : part)
      if (c < 32 || c == 127) return false;
  }
  return true;
}
Json PathJson(const std::vector<std::string>& path) {
  auto result = Array();
  for (const auto& part : path) result.array.push_back(String(part));
  return result;
}
bool ReadPath(const Json& value, size_t depth, std::vector<std::string>* path) {
  if (!value.IsArray() || value.array.size() > depth) return false;
  for (const auto& part : value.array) {
    if (!part.IsString()) return false;
    path->push_back(part.str);
  }
  return Path(*path, depth);
}
Json Conversion(const FileDocumentConversion& conversion) {
  return Object({{"codec", String(conversion.codec)},
                 {"product", String(conversion.product)},
                 {"version", String(conversion.version)},
                 {"build_id", String(conversion.build_id)},
                 {"output_format", String(conversion.output_format)},
                 {"output_sha256", String(conversion.output_sha256)}});
}
bool ReadConversion(const Json& value, FileDocumentConversion* conversion) {
  if (!value.IsObject() || value.object.size() != 6 ||
      !Text(value, "codec", 64, &conversion->codec) ||
      !Text(value, "product", 96, &conversion->product) ||
      !Text(value, "version", 64, &conversion->version) ||
      !Text(value, "build_id", 128, &conversion->build_id) ||
      !Text(value, "output_format", 16, &conversion->output_format) ||
      !Text(value, "output_sha256", 64, &conversion->output_sha256))
    return false;
  if (conversion->codec.empty())
    return conversion->product.empty() && conversion->version.empty() &&
           conversion->build_id.empty() && conversion->output_format.empty() &&
           conversion->output_sha256.empty();
  return !conversion->product.empty() && !conversion->version.empty() &&
         !conversion->output_format.empty() && Hex(conversion->output_sha256);
}
Json Source(const FileInputContent& source, size_t part) {
  return Object({{"filename", String(source.filename)},
                 {"mime_type", String(source.mime_type)},
                 {"file_id", String(source.file_id)},
                 {"sha256", String(source.sha256)},
                 {"part", Number(part)}});
}
bool Take(const Json& value, const char* name, InputSnapshot* stored,
          std::vector<bool>* used, std::string* output) {
  const auto index = value.GetInt(name, -1);
  if (index < 0 || static_cast<uint64_t>(index) >= stored->parts.size() ||
      (*used)[index])
    return false;
  (*used)[index] = true;
  *output = std::move(stored->parts[index]);
  return true;
}
bool ReadSource(const Json& value, InputSnapshot* stored,
                std::vector<bool>* used, FileInputContent* source) {
  return value.IsObject() && value.object.size() == 5 &&
         Text(value, "filename", 1024, &source->filename) &&
         Text(value, "mime_type", 255, &source->mime_type) &&
         Text(value, "file_id", 256, &source->file_id) &&
         Text(value, "sha256", 64, &source->sha256) &&
         Take(value, "part", stored, used, &source->bytes);
}
bool Consume(uint64_t bytes, uint64_t* remaining) {
  if (bytes > *remaining) return false;
  *remaining -= bytes;
  return true;
}
ObjectReply CheckLimits(const FileExtractOptions& options) {
  if (!ValidFileDocumentLimits(options.document) || !options.pdf.max_pages ||
      options.pdf.max_pages > 256 || !options.pdf.max_pixels ||
      options.pdf.max_pixels > (256ULL << 20) || !options.pdf.max_image_bytes ||
      options.pdf.max_image_bytes > (256ULL << 20))
    return Error(500, "file_document_budget_invalid", "Invalid file budget");
  return {};
}
ObjectReply Validate(const FileInputSnapshot& input,
                     const FileExtractOptions& options) {
  auto reply = CheckLimits(options);
  if (!reply.ok()) return reply;
  const auto& limits = options.document;
  if (input.detail != "auto" && input.detail != "low" && input.detail != "high")
    return Corrupt();
  uint64_t source_bytes = kFileInputBytes;
  for (const auto* source :
       {&input.source.content, input.source.indirect_source
                                   ? &*input.source.indirect_source
                                   : nullptr}) {
    if (!source) continue;
    if (!Consume(source->bytes.size(), &source_bytes)) return Capacity();
    if (!Utf8(source->filename, 1024) || !Utf8(source->mime_type, 255) ||
        (!source->file_id.empty() && !ValidId(source->file_id)) ||
        source->sha256 != Digest(source->bytes))
      return Corrupt();
  }
  if (input.source.urls.size() > 12 || !Utf8(input.source.export_format, 16))
    return Corrupt();
  for (const auto& url : input.source.urls)
    if (url.empty() || !Utf8(url, 8192)) return Corrupt();
  const auto& document = input.document;
  if (!document.pending.empty() ||
      document.source_sha256 != input.source.content.sha256)
    return Corrupt();
  if (document.encoding != "utf-8" && document.encoding != "utf-16le" &&
      document.encoding != "utf-16be" && document.encoding != "utf-32le" &&
      document.encoding != "utf-32be")
    return Corrupt();
  uint64_t text = limits.max_text_bytes;
  if (!Consume(document.text.size(), &text) ||
      document.sheets.size() > limits.max_sheets ||
      document.pages.size() > options.pdf.max_pages ||
      document.embedded.size() > limits.max_embedded_documents)
    return Capacity();
  if (!Utf8(document.text, limits.max_text_bytes)) return Corrupt();
  size_t cells = limits.max_cells;
  for (const auto& sheet : document.sheets) {
    if (!Utf8(sheet.name, 1024) || sheet.rows.size() > sheet.source_rows)
      return Corrupt();
    if (sheet.columns > limits.max_columns ||
        sheet.rows.size() > FileDocumentLimits::kSheetRows)
      return Capacity();
    for (const auto& row : sheet.rows) {
      if (row.size() > sheet.columns) return Corrupt();
      if (row.size() > cells) return Capacity();
      cells -= row.size();
      for (const auto& cell : row) {
        if (cell.size() > limits.max_cell_bytes || !Consume(cell.size(), &text))
          return Capacity();
        if (!Utf8(cell, limits.max_cell_bytes)) return Corrupt();
      }
    }
  }
  FileDocumentConversion conversion;
  if (!ReadConversion(Conversion(document.conversion), &conversion))
    return Corrupt();
  for (const auto& embedded : document.embedded) {
    if (embedded.path.empty() ||
        !Path(embedded.path, limits.max_nested_documents) ||
        !Hex(embedded.source_sha256) ||
        !ReadConversion(Conversion(embedded.conversion), &conversion))
      return Corrupt();
  }
  uint64_t pixels = options.pdf.max_pixels,
           images = options.pdf.max_image_bytes;
  const uint32_t edge = input.detail == "low"    ? 512
                        : input.detail == "high" ? 2048
                                                 : 1536;
  for (const auto& page : document.pages) {
    if (!Consume(page.png.size(), &images) ||
        !Consume(uint64_t{page.width} * page.height, &pixels))
      return Capacity();
    vision::MediaHeader header;
    const auto bytes = std::span(
        reinterpret_cast<const uint8_t*>(page.png.data()), page.png.size());
    constexpr std::string_view end("\0\0\0\0IEND\xae\x42\x60\x82", 12);
    if (!page.number || page.number > 256 || page.width > edge ||
        page.height > edge ||
        !Path(page.source_path, limits.max_nested_documents) ||
        !vision::ReadMediaHeader(bytes, &header) ||
        header.format != vision::MediaFormat::kPng ||
        header.width != page.width || header.height != page.height ||
        !page.png.ends_with(end))
      return Corrupt();
    if (page.source_path.empty()) {
      if (ClassifyFileDocument(input.source.content) !=
          FileDocumentFormat::kPdf)
        return Corrupt();
    } else if (std::none_of(document.embedded.begin(), document.embedded.end(),
                            [&](const auto& item) {
                              return item.path == page.source_path;
                            })) {
      return Corrupt();
    }
  }
  return {};
}
Json Pack(const FileInputSnapshot& input,
          std::vector<std::string_view>* parts) {
  const auto add = [&](std::string_view part) {
    parts->push_back(part);
    return parts->size() - 1;
  };
  const auto& document = input.document;
  auto source = Source(input.source.content, add(input.source.content.bytes));
  Json indirect;
  if (input.source.indirect_source)
    indirect = Source(*input.source.indirect_source,
                      add(input.source.indirect_source->bytes));
  auto urls = Array(), sheets = Array(), pages = Array(), embedded = Array();
  for (const auto& url : input.source.urls) urls.array.push_back(String(url));
  for (const auto& sheet : document.sheets) {
    auto rows = Array();
    for (const auto& row : sheet.rows) {
      auto cells = Array();
      for (const auto& cell : row) cells.array.push_back(String(cell));
      rows.array.push_back(std::move(cells));
    }
    sheets.array.push_back(Object({{"name", String(sheet.name)},
                                   {"source_rows", Number(sheet.source_rows)},
                                   {"columns", Number(sheet.columns)},
                                   {"rows", std::move(rows)}}));
  }
  for (const auto& page : document.pages)
    pages.array.push_back(Object({{"number", Number(page.number)},
                                  {"width", Number(page.width)},
                                  {"height", Number(page.height)},
                                  {"source_path", PathJson(page.source_path)},
                                  {"part", Number(add(page.png))}}));
  for (const auto& item : document.embedded)
    embedded.array.push_back(
        Object({{"path", PathJson(item.path)},
                {"source_sha256", String(item.source_sha256)},
                {"conversion", Conversion(item.conversion)}}));
  return Object({{"format", String("q4t.file_input_snapshot.v1")},
                 {"detail", String(input.detail)},
                 {"source", std::move(source)},
                 {"indirect_source", std::move(indirect)},
                 {"urls", std::move(urls)},
                 {"export_format", String(input.source.export_format)},
                 {"text", String(document.text)},
                 {"encoding", String(document.encoding)},
                 {"source_sha256", String(document.source_sha256)},
                 {"conversion", Conversion(document.conversion)},
                 {"embedded", std::move(embedded)},
                 {"sheets", std::move(sheets)},
                 {"pages", std::move(pages)}});
}
ObjectReply Unpack(InputSnapshot* stored, const FileExtractOptions& limits,
                   FileInputSnapshot* output) {
  const auto& value = stored->metadata;
  const auto* source = value.Find("source");
  const auto* indirect = value.Find("indirect_source");
  const auto* urls = value.GetArray("urls");
  const auto* sheets = value.GetArray("sheets");
  const auto* pages = value.GetArray("pages");
  const auto* embedded = value.GetArray("embedded");
  const auto* conversion = value.Find("conversion");
  auto& document = output->document;
  std::vector<bool> used(stored->parts.size());
  if (const auto* text = value.Find("text");
      text && text->IsString() &&
      text->str.size() > limits.document.max_text_bytes)
    return Capacity();
  if (value.object.size() != 13 ||
      value.GetString("format") != "q4t.file_input_snapshot.v1" || !source ||
      !indirect || !urls || !sheets || !pages || !embedded || !conversion ||
      !Text(value, "detail", 8, &output->detail) ||
      !Text(value, "export_format", 16, &output->source.export_format) ||
      !Text(value, "text", limits.document.max_text_bytes, &document.text) ||
      !Text(value, "encoding", 16, &document.encoding) ||
      !Text(value, "source_sha256", 64, &document.source_sha256) ||
      !ReadSource(*source, stored, &used, &output->source.content) ||
      !ReadConversion(*conversion, &document.conversion))
    return Corrupt();
  if (!indirect->IsNull()) {
    output->source.indirect_source.emplace();
    if (!ReadSource(*indirect, stored, &used, &*output->source.indirect_source))
      return Corrupt();
  }
  if (urls->array.size() > 12 ||
      sheets->array.size() > limits.document.max_sheets ||
      pages->array.size() > limits.pdf.max_pages ||
      embedded->array.size() > limits.document.max_embedded_documents)
    return Capacity();
  for (const auto& url : urls->array) {
    if (!url.IsString() || !Utf8(url.str, 8192)) return Corrupt();
    output->source.urls.push_back(url.str);
  }
  size_t cells = limits.document.max_cells;
  uint64_t text = limits.document.max_text_bytes - document.text.size();
  for (const auto& entry : sheets->array) {
    FileDocumentSheet sheet;
    const auto rows = entry.GetInt("source_rows", -1),
               columns = entry.GetInt("columns", -1);
    const auto* values = entry.GetArray("rows");
    if (!Text(entry, "name", 1024, &sheet.name) || !values || rows < 0 ||
        columns < 0)
      return Corrupt();
    if (static_cast<uint64_t>(columns) > limits.document.max_columns ||
        values->array.size() > FileDocumentLimits::kSheetRows)
      return Capacity();
    sheet.source_rows = rows;
    sheet.columns = columns;
    for (const auto& row : values->array) {
      if (!row.IsArray()) return Corrupt();
      if (row.array.size() > cells) return Capacity();
      cells -= row.array.size();
      sheet.rows.emplace_back();
      for (const auto& cell : row.array) {
        if (!cell.IsString()) return Corrupt();
        if (cell.str.size() > limits.document.max_cell_bytes ||
            !Consume(cell.str.size(), &text))
          return Capacity();
        sheet.rows.back().push_back(cell.str);
      }
    }
    document.sheets.push_back(std::move(sheet));
  }
  for (const auto& entry : embedded->array) {
    FileDocumentEmbedded item;
    const auto* path = entry.Find("path");
    const auto* conversion = entry.Find("conversion");
    if (!path || !conversion ||
        !ReadPath(*path, limits.document.max_nested_documents, &item.path) ||
        !Text(entry, "source_sha256", 64, &item.source_sha256) ||
        !ReadConversion(*conversion, &item.conversion))
      return Corrupt();
    document.embedded.push_back(std::move(item));
  }
  for (const auto& entry : pages->array) {
    FileDocumentPage page;
    const auto number = entry.GetInt("number", -1),
               width = entry.GetInt("width", -1),
               height = entry.GetInt("height", -1);
    const auto* path = entry.Find("source_path");
    if (number < 1 || number > 256 || width < 1 || width > 2048 || height < 1 ||
        height > 2048 || !path ||
        !ReadPath(*path, limits.document.max_nested_documents,
                  &page.source_path) ||
        !Take(entry, "part", stored, &used, &page.png))
      return Corrupt();
    page.number = number;
    page.width = width;
    page.height = height;
    document.pages.push_back(std::move(page));
  }
  if (std::find(used.begin(), used.end(), false) != used.end())
    return Corrupt();
  return Validate(*output, limits);
}
}  // namespace

ObjectReply SaveFileInputSnapshot(
    InputSnapshotStore& store, const BinaryAccess& access,
    std::string_view binding_sha256, const FileInputSnapshot& input,
    const FileExtractOptions& extraction, const InputSnapshotLimits& storage,
    int hold_seconds, InputSnapshotRef* reference, InputSnapshotHold* hold) {
  auto reply = Active(access);
  if (reply.ok()) reply = Validate(input, extraction);
  if (!reply.ok()) return reply;
  std::vector<std::string_view> parts;
  auto metadata = Pack(input, &parts);
  return store.Save(access, binding_sha256, metadata, parts, storage,
                    hold_seconds, reference, hold);
}
ObjectReply ReadFileInputSnapshot(InputSnapshotStore& store,
                                  const BinaryAccess& access,
                                  const InputSnapshotRef& reference,
                                  std::string_view binding_sha256,
                                  const FileExtractOptions& extraction,
                                  const InputSnapshotLimits& storage,
                                  FileInputSnapshot* output) {
  if (!output) return Corrupt();
  *output = {};
  auto reply = CheckLimits(extraction);
  if (!reply.ok()) return reply;
  InputSnapshot stored;
  reply = store.Read(access, reference, binding_sha256, storage, &stored);
  if (!reply.ok()) return reply;
  FileInputSnapshot next;
  reply = Unpack(&stored, extraction, &next);
  if (reply.ok()) reply = Active(access);
  if (reply.ok()) *output = std::move(next);
  return reply;
}
}  // namespace q4t::server
