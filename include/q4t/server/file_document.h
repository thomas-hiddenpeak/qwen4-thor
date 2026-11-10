// Document extraction contracts. Extracted data is untrusted model input.
#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/file_input.h"

namespace q4t::server {
enum class FileDocumentFormat {
  kText,
  kCsv,
  kTsv,
  kIif,
  kPdf,
  kOfficeZip,
  kOfficeBinary,
  kRtf
};
struct FileDocumentLimits {
  uint64_t max_text_bytes = 4ULL << 20;
  uint64_t max_cell_bytes = 1ULL << 20;
  size_t max_columns = 1024;
  size_t max_cells = 131072;
  // The standard file-input spreadsheet contract consumes the first 1000
  // rows per sheet. This limit is not a substitute for generated summaries.
  static constexpr size_t kSheetRows = 1000;
};
struct FileDocumentSheet {
  std::string name;
  std::vector<std::vector<std::string>> rows;
  uint64_t source_rows = 0;
  size_t columns = 0;
};
struct FileDocument {
  std::string text, encoding, source_sha256;
  std::vector<FileDocumentSheet> sheets;
};

// Classification never opens a filename or executes a decoder. Container and
// PDF signatures override misleading filename metadata.
FileDocumentFormat ClassifyFileDocument(const FileInputContent& input);
// Native text/code and delimited sheets only. The dispatcher must route
// PDF/Office/RTF to their independent decoders, not silently read them as text.
ObjectReply ParsePlainFileDocument(const FileInputContent& input,
                                   FileDocumentFormat format,
                                   const FileDocumentLimits& limits,
                                   const BinaryAccess& access,
                                   FileDocument* output);
}  // namespace q4t::server
