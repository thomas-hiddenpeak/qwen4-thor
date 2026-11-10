#include "q4t/server/file_document.h"

#include <algorithm>

#include "file_package.h"
#include "object_protocol.h"
#include "q4t/server/http_framing.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
ObjectReply Active(const BinaryAccess& access) {
  if (Clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Document parsing timed out");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Document parsing cancelled");
  return {};
}
ObjectReply Invalid() {
  return Error(400, "file_document_invalid",
               "Invalid document encoding or structure");
}
ObjectReply Capacity() {
  return Error(413, "file_document_capacity",
               "Document exceeds extraction budget");
}
bool Valid(const FileDocumentLimits& limits) {
  return limits.max_text_bytes > 0 && limits.max_text_bytes <= (16ULL << 20) &&
         limits.max_cell_bytes > 0 && limits.max_cell_bytes <= (4ULL << 20) &&
         limits.max_columns > 0 && limits.max_columns <= 16384 &&
         limits.max_cells > 0 && limits.max_cells <= 1000000 &&
         limits.max_sheets > 0 && limits.max_sheets <= 256 &&
         limits.max_nested_documents <= 4 && limits.timeout_ms > 0 &&
         limits.timeout_ms <= 120000 &&
         file_detail::ValidPackageLimits(limits.package);
}
bool Scalar(uint32_t cp) {
  return cp && cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff) &&
         (cp >= 32 || cp == '\t' || cp == '\n' || cp == '\r' || cp == '\f');
}
void Utf8(uint32_t cp, std::string* output) {
  if (cp < 0x80)
    output->push_back(cp);
  else if (cp < 0x800) {
    output->push_back(0xc0 | (cp >> 6));
    output->push_back(0x80 | (cp & 63));
  } else if (cp < 0x10000) {
    output->push_back(0xe0 | (cp >> 12));
    output->push_back(0x80 | ((cp >> 6) & 63));
    output->push_back(0x80 | (cp & 63));
  } else {
    output->push_back(0xf0 | (cp >> 18));
    output->push_back(0x80 | ((cp >> 12) & 63));
    output->push_back(0x80 | ((cp >> 6) & 63));
    output->push_back(0x80 | (cp & 63));
  }
}
ObjectReply Decode(std::string_view bytes, uint64_t limit,
                   const BinaryAccess& access, std::string* text,
                   std::string* encoding) {
  int width = 1;
  bool little = false;
  *encoding = "utf-8";
  if (bytes.starts_with(std::string_view("\xff\xfe\0\0", 4))) {
    width = 4;
    little = true;
    *encoding = "utf-32le";
    bytes.remove_prefix(4);
  } else if (bytes.starts_with(std::string_view("\0\0\xfe\xff", 4))) {
    width = 4;
    *encoding = "utf-32be";
    bytes.remove_prefix(4);
  } else if (bytes.starts_with("\xff\xfe")) {
    width = 2;
    little = true;
    *encoding = "utf-16le";
    bytes.remove_prefix(2);
  } else if (bytes.starts_with("\xfe\xff")) {
    width = 2;
    *encoding = "utf-16be";
    bytes.remove_prefix(2);
  } else if (bytes.starts_with("\xef\xbb\xbf"))
    bytes.remove_prefix(3);
  text->reserve(std::min<uint64_t>(limit, bytes.size()));
  auto unit = [&](size_t offset) {
    uint32_t result = 0;
    for (int i = 0; i < width; ++i) {
      const auto byte = static_cast<unsigned char>(bytes[offset + i]);
      result |= static_cast<uint32_t>(byte)
                << (8 * (little ? i : width - 1 - i));
    }
    return result;
  };
  size_t checked = 0;
  for (size_t offset = 0; offset < bytes.size();) {
    if (offset >= checked) {
      auto active = Active(access);
      if (!active.ok()) return active;
      checked = offset + 4096;
    }
    uint32_t cp;
    if (width == 1) {
      const auto first = static_cast<unsigned char>(bytes[offset++]);
      int follow = 0;
      uint32_t minimum = 0;
      if (first < 0x80)
        cp = first;
      else if (first >= 0xc2 && first <= 0xdf) {
        cp = first & 31;
        follow = 1;
        minimum = 0x80;
      } else if (first >= 0xe0 && first <= 0xef) {
        cp = first & 15;
        follow = 2;
        minimum = 0x800;
      } else if (first >= 0xf0 && first <= 0xf4) {
        cp = first & 7;
        follow = 3;
        minimum = 0x10000;
      } else
        return Invalid();
      if (static_cast<size_t>(follow) > bytes.size() - offset) return Invalid();
      for (int i = 0; i < follow; ++i) {
        const auto byte = static_cast<unsigned char>(bytes[offset++]);
        if ((byte & 0xc0) != 0x80) return Invalid();
        cp = (cp << 6) | (byte & 63);
      }
      if (cp < minimum) return Invalid();
    } else {
      if (bytes.size() - offset < static_cast<size_t>(width)) return Invalid();
      cp = unit(offset);
      offset += width;
      if (width == 2 && cp >= 0xd800 && cp <= 0xdbff) {
        if (bytes.size() - offset < 2) return Invalid();
        const auto low = unit(offset);
        if (low < 0xdc00 || low > 0xdfff) return Invalid();
        offset += 2;
        cp = 0x10000 + ((cp - 0xd800) << 10) + low - 0xdc00;
      }
    }
    if (!Scalar(cp)) return Invalid();
    const uint64_t count = cp < 0x80      ? 1
                           : cp < 0x800   ? 2
                           : cp < 0x10000 ? 3
                                          : 4;
    if (count > limit - text->size()) return Capacity();
    Utf8(cp, text);
  }
  return Active(access);
}

