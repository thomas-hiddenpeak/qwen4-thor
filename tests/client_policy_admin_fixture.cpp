// CPU-only transport fixture. stdin/stdout controls are never production APIs.
#include <unistd.h>

#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>

#include "q4t/io/json.h"
#include "q4t/server/client_policy_admin.h"
#include "q4t/server/sequence_admission.h"

int main(int argc, char** argv) {
  using namespace q4t::server;
  if (argc != 4) return 2;
  std::string raw;
  if (!std::getline(std::cin, raw) || raw.size() > kClientPolicyBytes) return 2;
  GatewayClientPolicy policy;
  ClientAdmission admission;
  if (!ParseGatewayClientPolicy(raw, &policy) || !admission.Configure(policy))
    return 2;
  const auto token = policy.gateway_token;
  SequenceAdmissionQueue queue;
  if (policy.fair_queue) queue.ConfigureClients(policy.clients.size());
  std::mutex mutex;
  const std::string mode = argv[3];
  auto publish = [&](GatewayClientPolicy candidate, uint64_t expected) {
    if (mode == "throw" && candidate.revision == 1)
      throw std::runtime_error("CPU publication exception");
    return PublishClientPolicy(admission, queue, mutex, std::move(candidate),
                               expected, 256, 256);
  };
  std::unique_ptr<ClientPolicyAdmin> admin;
  if (mode == "environment") {
    admin = StartClientPolicyAdmin(admission, 256, 256, publish);
  } else {
    admin = std::make_unique<ClientPolicyAdmin>(admission, 256, 256, publish);
    q4t::service::ProcessRecord parent;
    if (!q4t::service::ReadProcess(std::stoi(argv[2]), &parent) ||
        !admin->Open(argv[1], parent.identity))
      return 2;
    admin->Start();
  }
  std::cout << (admin ? admin->Describe() : "null") << std::endl;
  std::unique_lock held(mutex, std::defer_lock);
  std::unique_ptr<ClientRateLease> lease;
  while (std::getline(std::cin, raw) && raw.size() <= 1024) {
    q4t::io::Json command;
    if (!q4t::io::ParseJson(raw, &command)) return 2;
    const auto op = command.GetString("op");
    bool ok = true;
    if (op == "lock" && !held.owns_lock())
      held.lock();
    else if (op == "unlock" && held.owns_lock())
      held.unlock();
    else if (op == "stop")
      admin.reset();
    else if (op == "reserve" && !lease) {
      auto pending = std::make_unique<ClientRateLease>();
      ok =
          pending->Reserve(
              admission, admission.Authenticate(command.GetString("id"), token),
              3, 5) == ClientRateResult::kAllowed;
      if (ok) {
        pending->Start();
        lease = std::move(pending);
      }
    } else if (op == "settle" && lease) {
      lease->Completion(2);
      lease.reset();
    } else if (op != "status")
      ok = false;
    const auto snapshot = admission.Snapshot();
    const auto usage = admission.RateUsage(0);
    std::cout << "{\"ok\":" << (ok ? "true" : "false")
              << ",\"revision\":" << snapshot->revision
              << ",\"requests\":" << usage.requests
              << ",\"reserved\":" << usage.reserved
              << ",\"tokens\":" << usage.tokens << "}" << std::endl;
  }
  return 0;
}
