#pragma once

#include "q4t/server/file_document.h"

namespace q4t::server::file_detail {
// Preload system character conversions in the child, before the pure-memory
// sandbox. No macros, fields, fonts, files or document code are executed.
ObjectReply LoadRtfRuntime();
ObjectReply ParseRtfDocument(const FileInputContent& input,
                             const FileDocumentLimits& limits,
                             const BinaryAccess& access, FileDocument* output);
}  // namespace q4t::server::file_detail
