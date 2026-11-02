// CPU-only acceptance preflight. No tokenizer, weights, CUDA, or listener.
#include <cstdio>
#include <string_view>
#include <vector>

#include "q4t/server/server_options.h"
#include "q4t/server/session_cache.h"

int main(int argc, char** argv) {
  std::vector<std::string_view> args;
  for (int i = 1; i < argc; ++i) args.emplace_back(argv[i]);
  q4t::server::ServerOptions options;
  auto status = q4t::server::ParseServerOptions(args, &options);
  if (!status) {
    std::fprintf(stderr, "options: %s\n", status.message().c_str());
    return 2;
  }
  q4t::server::SessionCache cache;
  if (!options.session_cache_dir.empty()) {
    status = cache.Open(options.session_cache_dir);
    if (!status) {
      std::fprintf(stderr, "session cache: %s\n", status.message().c_str());
      return 3;
    }
  }
  // This driver never uses a shared prefix pool. Refuse a future expansion
  // instead of silently treating an unchecked directory as ready.
  if (!options.prefix_cache_dir.empty()) return 4;
  std::printf("{\"options_valid\":true,\"cache_opened\":%s,"
              "\"max_seq\":%d,\"max_len\":%d,\"mtp\":%s}\n",
              cache.Enabled() ? "true" : "false", options.max_seq,
              options.max_len, options.no_mtp ? "false" : "true");
}
