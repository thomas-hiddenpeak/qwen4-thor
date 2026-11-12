#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <string_view>

#include "q4t/server/object_store.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/output_schema.h"
#include "q4t/trace/sha256.h"
#include "responses_json.h"

namespace q4t::server::object_detail {
using namespace responses_detail;
inline constexpr uint64_t kMaxFrame = 64ULL << 20;
// A response can publish 4096 input items and up to 512 generated items in
// one commit. The byte, owner and object quotas still bound every transaction.
inline constexpr size_t kMaxTransactionObjects = 8192;
inline constexpr uint64_t kVersion = 1;
struct Frame {
  uint64_t version = kVersion;
  uint64_t sequence = 0;
  uint64_t bytes = 0;
};
static_assert(sizeof(Frame) == 24);

inline std::string Digest(std::string_view text) {
  return trace::HexDigest(trace::Sha256(
      {reinterpret_cast<const uint8_t*>(text.data()), text.size()}));
}
inline bool ValidId(std::string_view id) { return ValidApplicationId(id); }
inline std::string Kind(ObjectKind kind) {
  switch (kind) {
    case ObjectKind::kResponse:
      return "response";
    case ObjectKind::kChat:
      return "chat";
    case ObjectKind::kConversation:
      return "conversation";
    case ObjectKind::kConversationItem:
      return "item";
    case ObjectKind::kFile:
      return "file";
    case ObjectKind::kBatch:
      return "batch";
    case ObjectKind::kToolRun:
      return "tool_run";
    case ObjectKind::kPrompt:
      return "prompt";
    case ObjectKind::kInputSnapshot:
      return "input_snapshot";
    case ObjectKind::kVectorStore:
      return "vector_store";
    case ObjectKind::kVectorStoreFile:
      return "vector_store_file";
  }
  return {};
}
inline bool ValidKind(std::string_view kind) {
  for (int i = 0; i <= static_cast<int>(ObjectKind::kVectorStoreFile); ++i)
    if (kind == Kind(static_cast<ObjectKind>(i))) return true;
  return false;
}
inline std::string Operation(ObjectOperation op) {
  switch (op) {
    case ObjectOperation::kGet:
      return "get";
    case ObjectOperation::kPut:
      return "put";
    case ObjectOperation::kErase:
      return "erase";
    case ObjectOperation::kList:
      return "list";
    case ObjectOperation::kHealth:
      return "health";
  }
  return {};
}
inline ObjectReply Error(int status, std::string code, std::string message,
                         bool uncertain = false) {
  ObjectReply result;
  result.http_status = status;
  result.code = std::move(code);
  result.message = std::move(message);
  result.commit_unknown = uncertain;
  return result;
}
inline Json WireReply(const ObjectReply& reply) {
  return Object({{"status", Number(reply.http_status)},
                 {"code", String(reply.code)},
                 {"message", String(reply.message)},
                 {"commit_unknown", Boolean(reply.commit_unknown)},
                 {"value", reply.value}});
}
inline Status Parse(const std::string& text, Json* result) {
  io::JsonParseLimits limits;
  limits.max_depth = 96;
  limits.max_values = 2 * 1024 * 1024;
  limits.max_string_bytes = kMaxFrame;
  limits.reject_duplicate_keys = true;
  limits.allow_exact_number_text = true;
  return io::ParseJson(text, result, true, limits);
}
inline ObjectReply DecodeReply(const Json& wire) {
  const auto* value = wire.Find("value");
  const auto* code = wire.Find("code");
  const auto* message = wire.Find("message");
  const auto* unknown = wire.Find("commit_unknown");
  if (!wire.IsObject() || !value || !code || !code->IsString() || !message ||
      !message->IsString() || !unknown || !unknown->IsBool() ||
      wire.GetInt("status") < 200 || wire.GetInt("status") > 599)
    return Error(503, "object_transport_error", "Invalid object worker reply");
  ObjectReply result;
  result.value = *value;
  result.http_status = wire.GetInt("status");
  result.code = code->str;
  result.message = message->str;
  result.commit_unknown = unknown->boolean;
  return result;
}
}  // namespace q4t::server::object_detail
