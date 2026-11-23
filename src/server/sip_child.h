#pragma once

#include <sys/types.h>

#include <string>
#include <vector>

namespace q4t::server {

// Direct, unreaped child is the only process this owner may terminate.
class SipChild {
 public:
  ~SipChild();
  SipChild() = default;
  SipChild(const SipChild&) = delete;
  SipChild& operator=(const SipChild&) = delete;
  bool Start(const std::string& program, std::vector<std::string> args,
             int webhook_fd = -1);
  bool Ready(const std::string& expected);
  bool Send(const std::string& message) const;
  int Fd() const { return fd_; }
  void CloseChannel();

 private:
  int fd_ = -1;
  pid_t pid_ = -1;
};
}  // namespace q4t::server
