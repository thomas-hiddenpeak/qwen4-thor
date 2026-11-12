// Native operator-defined execution policies. These are not cloud approval.
#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {
struct ChatPlanConfig;
struct ChatPlanError;
struct GatewayClientPolicy;

struct AccessProgramGrant {
  std::string id;
  std::string level;
};
struct AccessProgramPolicy {
  std::string model;
  std::string tier;
  std::string minimum_grant;
  bool default_standard = false;
  bool standard_allowed = true;
  std::string instructions;
  std::string sha256;
  std::vector<AccessProgramGrant> clients;
};
struct AccessProgramSelection {
  io::Json selected;
  io::Json provenance;
  std::string instructions;
};

// Transactional, bounded and exact; the immutable file is read at startup.
Status ParseAccessProgramPolicy(std::string_view raw,
                                AccessProgramPolicy* output);
Status LoadAccessProgramPolicy(const std::string& path,
                               AccessProgramPolicy* output);
Status BindAccessProgramPolicy(const AccessProgramPolicy& policy,
                               const std::string& actual_model,
                               const GatewayClientPolicy* clients);
Status ResolveAccessProgram(const io::Json& request,
                            const ChatPlanConfig& config,
                            AccessProgramSelection* output,
                            ChatPlanError* error);
void AddAccessProgramInstructions(const AccessProgramSelection& selection,
                                  io::Json* messages);
io::Json AccessProgramCapabilities(const AccessProgramPolicy* policy);
}  // namespace q4t::server
