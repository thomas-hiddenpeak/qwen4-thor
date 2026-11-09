// CPU contract checker; never loads a model or emits transport credentials.
#include <array>
#include <iostream>
#include <string>

#include "q4t/server/client_policy.h"

int main(int argc, char**) {
  using namespace q4t::server;
  if (argc != 1) return 2;
  std::array<char, kClientPolicyBytes + 1> raw{};
  std::cin.read(raw.data(), raw.size());
  const auto bytes = std::cin.gcount();
  if (bytes <= 0 || bytes > static_cast<std::streamsize>(kClientPolicyBytes) ||
      std::cin.bad())
    return 2;
  GatewayClientPolicy policy;
  if (!ParseGatewayClientPolicy(std::string_view(raw.data(), bytes), &policy))
    return 1;
  std::cout << "{\"version\":" << policy.version
            << ",\"revision\":" << policy.revision
            << ",\"clients\":" << policy.clients.size() << ",\"sha256\":\""
            << policy.sha256 << "\",\"content_sha256\":\""
            << ClientPolicyDigest(policy) << "\"}\n";
  return 0;
}
