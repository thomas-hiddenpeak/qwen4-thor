#pragma once

#include "file_package.h"
#include "q4t/server/file_document.h"

namespace q4t::server::file_detail {
// Inside the isolated document worker, after LoadPackageRuntime and sandbox.
// Format routing uses the actual package structure, not a filename shortcut.
ObjectReply ParseOfficePackage(const FileInputContent& input,
                               const FileDocumentLimits& limits,
                               const PackageLimits& package_limits,
                               const BinaryAccess& access,
                               FileDocument* output);
}  // namespace q4t::server::file_detail
