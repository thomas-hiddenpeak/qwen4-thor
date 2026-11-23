// Parent of one isolated native RTP media worker, independent of model
// libraries.
#pragma once

#include <array>
#include <functional>
#include <stop_token>
#include <string_view>

#include "q4t/server/object_store.h"

namespace q4t::server {
class LiveMediaProcess {
 public:
  LiveMediaProcess() = default;
  ~LiveMediaProcess();
  LiveMediaProcess(const LiveMediaProcess&) = delete;
  LiveMediaProcess& operator=(const LiveMediaProcess&) = delete;
  ObjectReply Negotiate(std::string_view offer, const std::string& address,
                        const std::function<bool()>& cancelled,
                        std::string* answer,
                        const std::string* sip_peer = nullptr,
                        const std::string& worker_sha256 = {});
  bool Send(char type, std::string_view payload);
  bool Drain(std::stop_token stop);
  bool ActivateSip(const std::function<bool()>& cancelled);
  enum class Read { kPacket, kPending, kClosed };
  Read Receive(char* type, std::string_view* payload);
  int Fd() const { return fd_; }

 private:
  int fd_ = -1, pid_ = -1, slot_ = -1;
  std::array<char, 65536> buffer_;
};
}  // namespace q4t::server
