// Unified document dispatch for all file-consuming API paths.
#pragma once

#include "q4t/server/file_legacy.h"
#include "q4t/server/file_pdf.h"

namespace q4t::server {
struct FileExtractOptions {
  uint64_t timeout_ms = 30000;
  FileDocumentLimits document;
  FilePdfOptions pdf;
  FileLegacyOptions legacy;
  // Retrieval ingestion requires complete rows even in embedded documents.
  // Ordinary file-input callers keep their documented bounded row window.
  bool require_complete_rows = false;
};
// Signature-led routing, complete extraction, and original byte identity.
// Caller-owned options are deployment policy, never fields in a file part.
ObjectReply ExtractFileDocument(const FileInputContent& input,
                                const FileExtractOptions& options,
                                bool pdf_only, std::string_view detail,
                                const BinaryAccess& access,
                                FileDocument* output,
                                GrammarControl control = {});
}  // namespace q4t::server
