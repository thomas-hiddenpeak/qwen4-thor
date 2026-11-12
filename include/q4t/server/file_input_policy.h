// Deployment-owned, immutable file preparation policy. No request overrides.
#pragma once

#include "q4t/server/file_expansion.h"

namespace q4t::server {
struct FileInputPolicy {
  FileExpansionOptions options;
  std::string sha256;
};
// Failure leaves output unchanged. Parsing never opens a codec, CA or URL.
Status ParseFileInputPolicy(std::string_view raw, FileInputPolicy* output);
Status LoadFileInputPolicy(const std::string& path, FileInputPolicy* output);
bool ValidFileExpansionOptions(const FileExpansionOptions& options);
// Public numeric limits and redacted network configuration; no paths/hosts.
io::Json FileInputPolicyDescription(const FileExpansionOptions& options,
                                    std::string_view sha256 = {});
// CPU-only CLI, also used to seal packages with the engine's actual parser.
int RunFileInputPolicyCheck(int argc, char** argv);
}  // namespace q4t::server
