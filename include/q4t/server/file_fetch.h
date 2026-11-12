// Native HTTP(S) acquisition in an independently owned, bounded process.
#pragma once

#include <string>
#include <vector>

#include "q4t/server/file_input.h"
#include "q4t/server/grammar_process.h"

namespace q4t::server {
// Deployment-owned policy. No request field can override this policy, forward
// credentials, select a proxy, or select a CA/module path.
struct FileFetchOptions {
  bool public_networks = true;
  bool allow_http = true;
  std::vector<std::string> allowed_networks;
  std::vector<std::string> allowed_hosts;
  std::vector<int> ports{80, 443};
  std::string ca_file;
  int timeout_ms = 15000;
  int max_redirects = 5;
};
struct FileFetchReceipt {
  FileInputContent content;
  // Private provenance; URL queries may contain secrets. Never log these.
  std::vector<std::string> urls;
};
ObjectReply ParseFileFetchOptions(const io::Json& value,
                                  FileFetchOptions* options);
ObjectReply FetchFileInput(const BinaryAccess& access,
                           const FileInputSource& source,
                           const FileFetchOptions& options,
                           FileInputBudget* budget, FileFetchReceipt* result,
                           GrammarControl control = {});
int MaybeRunFileFetchWorker(int argc, char** argv);
}  // namespace q4t::server
