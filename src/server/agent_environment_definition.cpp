#include "agent_environment_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::environment_detail {
namespace {
using namespace object_detail;
constexpr std::string_view kFormat = "q4t.environment.definition.v1";

ObjectReply Invalid() {
  return Error(503, "environment_template_invalid",
               "Invalid stored environment definition");
}

ObjectReply StoredFailure(ObjectReply failure) {
  // The server supplied the stored ciphertext and binding. Authentication
  // failure here is a damaged resource, not a malformed client request.
  return failure.http_status == 400 ? Invalid() : std::move(failure);
}

StateCipherBinding Binding(const std::string& id, std::string purpose) {
  return {"q4t.agents", "environment", std::move(purpose), id};
}

// Bind every piece to the tenant (StateCipher), template, exact plaintext
// version and position. Only the authenticated index supplies the digest.
StateCipherBinding Part(const std::string& id, const std::string& digest,
                        size_t index) {
  return Binding(Digest(id + "_" + digest + "_" + std::to_string(index)),
                 "template_definition_part");
}
}  // namespace

ObjectReply SealDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const Json& definition,
                           Json* sealed) {
  const auto raw = OutputJson(definition);
  StateCipher cipher(objects);
  std::string opaque;
  if (raw.size() <= kCryptoPlaintextLimit) {
    auto result =
        cipher.Seal(raw, Binding(id, "template_definition"), access, &opaque);
    if (result.ok()) *sealed = String(std::move(opaque));
    return result;
  }
  if (raw.size() > kChatBodyBytes)
    return Error(413, "environment_definition_limit",
                 "Environment definition exceeds the application byte budget");
  const auto digest = Digest(raw);
  auto parts = Array();
  const std::string_view bytes(raw);
  for (size_t offset = 0; offset < bytes.size(); offset += kCryptoPlaintextLimit) {
    auto part = cipher.Seal(bytes.substr(offset, kCryptoPlaintextLimit),
                            Part(id, digest, parts.array.size()), access, &opaque);
    if (!part.ok()) return part;
    parts.array.push_back(String(std::move(opaque)));
  }
  const auto index = Object({{"bytes", Number(bytes.size())},
                             {"sha256", String(digest)}});
  auto result = cipher.Seal(OutputJson(index),
                            Binding(id, "template_definition_index"), access,
                            &opaque);
  if (result.ok())
    *sealed = Object({{"format", String(std::string(kFormat))},
                      {"index", String(std::move(opaque))},
                      {"parts", std::move(parts)}});
  return result;
}

ObjectReply OpenDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const Json& sealed,
                           Json* definition) {
  StateCipher cipher(objects);
  std::string raw;
  if (sealed.IsString()) {
    auto result =
        cipher.Open(sealed.str, Binding(id, "template_definition"), access, &raw);
    if (!result.ok()) return StoredFailure(std::move(result));
  } else {
    const auto* parts = sealed.GetArray("parts");
    const auto* index = sealed.Find("index");
    if (sealed.GetString("format") != kFormat || !parts || !index ||
        !index->IsString())
      return Invalid();
    std::string decoded;
    auto result = cipher.Open(index->str, Binding(id, "template_definition_index"),
                              access, &decoded);
    if (!result.ok()) return StoredFailure(std::move(result));
    Json manifest;
    if (!io::ParseJson(decoded, &manifest, true)) return Invalid();
    const auto bytes = manifest.GetInt("bytes");
    const auto digest = manifest.GetString("sha256");
    if (bytes <= static_cast<int64_t>(kCryptoPlaintextLimit) ||
        bytes > static_cast<int64_t>(kChatBodyBytes) || digest.size() != 64)
      return Invalid();
    const size_t size = static_cast<size_t>(bytes);
    if (parts->array.size() != (size - 1) / kCryptoPlaintextLimit + 1)
      return Invalid();
    raw.reserve(size);
    for (size_t i = 0; i < parts->array.size(); ++i) {
      const auto& part = parts->array[i];
      if (!part.IsString()) return Invalid();
      result = cipher.Open(part.str, Part(id, digest, i), access, &decoded);
      if (!result.ok()) return StoredFailure(std::move(result));
      if (decoded.size() != std::min(size - raw.size(), kCryptoPlaintextLimit))
        return Invalid();
      raw.append(decoded);
    }
    if (Digest(raw) != digest) return Invalid();
  }
  ChatPlanError error;
  if (!io::ParseJson(raw, definition, true, kApplicationJsonLimits) ||
      !ValidateEnvironmentTemplate(*definition,
                                   EnvironmentTemplateShape::kStoredDefinition,
                                   &error))
    return Invalid();
  return {};
}
}  // namespace q4t::server::environment_detail
