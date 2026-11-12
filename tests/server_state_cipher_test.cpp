#include "q4t/server/state_cipher.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <future>

#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& raw) {
  Json result;
  if (!q4t::io::ParseJson(raw, &result, true))
    throw std::runtime_error("cipher fixture JSON");
  return result;
}
Json& Field(Json* object, const char* name) {
  for (auto& [key, value] : object->object)
    if (key == name) return value;
  throw std::runtime_error("cipher fixture field");
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/state-cipher-cpu");
    std::filesystem::create_directories(root);
    auto name = (root / "cipher-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("cipher fixture path");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access(std::string scope = "client:cpu") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
StateCipherBinding Binding() {
  return {"cpu", "template-v1", "reasoning", "rs_1"};
}
ObjectRequest KeyRequest(ObjectOperation operation = ObjectOperation::kGet) {
  ObjectRequest request;
  request.operation = operation;
  request.kind = ObjectKind::kFile;
  request.id = "file_q4t_state_keys_v1";
  request.scope = "internal:q4t-opaque-state-keys-v1";
  request.ttl_seconds = 0;
  return request;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
}  // namespace

Q4T_TEST(state_cipher_restart_and_authenticated_bindings) {
  Fixture f;
  std::string opaque;
  {
    ObjectStore store(f.options);
    StateCipher cipher(store);
    Q4T_CHECK(Ok(cipher.Seal("真实推理", Binding(), Access(), &opaque)));
    Q4T_CHECK(opaque.starts_with("q4t1:") &&
              opaque.find("推理") == opaque.npos);
  }
  ObjectStore store(f.options);
  StateCipher cipher(store);
  std::string plain;
  Q4T_CHECK(Ok(cipher.Open(opaque, Binding(), Access(), &plain)));
  Q4T_CHECK(plain == "真实推理");
  for (int component = 0; component < 7; ++component) {
    auto binding = Binding();
    auto access = Access();
    auto modified = opaque;
    if (component == 0) access.scope = "client:other";
    if (component == 1) binding.model = "other";
    if (component == 2) binding.template_id = "template-v2";
    if (component == 3) binding.purpose = "compaction";
    if (component == 4) binding.item_id = "rs_other";
    if (component == 5) modified[modified.size() - 1] ^= 1;
    if (component == 6) modified[0] = 'x';
    plain = "unchanged";
    const auto reply = cipher.Open(modified, binding, access, &plain);
    Q4T_CHECK(reply.code == "invalid_encrypted_content" &&
              plain == "unchanged");
  }
  return true;
}

Q4T_TEST(state_cipher_rotation_keeps_old_states_until_explicit_retirement) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  std::string old, next, plain;
  Q4T_CHECK(Ok(cipher.Seal("old", Binding(), Access(), &old)));
  auto described = cipher.Describe(Access());
  Q4T_CHECK(Ok(described));
  const auto old_id = described.value.GetString("active");
  auto rotated =
      cipher.Rotate(described.value.GetInt("revision"), {}, Access());
  Q4T_CHECK(Ok(rotated) && rotated.value.GetString("active") != old_id);
  Q4T_CHECK(Ok(cipher.Open(old, Binding(), Access(), &plain)) &&
            plain == "old");
  Q4T_CHECK(Ok(cipher.Seal("next", Binding(), Access(), &next)));
  const auto current = cipher.Describe(Access());
  rotated = cipher.Rotate(current.value.GetInt("revision"), {old_id}, Access());
  Q4T_CHECK(Ok(rotated));
  Q4T_CHECK(cipher.Open(old, Binding(), Access(), &plain).code ==
            "invalid_encrypted_content");
  Q4T_CHECK(Ok(cipher.Open(next, Binding(), Access(), &plain)) &&
            plain == "next");
  return true;
}

Q4T_TEST(state_cipher_rotation_requires_current_revision_and_known_ids) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  const auto first = cipher.Describe(Access());
  Q4T_CHECK(Ok(first));
  const auto revision = first.value.GetInt("revision");
  Q4T_CHECK(cipher.Rotate(revision, {"absent"}, Access()).http_status == 400);
  const auto id = first.value.GetString("active");
  Q4T_CHECK(cipher.Rotate(revision, {id, id}, Access()).http_status == 400);
  Q4T_CHECK(Ok(cipher.Rotate(revision, {}, Access())));
  Q4T_CHECK(cipher.Rotate(revision, {}, Access()).code ==
            "state_crypto_revision_conflict");
  const auto last = cipher.Describe(Access());
  Q4T_CHECK(Ok(last) && last.value.GetInt("revision") == revision + 1);
  return true;
}

