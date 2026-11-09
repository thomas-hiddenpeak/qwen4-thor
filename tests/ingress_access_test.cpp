#include "q4t/service/ingress_access.h"

#include <atomic>
#include <string>
#include <thread>
#include <vector>

#include "q4t/service/guardian_wire.h"
#include "q4t/test.h"

namespace {
namespace a = q4t::service::ingress;
namespace w = q4t::service::wire;
const std::string kAlice(40, 'a'), kBob(40, 'b'), kNew(40, 'c');

a::AccessPolicy Initial() {
  const auto alice = a::AccessKeyDigest(kAlice);
  const auto bob = a::AccessKeyDigest(kBob);
  return {.clients = {{"alice", {alice}}, {"bob", {bob}}},
          .key_owners = {{alice, "alice"}, {bob, "bob"}}};
}

std::string Bearer(const std::string& key) { return "Bearer " + key; }

void AddKey(a::AccessPolicy& policy, size_t slot, const std::string& key) {
  const auto digest = a::AccessKeyDigest(key);
  policy.clients[slot].key_sha256.push_back(digest);
  policy.key_owners.emplace(digest, policy.clients[slot].id);
}

std::string Encode(const a::AccessPolicy& policy) {
  q4t::io::Json clients, history;
  clients.type = q4t::io::Json::Type::kArray;
  history.type = q4t::io::Json::Type::kObject;
  for (const auto& client : policy.clients) {
    q4t::io::Json keys;
    keys.type = q4t::io::Json::Type::kArray;
    for (const auto& key : client.key_sha256)
      keys.array.push_back(w::String(key));
    clients.array.push_back(w::Object(
        {{"id", w::String(client.id)}, {"key_sha256", std::move(keys)}}));
  }
  for (const auto& [key, id] : policy.key_owners)
    history.object.emplace_back(key, w::String(id));
  std::string raw;
  const auto result =
      w::Encode(w::Object({{"version", w::Unsigned(1)},
                           {"revision", w::Unsigned(policy.revision)},
                           {"clients", std::move(clients)},
                           {"key_owners", std::move(history)}}),
                &raw);
  return result.ok() ? raw : "";
}
}  // namespace

Q4T_TEST(AccessOverlapRevocationAndAdmittedIdentity) {
  a::ClientAccess access;
  auto policy = Initial();
  Q4T_CHECK(access.Configure(policy).ok());
  const auto admitted = access.Authenticate(Bearer(kAlice));
  Q4T_CHECK(admitted.Allowed() && admitted.Id() == "alice");
  policy.revision = 1;
  AddKey(policy, 0, kNew);
  Q4T_CHECK(access.Replace(policy, 0).ok());
  Q4T_CHECK(access.Authenticate(Bearer(kAlice)).Index() == 0);
  Q4T_CHECK(access.Authenticate(Bearer(kNew)).Index() == 0);
  policy.revision = 2;
  policy.clients[0].key_sha256.erase(policy.clients[0].key_sha256.begin());
  Q4T_CHECK(access.Replace(policy, 1).ok());
  Q4T_CHECK(!access.Authenticate(Bearer(kAlice)).Allowed());
  Q4T_CHECK(access.Authenticate(Bearer(kNew)).Allowed());
  Q4T_CHECK(admitted.Id() == "alice" && admitted.Revision() == 0);
  policy.revision = 3;
  policy.clients[0].key_sha256.clear();
  Q4T_CHECK(access.Replace(policy, 2).ok());
  Q4T_CHECK(!access.Authenticate(Bearer(kNew)).Allowed());
  Q4T_CHECK(access.Authenticate(Bearer(kBob)).Id() == "bob");
  Q4T_CHECK(!access.Authenticate("Basic " + kBob).Allowed());
  Q4T_CHECK(!access.Authenticate(Bearer(kBob + " ")).Allowed());
  Q4T_CHECK(admitted.Id() == "alice" && admitted.Index() == 0);
  return true;
}

