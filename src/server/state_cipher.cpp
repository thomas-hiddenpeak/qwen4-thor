#include "q4t/server/state_cipher.h"

#include <algorithm>
#include <ctime>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
// An internal scope disjoint from both client:<authenticated-id> and the
// loopback anonymous scope. Reuse the existing durable format/kind so older
// accepted binaries can still open the store during a rollback.
constexpr std::string_view kScope = "internal:q4t-opaque-state-keys-v1";
constexpr std::string_view kId = "file_q4t_state_keys_v1";
constexpr std::string_view kPrefix = "q4t1:";
ObjectReply Unavailable() {
  return Error(503, "state_crypto_unavailable",
               "Authenticated state encryption is temporarily unavailable");
}
ObjectReply Invalid() {
  // No differentiation between unknown, retired, foreign or tampered state.
  return Error(400, "invalid_encrypted_content",
               "Encrypted state is invalid for this client, model or item");
}
ObjectReply Corrupt() {
  return Error(503, "state_crypto_keys_invalid",
               "Persistent state keys require operator repair");
}
std::string Hex(std::span<const uint8_t> bytes) {
  constexpr char digits[] = "0123456789abcdef";
  std::string text(bytes.size() * 2, '\0');
  for (size_t i = 0; i < bytes.size(); ++i) {
    text[2 * i] = digits[bytes[i] >> 4];
    text[2 * i + 1] = digits[bytes[i] & 15];
  }
  return text;
}
std::string Hex(std::string_view text) {
  return Hex({reinterpret_cast<const uint8_t*>(text.data()), text.size()});
}
int Digit(char value) {
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
  return -1;
}
bool Unhex(std::string_view text, std::span<uint8_t> bytes) {
  if (text.size() != 2 * bytes.size()) return false;
  for (size_t i = 0; i < bytes.size(); ++i) {
    const int high = Digit(text[2 * i]), low = Digit(text[2 * i + 1]);
    if (high < 0 || low < 0) return false;
    bytes[i] = static_cast<uint8_t>((high << 4) | low);
  }
  return true;
}
struct Key {
  CryptoKey bytes{};
  ~Key() { CryptoWipe(bytes); }
};
void Wipe(Json* value) {
  CryptoWipe(&value->str);
  for (auto& child : value->array) Wipe(&child);
  for (auto& [name, child] : value->object) {
    (void)name;
    Wipe(&child);
  }
}
struct Ring {
  Json record;
  ~Ring() { Wipe(&record); }
  int64_t Revision() const { return record.GetInt("revision"); }
  Json& Data() { return Field(record, "data"); }
  Json& Keys() { return Field(Data(), "keys"); }
  const Json* Active() {
    const auto active = Data().GetString("active");
    for (const auto& key : Keys().array)
      if (key.GetString("id") == active) return &key;
    return nullptr;
  }
};
ObjectRequest Request(ObjectOperation operation) {
  ObjectRequest request;
  request.operation = operation;
  request.kind = ObjectKind::kFile;
  request.scope = kScope;
  request.id = kId;
  request.ttl_seconds = 0;
  return request;
}
ObjectReply Validate(Ring* ring) {
  const auto* data = ring->record.Find("data");
  const auto* keys = data ? data->GetArray("keys") : nullptr;
  if (ring->Revision() < 1 || ring->record.GetInt("expires_at") != 0 || !data ||
      data->GetInt("version") != 1 || !keys || keys->array.empty() ||
      keys->array.size() > StateCipher::kMaxKeys)
    return Corrupt();
  std::unordered_set<std::string> ids;
  for (const auto& entry : keys->array) {
    Key key;
    const auto id = entry.GetString("id");
    const auto* uses = entry.Find("uses");
    const auto* created = entry.Find("created_at");
    if (!Unhex(entry.GetString("key"), key.bytes) ||
        id != trace::HexDigest(trace::Sha256(key.bytes)).substr(0, 32) ||
        !ids.insert(id).second || !uses || !IsInputInteger(*uses) ||
        uses->number < 0 || uses->number > StateCipher::kMaxSeals || !created ||
        !IsInputInteger(*created) || created->number <= 0)
      return Corrupt();
  }
  return ring->Active() ? ObjectReply{} : Corrupt();
}
ObjectReply Generate(Json* entry) {
  Key key;
  if (CryptoRandom(key.bytes) != CryptoResult::kOk) return Unavailable();
  *entry = Object(
      {{"id", String(trace::HexDigest(trace::Sha256(key.bytes)).substr(0, 32))},
       {"key", String(Hex(key.bytes))},
       {"created_at", Number(std::time(nullptr))},
       {"uses", Number(0)}});
  return {};
}
ObjectReply Load(ObjectStore& store, const StateAccess& access, bool create,
                 Ring* ring) {
  auto reply = store.Execute(Request(ObjectOperation::kGet), access.deadline,
                             access.cancelled);
  if (!reply.ok()) {
    if (!create || reply.http_status != 404) return reply;
    Json key;
    auto generated = Generate(&key);
    if (!generated.ok()) return generated;
    auto request = Request(ObjectOperation::kPut);
    request.data = Object({{"version", Number(1)},
                           {"active", *key.Find("id")},
                           {"keys", Array({std::move(key)})}});
    reply = store.Execute(request, access.deadline, access.cancelled);
    Wipe(&request.data);
    // A competing first use may have created the same ID. On a lost receipt,
    // also read that ID once: no second creation or speculative overwrite.
    if (reply.ok() || reply.http_status == 409 || reply.commit_unknown) {
      Wipe(&reply.value);
      reply = store.Execute(Request(ObjectOperation::kGet), access.deadline,
                            access.cancelled);
    }
    if (!reply.ok()) return reply;
  }
  ring->record = std::move(reply.value);
  return Validate(ring);
}
Json DescribeRing(Ring* ring) {
  auto entries = Array();
  for (const auto& key : ring->Keys().array)
    entries.array.push_back(Object({{"id", *key.Find("id")},
                                    {"created_at", *key.Find("created_at")},
                                    {"uses", *key.Find("uses")}}));
  return Object({{"version", Number(1)},
                 {"revision", Number(ring->Revision())},
                 {"active", *ring->Data().Find("active")},
                 {"keys", std::move(entries)},
                 {"max_keys", Number(StateCipher::kMaxKeys)},
                 {"max_seals_per_key", Number(StateCipher::kMaxSeals)}});
}
bool Binding(const StateCipherBinding& binding, const StateAccess& access,
             std::string_view key_id, std::string* aad) {
  if (access.scope.empty() || access.scope.size() > 256 ||
      binding.model.empty() || binding.model.size() > 1024 ||
      binding.template_id.empty() || binding.template_id.size() > 256 ||
      binding.purpose.empty() || binding.purpose.size() > 64 ||
      !ValidId(binding.item_id))
    return false;
  *aad = OutputJson(Array({String("q4t-state-aes256gcm-v1"),
                           String(std::string(key_id)), String(access.scope),
                           String(binding.model), String(binding.template_id),
                           String(binding.purpose), String(binding.item_id)}));
  return aad->size() <= kCryptoAadLimit;
}
ObjectReply Available() {
  return CryptoAvailable() == CryptoResult::kOk ? ObjectReply{} : Unavailable();
}
}  // namespace

