// Unified document dispatch for all file-consuming API paths.
#pragma once

#include "q4t/server/file_legacy.h"
#include "q4t/server/file_pdf.h"

namespace q4t::server {
struct FileExtractOptions {
  FileDocumentLimits document;
  FilePdfOptions pdf;
  FileLegacyOptions legacy;
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
