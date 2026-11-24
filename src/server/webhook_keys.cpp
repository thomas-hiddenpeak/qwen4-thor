#include "webhook_keys.h"

#include <set>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server::webhook_detail {
namespace {
using namespace object_detail;
StateCipherBinding Binding(const std::string& id) {
  return {"q4t.webhooks", "endpoint", "signing_secrets", id};
}
ObjectReply Invalid() {
  return Error(503, "webhook_record_invalid", "Invalid stored signing keys");
}
}  // namespace

ObjectReply ReadKeys(ObjectStore& objects, const StateAccess& access,
                     const std::string& id, const Json& record, int64_t now,
                     Json* active) {
  StateCipher cipher(objects);
  std::string plain;
  auto opened =
      cipher.Open(record.GetString("sealed_keys"), Binding(id), access, &plain);
  if (!opened.ok()) return opened.http_status == 400 ? Invalid() : opened;
  Json keys;
  auto parsed = io::ParseJson(plain, &keys, false, {8, 64, 2048, true});
  CryptoWipe(&plain);
  if (!parsed || !keys.IsArray() || keys.array.empty() || keys.array.size() > 8)
    return Invalid();
  *active = Array();
  std::set<std::string> seen;
  for (size_t i = 0; i < keys.array.size(); ++i) {
    auto& key = keys.array[i];
    const auto value = key.GetString("secret");
    const auto expires = key.GetInt("expires_at", -1);
    size_t bytes = 0;
    if (key.object.size() != 2 || !value.starts_with("whsec_") ||
        !io::Base64DecodedSize(std::string_view(value).substr(6), &bytes) ||
        bytes != 32 || expires < 0 || ((i == 0) != (expires == 0)) ||
        !seen.insert(value).second)
      return Invalid();
    if (expires == 0 || expires > now) active->array.push_back(std::move(key));
  }
  return {};
}

ObjectReply Rotate(ObjectStore& objects, const StateAccess& access,
                   const std::string& id, int64_t now, bool keep_old, Json keys,
                   Json* record, std::string* secret) {
  if (keep_old && keys.array.size() >= 8)
    return Error(429, "webhook_rotation_capacity",
                 "Too many signing keys within the rotation grace period");
  CryptoKey key;
  if (CryptoRandom(key) != CryptoResult::kOk)
    return Error(503, "webhook_signing_unavailable", "No signing entropy");
  std::string encoded;
  auto status = io::EncodeBase64(
      {reinterpret_cast<const char*>(key.data()), key.size()}, 44, &encoded);
  CryptoWipe(key);
  if (!status) return Invalid();
  *secret = "whsec_" + encoded;
  CryptoWipe(&encoded);
  auto next =
      Array({Object({{"secret", String(*secret)}, {"expires_at", Number(0)}})});
  if (!keys.array.empty()) {
    if (keep_old)
      Set(&keys.array.front(), "expires_at", Number(now + 86400));
    else
      keys.array.erase(keys.array.begin());
    // Any earlier key with an existing grace period retains its own expiry.
    for (auto& item : keys.array) next.array.push_back(std::move(item));
  }
  StateCipher cipher(objects);
  auto plaintext = OutputJson(next);
  std::string opaque;
  auto sealed = cipher.Seal(plaintext, Binding(id), access, &opaque);
  CryptoWipe(&plaintext);
  if (!sealed.ok()) {
    CryptoWipe(secret);
    return sealed;
  }
  Set(record, "sealed_keys", String(std::move(opaque)));
  return {};
}
}  // namespace q4t::server::webhook_detail
