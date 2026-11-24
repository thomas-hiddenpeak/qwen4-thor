#include "agent_definition_cipher.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::agent_detail {
namespace {
using namespace object_detail;
struct DefinitionContract {
  const char* domain;
  const char* purpose;
  const char* format;
  const char* error;
  const char* message;
  const char* limit;
  const char* limit_message;
};

const DefinitionContract& Contract(DefinitionKind kind) {
  static constexpr DefinitionContract environment{
      "environment",
      "template_definition",
      "q4t.environment.definition.v1",
      "environment_template_invalid",
      "Invalid stored environment definition",
      "environment_definition_limit",
      "Environment definition exceeds the application byte budget"};
  static constexpr DefinitionContract automation{
      "automation",
      "automation_target",
      "q4t.automation.target.v1",
      "automation_record_invalid",
      "Invalid stored automation target",
      "automation_target_limit",
      "Automation target exceeds the application byte budget"};
  return kind == DefinitionKind::kEnvironmentTemplate ? environment
                                                      : automation;
}

ObjectReply Invalid(const DefinitionContract& spec) {
  return Error(503, spec.error, spec.message);
}

ObjectReply StoredFailure(const DefinitionContract& spec, ObjectReply failure) {
  return failure.http_status == 400 ? Invalid(spec) : std::move(failure);
}

StateCipherBinding Binding(const DefinitionContract& spec,
                           const std::string& id,
                           std::string_view suffix = {}) {
  std::string purpose(spec.purpose);
  purpose += suffix;
  return {"q4t.agents", spec.domain, std::move(purpose), id};
}

// Keep the historical template binding bytes. Each encrypted part is tied to
// the tenant, resource, plaintext digest and position in the verified index.
StateCipherBinding Part(const DefinitionContract& spec, const std::string& id,
                        const std::string& digest, size_t index) {
  return Binding(spec, Digest(id + "_" + digest + "_" + std::to_string(index)),
                 "_part");
}
}  // namespace

ObjectReply SealDefinition(ObjectStore& objects, const StateAccess& access,
                           DefinitionKind kind, const std::string& id,
                           const Json& definition, Json* sealed) {
  const auto& spec = Contract(kind);
  const auto raw = OutputJson(definition);
  StateCipher cipher(objects);
  std::string opaque;
  if (raw.size() <= kCryptoPlaintextLimit) {
    auto result = cipher.Seal(raw, Binding(spec, id), access, &opaque);
    if (result.ok()) *sealed = String(std::move(opaque));
    return result;
  }
  if (raw.size() > kChatBodyBytes)
    return Error(413, spec.limit, spec.limit_message);
  const auto digest = Digest(raw);
  auto parts = Array();
  const std::string_view bytes(raw);
  for (size_t offset = 0; offset < bytes.size();
       offset += kCryptoPlaintextLimit) {
    auto part = cipher.Seal(bytes.substr(offset, kCryptoPlaintextLimit),
                            Part(spec, id, digest, parts.array.size()), access,
                            &opaque);
    if (!part.ok()) return part;
    parts.array.push_back(String(std::move(opaque)));
  }
  const auto index =
      Object({{"bytes", Number(bytes.size())}, {"sha256", String(digest)}});
  auto result = cipher.Seal(OutputJson(index), Binding(spec, id, "_index"),
                            access, &opaque);
  if (result.ok())
    *sealed = Object({{"format", String(spec.format)},
                      {"index", String(std::move(opaque))},
                      {"parts", std::move(parts)}});
  return result;
}

ObjectReply OpenDefinition(ObjectStore& objects, const StateAccess& access,
                           DefinitionKind kind, const std::string& id,
                           const Json& sealed, Json* definition) {
  const auto& spec = Contract(kind);
  StateCipher cipher(objects);
  std::string raw;
  if (sealed.IsString()) {
    auto result = cipher.Open(sealed.str, Binding(spec, id), access, &raw);
    if (!result.ok()) return StoredFailure(spec, std::move(result));
  } else {
    const auto* parts = sealed.GetArray("parts");
    const auto* index = sealed.Find("index");
    if (sealed.GetString("format") != spec.format || !parts || !index ||
        !index->IsString())
      return Invalid(spec);
    std::string decoded;
    auto result =
        cipher.Open(index->str, Binding(spec, id, "_index"), access, &decoded);
    if (!result.ok()) return StoredFailure(spec, std::move(result));
    Json manifest;
    if (!io::ParseJson(decoded, &manifest, true)) return Invalid(spec);
    const auto bytes = manifest.GetInt("bytes");
    const auto digest = manifest.GetString("sha256");
    if (bytes <= static_cast<int64_t>(kCryptoPlaintextLimit) ||
        bytes > static_cast<int64_t>(kChatBodyBytes) || digest.size() != 64)
      return Invalid(spec);
    const size_t size = static_cast<size_t>(bytes);
    if (parts->array.size() != (size - 1) / kCryptoPlaintextLimit + 1)
      return Invalid(spec);
    raw.reserve(size);
    for (size_t i = 0; i < parts->array.size(); ++i) {
      const auto& part = parts->array[i];
      if (!part.IsString()) return Invalid(spec);
      result =
          cipher.Open(part.str, Part(spec, id, digest, i), access, &decoded);
      if (!result.ok()) return StoredFailure(spec, std::move(result));
      if (decoded.size() != std::min(size - raw.size(), kCryptoPlaintextLimit))
        return Invalid(spec);
      raw.append(decoded);
    }
    if (Digest(raw) != digest) return Invalid(spec);
  }
  if (!io::ParseJson(raw, definition, true, kApplicationJsonLimits))
    return Invalid(spec);
  return {};
}
}  // namespace q4t::server::agent_detail
