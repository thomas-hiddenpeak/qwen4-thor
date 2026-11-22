#include "q4t/server/sip_service.h"

#include <iostream>
#include <string_view>

int SipOwnerFixture(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--sip-owner-fixture") return -1;
  if (argc != 4) return 2;
  q4t::server::SipService service;
  service.Start(argv[2], [scope = std::string(argv[3])](std::string_view id) {
    return id == scope;
  });
  std::string command;
  while (std::cin >> command) {
    if (command == "stop") break;
    if (command == "health") {
      std::cout << service.HealthJson() << std::endl;
      continue;
    }
    std::string scope, session;
    int code, timeout;
    if (command != "reject" ||
        !(std::cin >> scope >> session >> code >> timeout) || timeout < 1 ||
        timeout > 5000)
      return 2;
    auto reply = service.Reject(
        scope, session, code,
        std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout));
    std::cout << reply.http_status << std::endl;
  }
  service.Stop();
  return 0;
}