Q4T_TEST(state_cipher_retention_bound_does_not_silently_destroy_keys) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  auto state = cipher.Describe(Access());
  for (size_t i = 1; i < StateCipher::kMaxKeys; ++i) {
    state = cipher.Rotate(state.value.GetInt("revision"), {}, Access());
    Q4T_CHECK(Ok(state));
  }
  const auto before = OutputJson(state.value);
  Q4T_CHECK(cipher.Rotate(state.value.GetInt("revision"), {}, Access()).code ==
            "state_crypto_retention_full");
  const auto after = cipher.Describe(Access());
  Q4T_CHECK(Ok(after) && OutputJson(after.value) == before);
  return true;
}

Q4T_TEST(state_cipher_missing_open_does_not_create_or_overwrite_state) {
  Fixture f, foreign;
  std::string opaque;
  {
    ObjectStore store(f.options);
    Q4T_CHECK(Ok(StateCipher(store).Seal("p", Binding(), Access(), &opaque)));
  }
  ObjectStore store(foreign.options);
  StateCipher cipher(store);
  std::string plain = "unchanged";
  Q4T_CHECK(cipher.Open(opaque, Binding(), Access(), &plain).code ==
            "invalid_encrypted_content");
  Q4T_CHECK(plain == "unchanged" &&
            cipher.Describe(Access()).http_status == 404);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  Q4T_CHECK(cipher.Open(opaque, Binding(), Access(), &plain).code ==
            "invalid_encrypted_content");
  return true;
}

Q4T_TEST(state_cipher_key_material_is_private_and_not_in_admin_output) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  const auto state = cipher.Describe(Access());
  Q4T_CHECK(Ok(state));
  Q4T_CHECK(OutputJson(state.value).find("\"key\":") == std::string::npos);
  for (const auto scope : {"client:cpu", "local:anonymous"}) {
    auto request = KeyRequest();
    request.scope = scope;
    Q4T_CHECK(store.Execute(request, Access().deadline).http_status == 404);
    request.operation = ObjectOperation::kList;
    const auto listed = store.Execute(request, Access().deadline);
    Q4T_CHECK(Ok(listed) && listed.value.GetArray("data") &&
              listed.value.GetArray("data")->array.empty());
  }
  for (const auto& entry :
       std::filesystem::directory_iterator(f.options.directory)) {
    if (entry.path().extension() != ".json") continue;
    struct stat info {};
    Q4T_CHECK(lstat(entry.path().c_str(), &info) == 0);
    Q4T_CHECK(S_ISREG(info.st_mode) && (info.st_mode & 077) == 0 &&
              info.st_uid == getuid());
  }
  return true;
}

