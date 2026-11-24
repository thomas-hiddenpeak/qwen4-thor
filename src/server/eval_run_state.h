#pragma once

#include "eval_runs.h"
#include "object_protocol.h"

namespace q4t::server::eval_detail {
using object_detail::Json;
inline ObjectRequest Read(ObjectKind kind, const StateAccess& access,
                          std::string_view id) {
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = kind;
  request.id = id;
  return request;
}
inline ObjectRequest Update(ObjectKind kind, const StateAccess& access,
                            const Json& record) {
  auto request = Read(kind, access, record.GetString("id"));
  request.operation = ObjectOperation::kPut;
  request.expected_revision = record.GetInt("revision");
  request.expires_at = record.GetInt("expires_at");
  if (const auto* data = record.Find("data")) request.data = *data;
  return request;
}
inline ObjectReply BadInput(std::string_view field, std::string text) {
  using namespace object_detail;
  auto result = Error(400, "invalid_request", std::move(text));
  result.value = Object({{"param", String(std::string(field))}});
  return result;
}
inline ObjectReply NotFound() {
  return object_detail::Error(404, "object_not_found",
                              "Evaluation object not found");
}
inline ObjectReply InvalidRecord() {
  return object_detail::Error(503, "eval_record_invalid",
                              "Invalid evaluation state");
}
inline ObjectReply Shape(const Json& value, const char* root) {
  ChatPlanError e;
  auto status = ValidateEval(value, root, &e);
  if (status) return {};
  if (e.http_status == 503)
    return object_detail::Error(503, e.code, status.message());
  return BadInput(e.param, status.message());
}
}  // namespace q4t::server::eval_detail