Q4T_TEST(AccessRevisionAndMalformedPolicyAreAtomic) {
  a::ClientAccess access;
  auto policy = Initial();
  Q4T_CHECK(access.Configure(policy).ok());
  const auto before = a::AccessPolicyDigest(policy);
  policy.revision = 2;
  Q4T_CHECK(!access.Replace(policy, 0).ok());
  policy.revision = 1;
  Q4T_CHECK(!access.Replace(policy, 1).ok());
  Q4T_CHECK(!access.Configure(policy).ok());
  Q4T_CHECK(access.Describe().GetString("sha256") == before);
  a::AccessPolicy output = Initial();
  std::string good = Encode(policy);
  auto fractional = good;
  fractional.replace(fractional.find("\"revision\":1"), 12, "\"revision\":1.5");
  for (const auto& bad :
       {std::string("{\"version\":1,") + good.substr(1),
        std::string("{\"extra\":1,") + good.substr(1), fractional,
        std::string(a::kAccessPolicyBytes + 1, ' ')}) {
    Q4T_CHECK(!a::ParseAccessPolicy(bad, &output).ok());
    Q4T_CHECK(a::AccessPolicyDigest(output) == before);
  }
  Q4T_CHECK(a::ParseAccessPolicy(good, &output).ok());
  Q4T_CHECK(access.Replace(output, 0).ok());
  Q4T_CHECK(access.Authenticate(Bearer(kAlice)).Revision() == 1);
  return true;
}

Q4T_TEST(AccessStableSlotsAndHistoricalOwnership) {
  a::ClientAccess access;
  auto policy = Initial();
  Q4T_CHECK(access.Configure(policy).ok());
  policy.revision = 1;
  std::swap(policy.clients[0], policy.clients[1]);
  Q4T_CHECK(!access.Replace(policy, 0).ok());
  policy = Initial();
  policy.revision = 1;
  policy.clients.pop_back();
  policy.key_owners.erase(a::AccessKeyDigest(kBob));
  Q4T_CHECK(!access.Replace(policy, 0).ok());
  policy = Initial();
  policy.revision = 1;
  policy.clients[0].key_sha256.clear();
  Q4T_CHECK(access.Replace(policy, 0).ok());
  policy.revision = 2;
  policy.key_owners[a::AccessKeyDigest(kAlice)] = "bob";
  policy.clients[1].key_sha256.push_back(a::AccessKeyDigest(kAlice));
  Q4T_CHECK(a::ValidateAccessPolicy(policy).ok());
  Q4T_CHECK(!access.Replace(policy, 1).ok());
  policy.clients[1].key_sha256.pop_back();
  policy.key_owners[a::AccessKeyDigest(kAlice)] = "alice";
  policy.clients.push_back({"carol", {}});
  AddKey(policy, 2, kNew);
  Q4T_CHECK(access.Replace(policy, 1).ok());
  Q4T_CHECK(access.Authenticate(Bearer(kNew)).Index() == 2);
  Q4T_CHECK(access.Authenticate(Bearer(kBob)).Index() == 1);
  return true;
}

Q4T_TEST(AccessRestartRetainsRevocationHistory) {
  auto policy = Initial();
  policy.revision = 19;
  policy.clients[0].key_sha256.clear();
  a::AccessPolicy decoded;
  Q4T_CHECK(a::ParseAccessPolicy(Encode(policy), &decoded).ok());
  a::ClientAccess restarted;
  Q4T_CHECK(restarted.Configure(decoded).ok());
  Q4T_CHECK(!restarted.Authenticate(Bearer(kAlice)).Allowed());
  decoded.revision = 20;
  decoded.key_owners.erase(a::AccessKeyDigest(kAlice));
  Q4T_CHECK(!restarted.Replace(decoded, 19).ok());
  decoded.key_owners[a::AccessKeyDigest(kAlice)] = "bob";
  decoded.clients[1].key_sha256.push_back(a::AccessKeyDigest(kAlice));
  Q4T_CHECK(!restarted.Replace(decoded, 19).ok());
  Q4T_CHECK(restarted.Authenticate(Bearer(kBob)).Revision() == 19);
  return true;
}