Q4T_TEST(state_cipher_corrupt_ring_and_exhausted_key_fail_closed) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  auto record = store.Execute(KeyRequest(), Access().deadline);
  Q4T_CHECK(Ok(record));
  auto request = KeyRequest(ObjectOperation::kPut);
  request.expected_revision = record.value.GetInt("revision");
  request.data = *record.value.Find("data");
  Field(&Field(&request.data, "keys").array[0], "uses") =
      Parse(std::to_string(StateCipher::kMaxSeals));
  auto written = store.Execute(request, Access().deadline);
  Q4T_CHECK(Ok(written));
  std::string opaque = "unchanged";
  Q4T_CHECK(cipher.Seal("p", Binding(), Access(), &opaque).code ==
            "state_crypto_rotation_required");
  Q4T_CHECK(opaque == "unchanged");
  request.expected_revision = written.value.GetInt("revision");
  Field(&Field(&request.data, "keys").array[0], "key") = Parse("\"broken\"");
  Q4T_CHECK(Ok(store.Execute(request, Access().deadline)));
  Q4T_CHECK(cipher.Ready(Access()).code == "state_crypto_keys_invalid");
  Q4T_CHECK(cipher.Seal("p", Binding(), Access(), &opaque).code ==
            "state_crypto_keys_invalid");
  Q4T_CHECK(opaque == "unchanged");
  return true;
}

Q4T_TEST(state_cipher_concurrent_first_use_and_seal_count) {
  Fixture f;
  ObjectStore store(f.options);
  auto one = std::async(std::launch::async,
                        [&] { return StateCipher(store).Ready(Access()); });
  auto two = std::async(std::launch::async,
                        [&] { return StateCipher(store).Ready(Access()); });
  Q4T_CHECK(Ok(one.get()) && Ok(two.get()));
  std::string a, b, plain;
  one = std::async(std::launch::async, [&] {
    return StateCipher(store).Seal("same", Binding(), Access(), &a);
  });
  two = std::async(std::launch::async, [&] {
    return StateCipher(store).Seal("same", Binding(), Access(), &b);
  });
  Q4T_CHECK(Ok(one.get()) && Ok(two.get()) && a != b);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(cipher.Open(a, Binding(), Access(), &plain)) && plain == "same");
  Q4T_CHECK(Ok(cipher.Open(b, Binding(), Access(), &plain)) && plain == "same");
  const auto state = cipher.Describe(Access());
  Q4T_CHECK(Ok(state) && state.value.GetArray("keys")->array.size() == 1 &&
            state.value.GetArray("keys")->array[0].GetInt("uses") == 2);
  return true;
}

Q4T_TEST(state_cipher_deadline_and_cancel_do_not_publish_ciphertext) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(cipher.Ready(Access())));
  const auto before = cipher.Describe(Access());
  for (int mode = 0; mode < 2; ++mode) {
    auto access = Access();
    if (mode == 0) access.deadline = Clock::now() - 1ms;
    if (mode == 1) access.cancelled = [] { return true; };
    std::string opaque = "unchanged";
    Q4T_CHECK(!cipher.Seal("p", Binding(), access, &opaque).ok());
    Q4T_CHECK(opaque == "unchanged");
  }
  Q4T_CHECK(OutputJson(cipher.Describe(Access()).value) ==
            OutputJson(before.value));
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(state_cipher_unavailable_dependency_leaves_plain_responses_usable) {
  Fixture f;
  ObjectStore store(f.options);
  struct Reset {
    ~Reset() { SetCryptoUnavailableForTesting(false); }
  } reset;
  SetCryptoUnavailableForTesting(true);
  Q4T_CHECK(StateCipher(store).Ready(Access()).code ==
            "state_crypto_unavailable");
  ChatPlanConfig config;
  config.model = "cpu";
  ResponsesExecution response(store);
  Q4T_CHECK(Ok(response.Prepare(Parse(R"({"input":"q","store":false})"), config,
                                Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(response.Begin(&wire)));
  ResponseDelta text;
  text.kind = ResponseDelta::Kind::kContent;
  text.text = "42";
  Q4T_CHECK(response.Append(text, "null", &wire));
  Q4T_CHECK(Ok(response.Finish(false, 0, {1, 0, 1, 0}, 43, &wire)));
  Q4T_CHECK(response.Output().Snapshot().GetString("status") == "completed");
  Q4T_CHECK(StateCipher(store).Describe(Access()).http_status == 404);
  SetCryptoUnavailableForTesting(false);
  Q4T_CHECK(Ok(StateCipher(store).Ready(Access())));
  return true;
}
#endif
