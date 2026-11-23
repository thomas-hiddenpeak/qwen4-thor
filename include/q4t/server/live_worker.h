#pragma once

#include <string>
#include <string_view>

namespace q4t::server {

inline bool ValidWorkerDigest(std::string_view digest) {
  return digest.size() == 64 &&
         digest.find_first_not_of("0123456789abcdef") == digest.npos;
}

struct LiveWorkerPins {
  std::string webrtc, sip, rtp;

  bool Parse(std::string_view value) {
    if (value == "none") {
      webrtc = sip = rtp = "none";
      return true;
    }
    if (value.size() != 194 || value[64] != ',' || value[129] != ',')
      return false;
    auto a = value.substr(0, 64);
    auto b = value.substr(65, 64);
    auto c = value.substr(130);
    if (!ValidWorkerDigest(a) || !ValidWorkerDigest(b) || !ValidWorkerDigest(c))
      return false;
    webrtc = a;
    sip = b;
    rtp = c;
    return true;
  }
};

// Called only in the new auxiliary process, before initializing the model.
int RunPinnedLiveWorker(int argc, char** argv);

}  // namespace q4t::server
