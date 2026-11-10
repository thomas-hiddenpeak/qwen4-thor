// Shared source resolution before document extraction and immutable snapshots.
#pragma once

#include <optional>

#include "q4t/server/file_fetch.h"

namespace q4t::server {
struct FileSourceReceipt {
  FileInputContent content;
  // For a Google shortcut, retain its original bytes/identity separately from
  // the actual exported document. All URLs are private provenance, never logs.
  std::optional<FileInputContent> indirect_source;
  std::vector<std::string> urls;
  std::string export_format;
};
struct GoogleDocumentExport {
  std::string url, format, kind;
};
// Pure URL conversion. Unrelated or already-explicit export URLs stay intact.
// Only standard HTTPS docs.google.com view/edit links are adapted.
ObjectReply PlanGoogleDocumentExport(std::string_view url,
                                     GoogleDocumentExport* output);
ObjectReply ResolveFileInput(ObjectStore* objects, const BinaryAccess& access,
                             const FileInputSource& source,
                             const FileFetchOptions& fetch,
                             FileInputBudget* budget, FileSourceReceipt* output,
                             GrammarControl control = {});
}  // namespace q4t::server
