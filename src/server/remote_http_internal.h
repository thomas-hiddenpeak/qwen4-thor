// Reuses file acquisition's validated numeric endpoints and verified TLS.
// Call only in an independently owned worker: DNS/library I/O may block.
#pragma once

#include <map>
#include <memory>

#include "file_fetch_internal.h"

namespace q4t::server::file_detail {
using RemoteHeaders = std::map<std::string, std::string>;

// One request/response; no redirect, retry, proxy, pooling or credential store.
// Incremental reads permit MCP SSE without waiting for the peer to close.
class RemoteHttp {
 public:
  ~RemoteHttp();
  RemoteHttp(const RemoteHttp&) = delete;
  RemoteHttp& operator=(const RemoteHttp&) = delete;
  static ObjectReply Open(const Url& url, const FileFetchOptions& options,
                          std::string_view method, const RemoteHeaders& headers,
                          std::string_view body, uint64_t max_bytes,
                          std::chrono::steady_clock::time_point deadline,
                          std::unique_ptr<RemoteHttp>* output);
  // A bounded native tunnel completion reuses the same MCP JSON/SSE reader.
  // This creates no socket, performs no DNS and accepts no redirects.
  static ObjectReply Buffered(int status, const RemoteHeaders& headers,
                              std::string body,
                              std::unique_ptr<RemoteHttp>* output);
  int Status() const;
  std::string_view Mime() const;
  std::string_view Header(std::string_view name) const;
  bool HasHeader(std::string_view name) const;
  // Keep an existing legacy SSE connection while bounding the next operation.
  // Begin a new bounded read without discarding buffered framing or bytes.
  void BeginRead(std::chrono::steady_clock::time_point deadline);
  ObjectReply Read(std::string* bytes, bool* eof);
  ObjectReply ReadAll(std::string* bytes);

 private:
  struct Impl;
  explicit RemoteHttp(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};
}  // namespace q4t::server::file_detail
