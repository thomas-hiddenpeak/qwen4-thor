#include "q4t/server/batch_checkpoint.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/batch_state.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Hash(std::string_view text) {
  return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
bool Payload(const Json& value, Json* decoded) {
  const auto* text = value.Find("payload");
  if (!value.IsObject() || !text || !text->IsString() || text->str.empty() ||
      text->str.size() > kBatchInlineBytes ||
      value.GetString("sha256") != Digest(text->str))
    return false;
  io::JsonParseLimits limits;
  limits.max_depth = 96;
  limits.max_values = kBatchInlineBytes;
  limits.max_string_bytes = kBatchInlineBytes;
  limits.reject_duplicate_keys = true;
  limits.allow_exact_number_text = true;
  return io::ParseJson(text->str, decoded, true, limits).ok() &&
         decoded->IsObject();
}
bool Same(const Json& left, const Json& right, const std::string& key) {
  const auto* a = left.Find(key);
  const auto* b = right.Find(key);
  return (!a && !b) || (a && b && OutputJson(*a) == OutputJson(*b));
}
ObjectReply Immutable() {
  return Error(409, "batch_checkpoint_immutable",
               "Published Batch inputs and results cannot be rewritten");
}
}  // namespace

bool IsBatchPage(const Json& data) {
  const auto format = data.GetString("format");
  return format == "q4t.batch_page.v1" || format == "q4t.batch_page.v2";
}
bool ValidBatchInlineInput(const Json& value) {
  Json payload;
  if (value.object.size() != 5 ||
      value.GetString("format") != "q4t.batch_inline_input.v1" ||
      !Hash(value.GetString("scope_sha256")) ||
      !Hash(value.GetString("binding_sha256")) || !Payload(value, &payload))
    return false;
  // External dependencies keep the original immutable root/hold protocol.
  // Inline inputs never drop a child snapshot's lifetime or reference checks.
  const auto* dependencies = payload.Find("input_snapshots");
  return !dependencies ||
         (dependencies->IsArray() && dependencies->array.empty());
}
bool ValidBatchInlineResult(const Json& row) {
  const auto* value = row.Find("result_inline");
  Json payload;
  if (!value || value->object.size() != 2 || !Payload(*value, &payload))
    return false;
  const auto text = value->GetString("payload");
  if (text.back() != '\n' || text.find('\n') != text.size() - 1 ||
      text.find('\r') != text.npos ||
      Digest(payload.GetString("custom_id")) !=
          row.GetString("custom_id_sha256") ||
      !ValidId(payload.GetString("id")))
    return false;
  const bool success = row.GetString("status") == "completed";
  if (!success && row.GetString("status") != "failed") return false;
  const auto* response = payload.Find("response");
  const auto* error = payload.Find("error");
  if (!response || !error) return false;
  if (error->IsObject())
    return !success && response->IsNull() && error->Find("code") &&
           error->Find("code")->IsString() && error->Find("message") &&
           error->Find("message")->IsString();
  const auto code = response->GetInt("status_code");
  const auto* body = response->Find("body");
  return error->IsNull() && response->IsObject() && body && body->IsObject() &&
         code >= 200 && code <= 599 && (code < 300 || code >= 400) &&
         success == (code < 300) &&
         response->GetString("request_id") == row.GetString("request_id") &&
         !row.GetString("request_id").empty();
}
ObjectReply CheckBatchPageMutation(const Json& data, const Json* previous) {
  if (!IsBatchPage(data) && (!previous || !IsBatchPage(*previous))) return {};
  const auto* rows = data.GetArray("rows");
  if (!IsBatchPage(data) || !rows || rows->array.empty() ||
      rows->array.size() > kBatchPageRows ||
      !ValidId(data.GetString("batch_id")))
    return Immutable();
  for (const auto& row : rows->array) {
    if (!row.IsObject()) return Immutable();
    if (const auto* input = row.Find("prepared_inline")) {
      if (data.GetString("format") != "q4t.batch_page.v2" ||
          row.Has("input_snapshots") || !ValidBatchInlineInput(*input))
        return Immutable();
    }
    if (row.Has("result_inline") &&
        (data.GetString("format") != "q4t.batch_page.v2" ||
         !row.Find("result_id") || !row.Find("result_id")->IsNull() ||
         !ValidBatchInlineResult(row)))
      return Immutable();
  }
  if (!previous) return {};
  const auto* old_rows = previous->GetArray("rows");
  if (!IsBatchPage(*previous) || !old_rows ||
      rows->array.size() < old_rows->array.size() ||
      !Same(data, *previous, "batch_id") ||
      (previous->GetString("format") == "q4t.batch_page.v2" &&
       data.GetString("format") != "q4t.batch_page.v2"))
    return Immutable();
  for (size_t i = 0; i < old_rows->array.size(); ++i) {
    const auto& old = old_rows->array[i];
    const auto& next = rows->array[i];
    for (const auto* key : {"line", "offset", "wire_bytes", "custom_id_sha256",
                            "input_snapshots", "prepared_inline"})
      if (!Same(old, next, key)) return Immutable();
    // Once an inline result is durable, subsequent page updates may only
    // affect other rows. Binary-backed legacy result semantics are unchanged.
    if (old.Has("result_inline") && OutputJson(old) != OutputJson(next))
      return Immutable();
  }
  return {};
}
}  // namespace q4t::server
