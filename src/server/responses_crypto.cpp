#include "q4t/server/responses_crypto.h"

#include <algorithm>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/responses_compaction.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/schema_graph.h"

namespace q4t::server {
namespace {
using namespace object_detail;
struct Plaintext {
  std::string bytes;
  ~Plaintext() { CryptoWipe(&bytes); }
};
StateCipherBinding Binding(const ChatPlanConfig& config, const Json& item) {
  // Bind to the canonical served model, never an alias/body-selected name.
  // Template changes require explicit migration; unrelated protocol releases
  // do not invalidate otherwise identical model/template state.
  return {config.model,
          Digest(std::string(identity::kTemplateReference) + ':' +
                 identity::kRenderer),
          "responses.reasoning.v1", item.GetString("id")};
}
void Remove(Json* value, std::initializer_list<std::string_view> fields) {
  std::erase_if(value->object, [&](const auto& field) {
    return std::find(fields.begin(), fields.end(), field.first) != fields.end();
  });
}
Json PlainItem(Json item) {
  Remove(&item, {"encrypted_content"});
  return item;
}
Json SemanticItem(Json item) {
  Remove(&item, {"status", "encrypted_content"});
  if (item.GetString("type") == "message") {
    Remove(&item, {"id"});
    auto& content = Field(item, "content");
    if (content.IsString())
      content =
          Array({Object({{"type", String("input_text")}, {"text", content}})});
    for (auto& part : content.array) {
      if (part.GetString("type") != "output_text") continue;
      Set(&part, "type", String("input_text"));
      Remove(&part, {"annotations", "logprobs"});
    }
  }
  return item;
}
bool Equal(const Json& left, const Json& right) {
  std::string a, b;
  return CanonicalSchemaValue(left, &a) && CanonicalSchemaValue(right, &b) &&
         a == b;
}
ObjectReply Invalid(
    std::string message = "Invalid authenticated reasoning state") {
  return Error(400, "invalid_encrypted_content", std::move(message));
}
ObjectReply Active(const StateAccess& access) {
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Reasoning state deadline expired");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Reasoning state request cancelled");
  return {};
}
ObjectReply At(ObjectReply reply, size_t index) {
  if (!reply.ok())
    reply.value = Object({{"param", String("input[" + std::to_string(index) +
                                           "].encrypted_content")}});
  return reply;
}
ObjectReply Validate(const Json& item) {
  ChatPlanError error;
  const auto status = ValidateResponsesItemShape(
      item, ResponsesItemShape::kConversation, &error, "input");
  return status ? ObjectReply{} : Invalid();
}
}  // namespace