ObjectReply StateCipher::Ready(const StateAccess& access) {
  auto available = Available();
  if (!available.ok()) return available;
  Ring ring;
  return Load(objects_, access, true, &ring);
}
ObjectReply StateCipher::Describe(const StateAccess& access) {
  Ring ring;
  auto reply = Load(objects_, access, false, &ring);
  if (reply.ok()) reply.value = DescribeRing(&ring);
  return reply;
}
ObjectReply StateCipher::Rotate(int64_t expected_revision,
                                const std::vector<std::string>& retire,
                                const StateAccess& access) {
  auto available = Available();
  if (!available.ok()) return available;
  Ring ring;
  auto reply = Load(objects_, access, false, &ring);
  if (!reply.ok()) return reply;
  if (expected_revision < 1 || expected_revision != ring.Revision())
    return Error(409, "state_crypto_revision_conflict",
                 "Inspect current key revision before rotating");
  std::unordered_set<std::string> retired;
  for (const auto& id : retire) {
    const auto& keys = ring.Keys().array;
    if (!retired.insert(id).second ||
        std::none_of(keys.begin(), keys.end(), [&](const auto& key) {
          return key.GetString("id") == id;
        }))
      return Error(400, "state_crypto_retire_invalid",
                   "Each retired key must exist and be listed once");
  }
  if (ring.Keys().array.size() - retired.size() >= kMaxKeys)
    return Error(409, "state_crypto_retention_full",
                 "Explicitly retire an old key before adding another");
  std::erase_if(ring.Keys().array, [&](Json& entry) {
    if (!retired.contains(entry.GetString("id"))) return false;
    Wipe(&entry);
    return true;
  });
  Json next;
  reply = Generate(&next);
  if (!reply.ok()) return reply;
  const auto id = next.GetString("id");
  if (std::any_of(ring.Keys().array.begin(), ring.Keys().array.end(),
                  [&](const auto& key) { return key.GetString("id") == id; })) {
    Wipe(&next);
    return Unavailable();
  }
  Set(&ring.Data(), "active", String(id));
  ring.Keys().array.push_back(std::move(next));
  auto request = Request(ObjectOperation::kPut);
  request.expected_revision = expected_revision;
  request.data = std::move(ring.Data());
  reply = objects_.Execute(request, access.deadline, access.cancelled);
  Wipe(&request.data);
  if (reply.ok()) {
    // Project this exact receipt; a concurrent later rotation is separate.
    ring.record = std::move(reply.value);
    reply.value = DescribeRing(&ring);
  } else if (reply.commit_unknown) {
    // A caller can reconcile the operation by this public new key ID.
    reply.value = Object({{"id", String(id)}});
  }
  return reply;
}
ObjectReply StateCipher::Seal(std::string_view plaintext,
                              const StateCipherBinding& binding,
                              const StateAccess& access, std::string* opaque) {
  if (!opaque || plaintext.size() > kCryptoPlaintextLimit)
    return Error(400, "encrypted_content_limit",
                 "Encrypted state is too large");
  auto available = Available();
  if (!available.ok()) return available;
  for (int attempt = 0; attempt < 3; ++attempt) {
    Ring ring;
    auto reply = Load(objects_, access, true, &ring);
    if (!reply.ok()) return reply;
    const auto* active = ring.Active();
    const auto id = active->GetString("id");
    Key key;
    if (!Unhex(active->GetString("key"), key.bytes)) return Corrupt();
    std::string aad;
    if (!Binding(binding, access, id, &aad)) return Invalid();
    const auto uses = active->GetInt("uses");
    if (uses >= kMaxSeals)
      return Error(503, "state_crypto_rotation_required",
                   "Rotate the state key before issuing more encrypted state");
    CryptoNonce nonce{};
    if (CryptoRandom(nonce) != CryptoResult::kOk) return Unavailable();
    for (auto& entry : ring.Keys().array)
      if (entry.GetString("id") == id) Set(&entry, "uses", Number(uses + 1));
    auto request = Request(ObjectOperation::kPut);
    request.expected_revision = ring.Revision();
    request.data = std::move(ring.Data());
    reply = objects_.Execute(request, access.deadline, access.cancelled);
    Wipe(&request.data);
    if (!reply.ok()) {
      if (!reply.commit_unknown && reply.http_status == 409) continue;
      return reply;
    }
    Wipe(&reply.value);
    CryptoTag tag{};
    std::string ciphertext;
    if (CryptoSeal(key.bytes, nonce, aad, plaintext, &ciphertext, &tag) !=
        CryptoResult::kOk)
      return Unavailable();
    *opaque = std::string(kPrefix) + id + ':' + Hex(nonce) + ':' + Hex(tag) +
              ':' + Hex(ciphertext);
    return {};
  }
  return Error(503, "state_crypto_busy", "State key is being updated");
}
ObjectReply StateCipher::Open(std::string_view opaque,
                              const StateCipherBinding& binding,
                              const StateAccess& access,
                              std::string* plaintext) {
  constexpr size_t header = 5 + 32 + 1 + 24 + 1 + 32 + 1;
  if (!plaintext || opaque.size() < header ||
      opaque.size() > header + 2 * kCryptoPlaintextLimit ||
      !opaque.starts_with(kPrefix) || opaque[37] != ':' || opaque[62] != ':' ||
      opaque[95] != ':' || (opaque.size() - header) % 2)
    return Invalid();
  const auto id = opaque.substr(5, 32);
  CryptoNonce nonce{};
  CryptoTag tag{};
  std::string ciphertext((opaque.size() - header) / 2, '\0');
  if (!Unhex(opaque.substr(38, 24), nonce) ||
      !Unhex(opaque.substr(63, 32), tag) ||
      !Unhex(
          opaque.substr(header),
          {reinterpret_cast<uint8_t*>(ciphertext.data()), ciphertext.size()}))
    return Invalid();
  std::string aad;
  if (!Binding(binding, access, id, &aad)) return Invalid();
  auto available = Available();
  if (!available.ok()) return available;
  Ring ring;
  auto reply = Load(objects_, access, false, &ring);
  if (!reply.ok()) return reply.http_status == 404 ? Invalid() : reply;
  const auto& entries = ring.Keys().array;
  const auto found =
      std::find_if(entries.begin(), entries.end(),
                   [&](const auto& key) { return key.GetString("id") == id; });
  if (found == entries.end()) return Invalid();
  Key key;
  if (!Unhex(found->GetString("key"), key.bytes)) return Corrupt();
  const auto result =
      CryptoOpen(key.bytes, nonce, aad, ciphertext, tag, plaintext);
  if (result == CryptoResult::kOk) return {};
  if (result == CryptoResult::kAuthentication ||
      result == CryptoResult::kInvalid)
    return Invalid();
  return Unavailable();
}
}  // namespace q4t::server
