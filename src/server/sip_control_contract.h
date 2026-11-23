// Both SDK families use the same SIP status codes and Refer-To syntax.
#pragma once

#include "q4t/server/chat_plan.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/sip_refer_target.h"

namespace q4t::server {
// Required fields and additional properties are checked by each API's schema.
inline Status CheckSipRejectCode(const io::Json& body, ChatPlanError* error) {
  const auto* code = body.Find("status_code");
  if (!code || (IsInputInteger(*code) && code->number >= 300 &&
                code->number <= 699))
    return {};
  if (error) *error = {"invalid_request", "status_code", 400};
  return Status::Fail("Expected a SIP rejection code from 300 to 699");
}

inline Status CheckSipReferTarget(const io::Json& body, ChatPlanError* error) {
  if (ValidReferTarget(body.GetString("target_uri"))) return {};
  if (error) *error = {"invalid_request", "target_uri", 400};
  return Status::Fail("Expected an absolute transfer URI");
}
}  // namespace q4t::server
