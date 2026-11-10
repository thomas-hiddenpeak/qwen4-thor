// Immutable document input, shared by all generation and history consumers.
#pragma once

#include "q4t/server/file_extract.h"
#include "q4t/server/file_source.h"
#include "q4t/server/input_snapshot.h"

namespace q4t::server {
struct FileInputSnapshot {
  FileSourceReceipt source;
  FileDocument document;
  std::string detail = "auto";
};
// The binding is a canonical original part/profile identity from trusted
// planning. Private URLs and original bytes never become public object fields.
// Raw source and PNG sections remain binary, not a giant base64 JSON record.
ObjectReply SaveFileInputSnapshot(
    InputSnapshotStore& store, const BinaryAccess& access,
    std::string_view binding_sha256, const FileInputSnapshot& input,
    const FileExtractOptions& extraction, const InputSnapshotLimits& storage,
    int hold_seconds, InputSnapshotRef* reference, InputSnapshotHold* hold);
ObjectReply ReadFileInputSnapshot(InputSnapshotStore& store,
                                  const BinaryAccess& access,
                                  const InputSnapshotRef& reference,
                                  std::string_view binding_sha256,
                                  const FileExtractOptions& extraction,
                                  const InputSnapshotLimits& storage,
                                  FileInputSnapshot* output);
}  // namespace q4t::server
