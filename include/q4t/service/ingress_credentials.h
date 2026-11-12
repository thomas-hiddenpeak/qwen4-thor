// One owned certificate reader; file I/O never runs in the serving loop.
#pragma once

#include <atomic>
#include <memory>
#include <string>

#include "q4t/io/json.h"
#include "q4t/service/guardian_process.h"
#include "q4t/service/ingress_tls.h"

namespace q4t::service::ingress {

struct TlsFiles {
  std::string certificate;
  std::string key;
  std::string ca;
  std::string crl;
  std::string lease;
  std::string name;
  bool client_certificate = false;
};

class Credentials {
 public:
  explicit Credentials(TlsFiles files);
  ~Credentials();
  void Step(bool reload);
  std::shared_ptr<TlsContext> Context() const { return context_.load(); }
  io::Json Status() const;
  bool FinishedInitial() const { return attempted_ && pid_ < 0; }

 private:
  void Start();
  void Fail(const char* reason);
  TlsFiles files_;
  std::atomic<std::shared_ptr<TlsContext>> context_;
  std::shared_ptr<TlsContext> candidate_;
  OwnedFd socket_;
  OwnedFd process_;
  pid_t pid_ = -1;
  TlsDeadline deadline_;
  bool attempted_ = false;
  bool received_ = false;
  bool killed_ = false;
  unsigned generation_ = 0;
  uint64_t attempt_ = 0;
  std::string error_;
};

// The private exec mode inherits only a seqpacket FD and its parent PID.
int CredentialReader(int fd, pid_t parent);

}  // namespace q4t::service::ingress
