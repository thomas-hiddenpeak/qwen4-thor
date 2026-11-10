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
struct FilePackageLimits {
  uint64_t max_bytes = 128ULL << 20;
  uint64_t max_entry_bytes = 32ULL << 20;
  size_t max_entries = 4096;
  size_t max_xml_nodes = 1000000;
  size_t max_xml_depth = 64;
};
struct FileDocumentLimits {
  uint64_t max_text_bytes = 4ULL << 20;
  uint64_t max_cell_bytes = 1ULL << 20;
  size_t max_columns = 1024;
  size_t max_cells = 131072;
  size_t max_sheets = 256;
  size_t max_nested_documents = 4;
  size_t max_embedded_documents = 32;
  uint64_t timeout_ms = 10000;
  FilePackageLimits package;
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
struct FileDocumentPage {
  uint32_t number = 0, width = 0, height = 0;
  std::string png;
  // Empty for the outer file. Each component is a containing package member,
  // never a host path; page number remains relative to its source PDF.
  std::vector<std::string> source_path{};
};
struct FileDocumentConversion {
  std::string codec, product, version, build_id;
  std::string output_format, output_sha256;
};
struct FileDocumentEmbedded {
  std::vector<std::string> path;
  std::string source_sha256;
  FileDocumentConversion conversion;
};
struct FileDocumentPending {
  size_t text_offset = 0, depth = 0;
  std::vector<std::string> path;
  FileInputContent source;
};
struct FileDocument {
  std::string text, encoding, source_sha256;
  std::vector<FileDocumentSheet> sheets;
  std::vector<FileDocumentPage> pages;
  // Empty codec means no system Office conversion. Converted formula values
  // must not be presented as the original binary document's cached results.
  FileDocumentConversion conversion;
  std::vector<FileDocumentEmbedded> embedded;
  // Private intermediate output from the memory-only parser. The unified
  // extractor resolves all entries before publishing a complete document.
  std::vector<FileDocumentPending> pending;
};
bool ValidFileDocumentLimits(const FileDocumentLimits& limits);

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
