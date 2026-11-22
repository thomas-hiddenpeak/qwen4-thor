#include "q4t/server/sip_service.h"

#include "q4t/server/live_media_process.h"

#include <iostream>
#include <string_view>

int SipOwnerFixture(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--sip-owner-fixture") return -1;
  if (argc != 4) return 2;
  q4t::server::SipService service;
  service.Start(argv[2], [scope = std::string(argv[3])](std::string_view id) {
    return id == scope;
  });
  std::unique_ptr<q4t::server::SipInvitation> call;
  std::unique_ptr<q4t::server::LiveMediaProcess> media;
  std::string command;
  while (std::cin >> command) {
    if (command == "stop") break;
    if (command == "health") {
      std::cout << service.HealthJson() << std::endl;
      continue;
    }
    if (command == "release") {
      media.reset();
      call.reset();
      std::cout << 200 << std::endl;
      continue;
    }
    std::string scope, session;
    if (command == "accept") {
      int timeout;
      if (!(std::cin >> scope >> session >> timeout) || timeout < 1 ||
          timeout > 5000)
        return 2;
      std::unique_ptr<q4t::server::SipInvitation> ticket;
      auto worker = std::make_unique<q4t::server::LiveMediaProcess>();
      std::string answer;
      auto reply = service.Reserve(scope, session, &ticket);
      if (reply.ok())
        reply = worker->Negotiate(
            ticket->offer, ticket->local_address, [] { return false; }, &answer,
            &ticket->peer_address);
      if (reply.ok())
        reply = ticket->Answer(answer, std::chrono::steady_clock::now() +
                                           std::chrono::milliseconds(timeout));
      if (reply.ok()) {
        if (!worker->ActivateSip([] { return false; })) {
          std::cout << 503 << std::endl;
          continue;
        }
        call = std::move(ticket);
        media = std::move(worker);
      }
      std::cout << reply.http_status << std::endl;
      continue;
    }
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