ObjectReply OpenResponsesReasoning(StateCipher& cipher, const Json& history,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access,
                                   Json* generation_history) {
  if (!generation_history || !history.IsArray() || history.array.size() > 4096)
    return Invalid();
  auto result = Array();
  for (size_t i = 0; i < history.array.size(); ++i) {
    auto active = Active(access);
    if (!active.ok()) return At(std::move(active), i);
    const auto& incoming = history.array[i];
    if (incoming.GetString("type") == "compaction") {
      Json compacted;
      auto opened = OpenResponsesCompaction(cipher, result, incoming, config,
                                            access, &compacted);
      if (!opened.ok()) return At(std::move(opened), i);
      if (compacted.array.size() + history.array.size() - i - 1 > 4096)
        return At(Error(400, "input_limit_exceeded",
                        "Restored compaction context exceeds 4096 items"),
                  i);
      result = std::move(compacted);
      continue;
    }
    if (incoming.GetString("type") != "reasoning" ||
        !Nonnull(incoming, "encrypted_content")) {
      result.array.push_back(incoming);
      continue;
    }
    Plaintext plaintext;
    auto opened =
        cipher.Open(incoming.GetString("encrypted_content"),
                    Binding(config, incoming), access, &plaintext.bytes);
    if (!opened.ok()) return At(std::move(opened), i);
    Json payload;
    io::JsonParseLimits limits;
    limits.max_depth = 96;
    limits.max_values = 2 * 1024 * 1024;
    limits.max_string_bytes = kCryptoPlaintextLimit;
    limits.reject_duplicate_keys = true;
    limits.allow_exact_number_text = true;
    if (!io::ParseJson(plaintext.bytes, &payload, true, limits) ||
        payload.GetInt("version") != 1 || payload.object.size() != 3)
      return At(Invalid(), i);
    const auto* prefix = payload.GetArray("prefix");
    const auto* item = payload.Find("item");
    if (!prefix || prefix->array.size() > 4096 || !item ||
        item->GetString("type") != "reasoning" ||
        item->GetString("id") != incoming.GetString("id") ||
        Nonnull(*item, "encrypted_content") || !Validate(*item).ok())
      return At(Invalid(), i);
    // Empty/omitted public reasoning projections may be restored. A nonempty
    // competing plaintext cannot override the authenticated original.
    for (const auto* name : {"content", "summary"}) {
      const auto* projected = incoming.Find(name);
      const auto* original = item->Find(name);
      if (projected && !projected->IsNull() &&
          (!projected->IsArray() ||
           (!projected->array.empty() &&
            (!original || !Equal(*projected, *original)))))
        return At(Invalid("Public reasoning differs from authenticated state"),
                  i);
    }
    if (result.array.size() > prefix->array.size())
      return At(Invalid("Reasoning origin prefix does not match"), i);
    const auto missing = prefix->array.size() - result.array.size();
    for (size_t j = 0; j < prefix->array.size(); ++j) {
      const auto& previous = prefix->array[j];
      if (Nonnull(previous, "encrypted_content") || !Validate(previous).ok())
        return At(Invalid(), i);
      if (j >= missing && !Equal(SemanticItem(previous),
                                 SemanticItem(result.array[j - missing])))
        return At(Invalid("Reasoning origin prefix does not match"), i);
    }
    // Restore only an omitted prefix, never replace a conflicting supplied one.
    // Reuse in the same authorized conversation is allowed. It is context, not
    // a command to replay a model step or execute a historical tool call.
    result.array.insert(result.array.begin(), prefix->array.begin(),
                        prefix->array.begin() + missing);
    result.array.push_back(*item);
    if (result.array.size() + history.array.size() - i - 1 > 4096)
      return At(Error(400, "input_limit_exceeded",
                      "Restored reasoning context exceeds 4096 items"),
                i);
  }
  std::unordered_set<std::string> ids;
  for (const auto& item : result.array) {
    const auto id = item.GetString("id");
    if (!id.empty() && !ids.insert(id).second)
      return Invalid("Restored reasoning context contains duplicate item IDs");
  }
  *generation_history = std::move(result);
  return {};
}

ObjectReply SealResponsesReasoning(StateCipher& cipher,
                                   const Json& generation_history,
                                   const Json& output, size_t index,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access,
                                   std::string* opaque) {
  auto active = Active(access);
  if (!active.ok()) return active;
  if (!generation_history.IsArray() || !output.IsArray() ||
      index >= output.array.size() ||
      output.array[index].GetString("type") != "reasoning")
    return Error(500, "response_state_invalid", "Invalid reasoning to seal");
  auto prefix = generation_history;
  for (auto& item : prefix.array) Remove(&item, {"encrypted_content"});
  for (size_t i = 0; i < index; ++i) {
    // The execution owner already supplies the measured compacted history.
    // Do not embed an opaque compaction item again in a reasoning prefix.
    if (output.array[i].GetString("type") != "compaction")
      prefix.array.push_back(PlainItem(output.array[i]));
  }
  if (prefix.array.size() >= 4096)
    return Error(400, "input_limit_exceeded",
                 "Reasoning context exceeds 4096 items");
  Plaintext plaintext;
  plaintext.bytes =
      OutputJson(Object({{"version", Number(1)},
                         {"prefix", std::move(prefix)},
                         {"item", PlainItem(output.array[index])}}));
  auto sealed = cipher.Seal(
      plaintext.bytes, Binding(config, output.array[index]), access, opaque);
  // An uncertain key-use reservation is not an uncertain response commit.
  // Do not expose a private key record ID as a client retrieval instruction.
  if (sealed.commit_unknown)
    return Error(503, "state_crypto_commit_unknown",
                 "Key accounting receipt unavailable; no ciphertext issued");
  return sealed;
}
}  // namespace q4t::server
