#pragma once

#include <chrono>
#include <string>
#include <string_view>

#include "q4t/server/file_fetch.h"
#include "q4t/service/ingress_address.h"

namespace q4t::server::file_detail {
struct Url {
  std::string scheme, host, authority, target;
  int port = 0;
  bool tls = false;
  std::string Text() const { return scheme + "://" + authority + target; }
};
bool ParseUrl(std::string_view value, Url* out);
bool Redirect(const Url& from, std::string_view location, Url* out);
bool AllowedAddress(const service::ingress::Address& address,
                    const FileFetchOptions& options);
bool AllowedUrl(const Url& url, const FileFetchOptions& options);
bool ValidOptions(const FileFetchOptions& options);
io::Json OptionsJson(const FileFetchOptions& options);
ObjectReply Fetch(const Url& url, const FileFetchOptions& options,
                  uint64_t max_bytes,
                  std::chrono::steady_clock::time_point deadline,
                  FileFetchReceipt* result);
}  // namespace q4t::server::file_detail
