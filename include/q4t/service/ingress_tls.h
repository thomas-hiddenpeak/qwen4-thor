// Optional system TLS, no SSL/crypto linkage in a plaintext process.
#pragma once

#include <sys/types.h>

#include <chrono>
#include <memory>
#include <string>
#include <string_view>

namespace q4t::service::ingress {

using TlsDeadline = std::chrono::steady_clock::time_point;

struct TlsMaterial {
  std::string certificate;
  std::string key;
  std::string ca;
  std::string crl;
};

class TlsStream;
class TlsContext : public std::enable_shared_from_this<TlsContext> {
 public:
  struct Impl;
  ~TlsContext();
  static std::shared_ptr<TlsContext> Create(const TlsMaterial& material,
                                            const std::string& name,
                                            bool client_certificate,
                                            std::string* error);
  bool ValidNow() const;
  const std::string& Fingerprint() const;
  int64_t ExpiresAt() const;
  bool ChecksRevocation() const;
  std::unique_ptr<TlsStream> Stream(int fd);

 private:
  friend class TlsStream;
  explicit TlsContext(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};

class TlsStream {
 public:
  struct Impl;
  ~TlsStream();
  bool Accept(TlsDeadline deadline);
  ssize_t Read(void* buffer, size_t size, TlsDeadline deadline);
  bool Write(std::string_view bytes, TlsDeadline deadline, bool* started);
  // Consume only TLS control records. EOF or unexpected HTTP data cancels the
  // single in-flight request; a TLS 1.3 key update alone must not cancel SSE.
  bool Cancelled();
  short PollEvents() const;

 private:
  friend class TlsContext;
  explicit TlsStream(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};

}  // namespace q4t::service::ingress