ObjectReply Delimited(std::string_view text, char delimiter,
                      const FileDocumentLimits& limits,
                      const BinaryAccess& access, FileDocumentSheet* sheet) {
  bool quoted = false, closed = false, began = false;
  uint64_t cell_bytes = 0, total_bytes = 0;
  size_t columns = 0, cells = 0, checked = 0;
  std::string cell;
  std::vector<std::string> row;
  auto retain = [&] {
    return sheet->source_rows < FileDocumentLimits::kSheetRows;
  };
  auto append = [&](char c) {
    if (++cell_bytes > limits.max_cell_bytes) return false;
    if (retain()) {
      if (total_bytes == limits.max_text_bytes) return false;
      ++total_bytes;
      cell.push_back(c);
    }
    return true;
  };
  auto end_cell = [&] {
    if (++columns > limits.max_columns) return false;
    if (retain()) {
      if (++cells > limits.max_cells) return false;
      row.push_back(std::move(cell));
    }
    cell.clear();
    cell_bytes = 0;
    began = closed = false;
    return true;
  };
  auto end_row = [&] {
    sheet->columns = std::max(sheet->columns, columns);
    if (retain()) sheet->rows.push_back(std::move(row));
    row.clear();
    columns = 0;
    ++sheet->source_rows;
  };
  for (size_t i = 0; i < text.size(); ++i) {
    if (i >= checked) {
      auto active = Active(access);
      if (!active.ok()) return active;
      checked = i + 4096;
    }
    const char c = text[i];
    if (quoted) {
      if (c == '"') {
        if (i + 1 < text.size() && text[i + 1] == '"') {
          ++i;
          if (!append('"')) return Capacity();
        } else {
          quoted = false;
          closed = true;
        }
      } else if (!append(c))
        return Capacity();
    } else if (c == delimiter) {
      if (!end_cell()) return Capacity();
    } else if (c == '\r' || c == '\n') {
      if (c == '\r' && i + 1 < text.size() && text[i + 1] == '\n') ++i;
      if (!end_cell()) return Capacity();
      end_row();
    } else if (c == '"' && !began && !closed) {
      quoted = began = true;
    } else {
      if (closed || c == '"') return Invalid();
      began = true;
      if (!append(c)) return Capacity();
    }
  }
  if (quoted) return Invalid();
  if (began || closed || columns) {
    if (!end_cell()) return Capacity();
    end_row();
  }
  return Active(access);
}
}  // namespace

bool ValidFileDocumentLimits(const FileDocumentLimits& limits) {
  return Valid(limits);
}