Q4T_TEST(AccessConcurrentPublicationHasCompleteSnapshots) {
  a::ClientAccess access;
  auto policy = Initial();
  policy.key_owners.emplace(a::AccessKeyDigest(kNew), "alice");
  Q4T_CHECK(access.Configure(policy).ok());
  std::atomic<bool> start{false}, done{false}, valid{true};
  std::atomic<uint64_t> observations{0};
  std::vector<std::jthread> readers;
  for (int i = 0; i < 4; ++i) {
    readers.emplace_back([&] {
      while (!start.load()) std::this_thread::yield();
      do {
        for (int key = 0; key < 2; ++key) {
          const auto id = access.Authenticate(Bearer(key ? kNew : kAlice));
          if (id.Allowed() && (id.Id() != "alice" || id.Index() != 0 ||
                               id.Revision() % 2 != unsigned(key)))
            valid.store(false);
          ++observations;
        }
        const auto bob = access.Authenticate(Bearer(kBob));
        if (!bob.Allowed() || bob.Id() != "bob" || bob.Index() != 1)
          valid.store(false);
      } while (!done.load());
    });
  }
  start.store(true);
  for (uint64_t revision = 1; revision <= 100; ++revision) {
    policy.revision = revision;
    policy.clients[0].key_sha256 = {
        a::AccessKeyDigest(revision % 2 ? kNew : kAlice)};
    if (!access.Replace(policy, revision - 1).ok()) valid.store(false);
  }
  done.store(true);
  readers.clear();
  Q4T_CHECK(valid.load() && observations.load() >= 8);
  return true;
}

Q4T_TEST(AccessBoundsAndPublicStatusContainNoCredentials) {
  auto policy = Initial();
  auto too_many = policy;
  for (size_t i = 2; i <= a::kAccessClients; ++i)
    too_many.clients.push_back({"client" + std::to_string(i), {}});
  Q4T_CHECK(!a::ValidateAccessPolicy(too_many).ok());
  too_many.clients.pop_back();
  Q4T_CHECK(a::ValidateAccessPolicy(too_many).ok());
  auto keys = policy;
  for (char c : {'d', 'e', 'f'}) AddKey(keys, 0, std::string(40, c));
  Q4T_CHECK(a::ValidateAccessPolicy(keys).ok());
  AddKey(keys, 0, std::string(40, 'g'));
  Q4T_CHECK(!a::ValidateAccessPolicy(keys).ok());
  auto history = policy;
  for (size_t i = 0; history.key_owners.size() < a::kAccessHistory; ++i) {
    const auto key = std::string(40, 'h') + std::to_string(i);
    history.key_owners.emplace(a::AccessKeyDigest(key), "alice");
  }
  Q4T_CHECK(a::ValidateAccessPolicy(history).ok());
  history.key_owners.emplace(a::AccessKeyDigest(kNew), "alice");
  Q4T_CHECK(!a::ValidateAccessPolicy(history).ok());
  a::ClientAccess access;
  Q4T_CHECK(access.Configure(policy).ok());
  std::string status;
  Q4T_CHECK(w::Encode(access.Describe(), &status).ok());
  for (const auto& secret :
       {kAlice, kBob, a::AccessKeyDigest(kAlice), a::AccessKeyDigest(kBob)})
    Q4T_CHECK(status.find(secret) == status.npos);
  Q4T_CHECK(access.Describe().GetBool("credential_values_redacted"));
  Q4T_CHECK(a::AccessKeyDigest(std::string(31, 'a')).empty());
  Q4T_CHECK(a::AccessKeyDigest(std::string(129, 'a')).empty());
  return true;
}

int main() { return q4t::test::RunAllTests(); }