FileDocumentFormat ClassifyFileDocument(const FileInputContent& input) {
  const auto bytes = std::string_view(input.bytes);
  if (bytes.starts_with("%PDF-")) return FileDocumentFormat::kPdf;
  if (bytes.starts_with("PK\x03\x04") || bytes.starts_with("PK\x05\x06"))
    return FileDocumentFormat::kOfficeZip;
  if (bytes.starts_with("\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"))
    return FileDocumentFormat::kOfficeBinary;
  if (bytes.starts_with("{\\rtf")) return FileDocumentFormat::kRtf;
  const auto dot = input.filename.rfind('.');
  const auto extension =
      dot == input.filename.npos ? "" : http::Lower(input.filename.substr(dot));
  const auto mime = http::Lower(http::Trim(
      std::string_view(input.mime_type).substr(0, input.mime_type.find(';'))));
  if (extension == ".pdf" || mime == "application/pdf")
    return FileDocumentFormat::kPdf;
  if (extension == ".docx" || extension == ".docm" || extension == ".dotx" ||
      extension == ".dotm" || extension == ".pptx" || extension == ".pptm" ||
      extension == ".potx" || extension == ".potm" || extension == ".ppsx" ||
      extension == ".ppsm" || extension == ".ppam" || extension == ".xlsx" ||
      extension == ".xlsm" || extension == ".xltx" || extension == ".xltm" ||
      extension == ".xlam" || extension == ".xlsb" ||
      extension == ".odt" || extension == ".odp" || extension == ".ods" ||
      extension == ".pages" || extension == ".key" || extension == ".numbers" ||
      mime ==
          "application/"
          "vnd.openxmlformats-officedocument.wordprocessingml.document" ||
      mime ==
          "application/"
          "vnd.openxmlformats-officedocument.presentationml.presentation" ||
      mime ==
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" ||
      mime == "application/vnd.oasis.opendocument.text" ||
      mime == "application/vnd.oasis.opendocument.presentation" ||
      mime == "application/vnd.oasis.opendocument.spreadsheet")
    return FileDocumentFormat::kOfficeZip;
  if (extension == ".doc" || extension == ".dot" || extension == ".ppt" ||
      extension == ".pot" || extension == ".ppa" || extension == ".pps" ||
      extension == ".pwz" || extension == ".wiz" || extension == ".xls" ||
      extension == ".xla" || extension == ".xlb" || extension == ".xlc" ||
      extension == ".xlm" || extension == ".xlt" || extension == ".xlw" ||
      mime == "application/msword" || mime == "application/vnd.ms-powerpoint" ||
      mime == "application/vnd.ms-excel")
    return FileDocumentFormat::kOfficeBinary;
  if (extension == ".rtf" || mime == "application/rtf" || mime == "text/rtf")
    return FileDocumentFormat::kRtf;
  if (extension == ".csv" || mime == "text/csv")
    return FileDocumentFormat::kCsv;
  if (extension == ".tsv" || mime == "text/tab-separated-values")
    return FileDocumentFormat::kTsv;
  if (extension == ".iif") return FileDocumentFormat::kIif;
  return FileDocumentFormat::kText;
}
ObjectReply ParsePlainFileDocument(const FileInputContent& input,
                                   FileDocumentFormat format,
                                   const FileDocumentLimits& limits,
                                   const BinaryAccess& access,
                                   FileDocument* output) {
  if (!output)
    return Error(500, "file_document_state_invalid", "Missing document output");
  *output = {};
  if (!Valid(limits))
    return Error(500, "file_document_budget_invalid",
                 "Invalid extraction budget");
  if (input.bytes.size() > kFileInputBytes) return Capacity();
  const bool sheet = format == FileDocumentFormat::kCsv ||
                     format == FileDocumentFormat::kTsv ||
                     format == FileDocumentFormat::kIif;
  if (format != FileDocumentFormat::kText && !sheet)
    return Error(500, "file_document_decoder_mismatch",
                 "Document requires its format decoder");
  if (ClassifyFileDocument(input) != format) return Invalid();
  auto reply = Active(access);
  if (!reply.ok()) return reply;
  FileDocument next;
  std::string text;
  reply = Decode(input.bytes, sheet ? kFileInputBytes : limits.max_text_bytes,
                 access, &text, &next.encoding);
  if (!reply.ok()) return reply;
  if (sheet) {
    FileDocumentSheet table;
    table.name = input.filename;
    reply = Delimited(text, format == FileDocumentFormat::kCsv ? ',' : '\t',
                      limits, access, &table);
    if (!reply.ok()) return reply;
    next.sheets.push_back(std::move(table));
  } else
    next.text = std::move(text);
  next.source_sha256 = Digest(input.bytes);
  reply = Active(access);
  if (!reply.ok()) return reply;
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server
