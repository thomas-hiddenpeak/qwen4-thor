#include "q4t/server/object_retention.h"

#include <cstdlib>
#include <array>
#include <ctime>
#include <filesystem>
#include <thread>

#include "q4t/server/object_store.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("retention fixture JSON");
  return result;
}
const std::string kOwner(64, 'a'), kOther(64, 'b'), kDigest(64, 'c');
ObjectRetentionNode Node(std::string_view id, const ObjectRetentionInfo& info,
                         int64_t expires = 10, std::string_view kind = "item",
                         std::string_view owner = kOwner) {
  return {owner, kind, id, kDigest, 1, expires, 123, &info};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/object-retention-cpu");
    std::filesystem::create_directories(root);
    auto directory = (root / "retention-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("retention fixture directory");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
ObjectRequest Item(std::string id, std::string scope = "client:a") {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.scope = std::move(scope);
  request.id = std::move(id);
  request.kind = ObjectKind::kConversationItem;
  request.ttl_seconds = 0;
  request.created_at = std::time(nullptr) - 10;
  request.data = Parse(R"({"item":{"type":"message","role":"user",
    "content":"private text never present in retention receipts"}})");
  return request;
}
ObjectReply Run(ObjectStore& store, const ObjectRequest& request) {
  return store.Execute(request, Clock::now() + 5s);
}
ObjectReply Read(ObjectStore& store, ObjectRequest request) {
  request.operation = ObjectOperation::kGet;
  return Run(store, request);
}
ObjectReply Erase(ObjectStore& store, ObjectRequest request) {
  request.operation = ObjectOperation::kErase;
  return Run(store, request);
}
ObjectReply Inspect(ObjectStore& store, int64_t cutoff = 0,
                    std::string expected = {}, size_t limit = 128) {
  return store.Retention(cutoff, limit, std::move(expected), Clock::now() + 5s);
}
void Expired(int64_t until) {
  const auto deadline = Clock::now() + 2s;
  while (std::time(nullptr) < until && Clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
  if (std::time(nullptr) < until)
    throw std::runtime_error("retention fixture expiry deadline");
}
}  // namespace

Q4T_TEST(object_retention_expired_active_cas_can_finish_without_extending_expiry) {
  Fixture f;
  ObjectStore objects(f.options);
  auto root = Item("resp_preserve_expiry");
  root.kind = ObjectKind::kResponse;
  root.ttl_seconds = 1;
  root.data = Parse(
      R"({"response":{"status":"queued"},"continuation":{"scope":"client:a"}})");
  auto made = Run(objects, root);
  Q4T_CHECK(Ok(made));
  root.created_at = made.value.GetInt("created_at");
  root.expires_at = made.value.GetInt("expires_at");
  Expired(root.expires_at);
  root.expected_revision = 1;
  root.data = Parse(
      R"({"response":{"status":"in_progress"},"continuation":{"scope":"client:a"}})");
  auto progress = Run(objects, root);
  Q4T_CHECK(Ok(progress) &&
            progress.value.GetInt("expires_at") == root.expires_at &&
            progress.value.GetInt("revision") == 2);
  auto unrelated = root;
  unrelated.id = "resp_new_expired";
  unrelated.expected_revision = 0;
  Q4T_CHECK(Run(objects, unrelated).http_status == 404);
  root.expected_revision = 2;
  root.data = Parse(R"({"response":{"status":"failed"}})");
  auto item = Item("item_final_receipt");
  const std::array mutations{root, item};
  auto terminal = objects.ExecuteBatch(mutations, Clock::now() + 5s);
  Q4T_CHECK(Ok(terminal));
  const auto* results = terminal.value.GetArray("data");
  Q4T_CHECK(results && results->array.size() == 2 &&
            results->array[0].GetInt("expires_at") == root.expires_at &&
            Read(objects, root).http_status == 404 && Ok(Read(objects, item)));
  return true;
}

Q4T_TEST(
    object_retention_referenced_expired_nonactive_cannot_use_active_cas_exception) {
  Fixture f;
  ObjectStore objects(f.options);
  auto item = Item("item_old_expiry");
  item.ttl_seconds = 1;
  auto created = Run(objects, item);
  Q4T_CHECK(Ok(created));
  item.created_at = created.value.GetInt("created_at");
  item.expires_at = created.value.GetInt("expires_at");
  auto parent = Item("conv_hold_old");
  parent.kind = ObjectKind::kConversation;
  parent.data = Parse(R"({"items":[{"id":"item_old_expiry"}]})");
  Q4T_CHECK(Ok(Run(objects, parent)));
  Expired(item.expires_at);
  Q4T_CHECK(Ok(Read(objects, item)));
  item.expected_revision = 1;
  Q4T_CHECK(Run(objects, item).http_status == 404);
  const std::array mutations{item};
  Q4T_CHECK(objects.ExecuteBatch(mutations, Clock::now() + 5s).http_status ==
            404);
  auto unchanged = Read(objects, item);
  Q4T_CHECK(Ok(unchanged) && unchanged.value.GetInt("revision") == 1 &&
            unchanged.value.GetInt("expires_at") == item.expires_at);
  return true;
}

Q4T_TEST(object_retention_extracts_typed_dependencies_not_user_strings) {
  auto info = InspectObjectRetention("response", Parse(R"({
    "response":{"status":"in_progress","output":[{"id":"msg_1",
      "content":[{"file_id":"file_1"}]}]},
    "request":{"previous_response_id":"resp_1",
      "conversation":{"id":"conv_1"},"prompt":{"id":"prompt_1"},
      "input":[{"id":"item_1","arguments":"file_fake"}],
      "metadata":{"id":"fake_metadata"}},
    "history":[{"type":"item_reference","id":"old_1"}],
    "own_input":[{"type":"message","content":"resp_fake"}],
    "continuation":{"conversation_id":"conv_1"}})"));
  Q4T_CHECK(info.active && !info.uncertain);
  Q4T_CHECK((info.references ==
             std::vector<std::string>{"conv_1", "file_1", "item_1", "msg_1",
                                      "old_1", "prompt_1", "resp_1"}));
  const auto prompt = InspectObjectRetention("prompt", Parse(R"({"versions":[
    {"definition":{"request":{"input":[{"id":"item_1"}]}}},
    {"definition":{"request":{"previous_response_id":"resp_1"}}}]})"));
  Q4T_CHECK(!prompt.uncertain && prompt.references.size() == 2);
  for (const auto* kind : {"response", "conversation", "item", "chat", "prompt",
                           "batch", "file", "tool_run"})
    Q4T_CHECK(InspectObjectRetention(kind, Parse("{}")).uncertain);
  Q4T_CHECK(InspectObjectRetention("response", Parse(R"({
    "response":{"status":"future"},"request":{"input":[{"id":2}]}})"))
                .uncertain);
  return true;
}

Q4T_TEST(object_retention_active_roots_preserve_chain_with_owner_isolation) {
  ObjectRetentionInfo active{true, false, {"item_1"}};
  ObjectRetentionInfo first{false, false, {"item_2"}}, leaf;
  std::vector nodes{Node("resp_1", active, 10, "response"),
                    Node("item_1", first), Node("item_2", leaf),
                    Node("item_1", leaf, 10, "item", kOther)};
  ObjectRetentionPlan plan;
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 0, 128, &plan));
  Q4T_CHECK(plan.active == 1 && plan.referenced == 2 && plan.eligible == 1 &&
            plan.selected == std::vector<size_t>{3});
  active.active = false;
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 0, 2, &plan));
  Q4T_CHECK(plan.eligible == 4 && plan.selected.size() == 2 &&
            plan.bytes == 246);
  return true;
}

Q4T_TEST(object_retention_unrooted_cycles_and_explicit_permanent_cutoff) {
  ObjectRetentionInfo a{false, false, {"item_b"}}, b{false, false, {"item_a"}},
      root{false, false, {"item_a"}};
  std::vector nodes{Node("item_a", a, 0), Node("item_b", b, 0)};
  ObjectRetentionPlan plan;
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 0, 128, &plan) &&
            plan.eligible == 0);
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 1, 128, &plan) &&
            plan.eligible == 0);
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 2, 128, &plan) &&
            plan.eligible == 2);
  nodes.push_back(Node("conv_1", root, 0, "conversation"));
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 2, 128, &plan) &&
            plan.eligible == 0 && plan.referenced == 2);
  nodes.pop_back();
  nodes[0].expires = nodes[1].expires = 10;
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 0, 128, &plan) &&
            plan.eligible == 2);
  return true;
}

Q4T_TEST(object_retention_unknown_owner_protection_and_plan_identity) {
  ObjectRetentionInfo unknown{false, true, {}}, leaf;
  std::vector nodes{Node("item_a", leaf),
                    Node("resp_1", unknown, 10, "response"),
                    Node("item_b", leaf, 10, "item", kOther)};
  ObjectRetentionPlan plan, again;
  Q4T_CHECK(PlanObjectRetention(nodes, 20, 0, 128, &plan) &&
            plan.uncertain == 2 && plan.selected == std::vector<size_t>{2});
  Q4T_CHECK(PlanObjectRetention(nodes, 21, 0, 128, &again) &&
            again.sha256 == plan.sha256);
  nodes[2].digest = kOwner;
  Q4T_CHECK(PlanObjectRetention(nodes, 21, 0, 128, &again) &&
            again.sha256 != plan.sha256);
  Q4T_CHECK(!PlanObjectRetention(nodes, 20, 21, 128, &plan));
  Q4T_CHECK(!PlanObjectRetention(nodes, 20, 0, 129, &plan));
  nodes[0].retention = nullptr;
  Q4T_CHECK(!PlanObjectRetention(nodes, 20, 0, 1, &plan));
  return true;
}

Q4T_TEST(object_retention_reference_capacity_preserves_incomplete_owner) {
  std::string raw = R"({"items":[)";
  // Exceed the 4 MiB reference budget, including each std::string slot.
  // The previous 30K short references fit the expanded Batch checkpoint cap.
  for (size_t i = 0; i < 100000; ++i) {
    if (i) raw += ',';
    raw += R"({"id":"item_1234567890"})";
  }
  raw += "]}";
  const auto info = InspectObjectRetention("conversation", Parse(raw));
  Q4T_CHECK(info.uncertain && info.references.empty() && info.Bytes() == 0);
  return true;
}

Q4T_TEST(object_retention_expired_active_survives_restart_and_recovery_scan) {
  Fixture f;
  auto request = Item("resp_active");
  request.kind = ObjectKind::kResponse;
  request.ttl_seconds = 1;
  request.data = Parse(R"({"response":{"status":"queued","background":true},
    "continuation":{"scope":"client:a","execution_deadline_at":1}})");
  int64_t expires = 0;
  {
    ObjectStore objects(f.options);
    const auto created = Run(objects, request);
    Q4T_CHECK(Ok(created));
    expires = created.value.GetInt("expires_at");
  }
  Expired(expires);
  ObjectStore objects(f.options);
  auto inspect = Inspect(objects);
  Q4T_CHECK(Ok(inspect) && inspect.value.GetInt("protected_active") == 1 &&
            inspect.value.GetInt("eligible_objects") == 0);
  const auto scan = objects.ScanResponses({}, Clock::now() + 5s);
  Q4T_CHECK(Ok(scan) && scan.value.GetArray("data")->array.size() == 1 &&
            scan.value.GetArray("data")->array[0].GetString("scope") ==
                "client:a");
  request.expected_revision = 1;
  request.data = Parse(R"({"response":{"status":"failed"}})");
  const auto terminal = Run(objects, request);
  Q4T_CHECK(Ok(terminal) && terminal.value.GetInt("revision") == 2);
  Expired(terminal.value.GetInt("expires_at"));
  Q4T_CHECK(Read(objects, request).http_status == 404);
  return true;
}

Q4T_TEST(
    object_retention_automatic_expiry_preserves_live_same_owner_reference) {
  Fixture f;
  ObjectStore objects(f.options);
  auto own = Item("item_same"), other = Item("item_same", "client:b");
  own.ttl_seconds = other.ttl_seconds = 1;
  const auto a = Run(objects, own), b = Run(objects, other);
  Q4T_CHECK(Ok(a) && Ok(b));
  auto root = Item("conv_1");
  root.kind = ObjectKind::kConversation;
  root.data = Parse(R"({"items":[{"id":"item_same"}]})");
  Q4T_CHECK(Ok(Run(objects, root)));
  Expired(std::max(a.value.GetInt("expires_at"), b.value.GetInt("expires_at")));
  Q4T_CHECK(Ok(Read(objects, own)) && Read(objects, other).http_status == 404);
  Q4T_CHECK(Ok(Erase(objects, root)));
  Q4T_CHECK(Read(objects, own).http_status == 404);
  return true;
}

Q4T_TEST(object_retention_inspect_is_readonly_and_stale_plan_cannot_collect) {
  Fixture f;
  auto item = Item("item_1"), extra = Item("item_2");
  const auto cutoff = std::time(nullptr);
  {
    ObjectStore objects(f.options);
    Q4T_CHECK(Ok(Run(objects, item)));
    auto first = Inspect(objects, cutoff);
    Q4T_CHECK(Ok(first) && !first.value.GetBool("applied") &&
              first.value.GetInt("eligible_objects") == 1 &&
              Ok(Read(objects, item)));
    const auto serialized = OutputJson(first.value);
    Q4T_CHECK(serialized.find("private text") == std::string::npos &&
              serialized.find("client:a") == std::string::npos);
    auto root = Item("conv_1");
    root.kind = ObjectKind::kConversation;
    root.data = Parse(R"({"items":[{"id":"item_1"}]})");
    Q4T_CHECK(Ok(Run(objects, root)));
    Q4T_CHECK(
        Inspect(objects, cutoff, first.value.GetString("plan_sha256")).code ==
        "retention_plan_changed");
    Q4T_CHECK(Ok(Read(objects, item)) && Ok(Erase(objects, root)) &&
              Ok(Run(objects, extra)));
    auto current = Inspect(objects, cutoff);
    Q4T_CHECK(Ok(current) && current.value.GetInt("eligible_objects") == 2);
    const auto collected =
        Inspect(objects, cutoff, current.value.GetString("plan_sha256"));
    Q4T_CHECK(Ok(collected) && collected.value.GetInt("deleted_objects") == 2);
    auto health = item;
    health.operation = ObjectOperation::kHealth;
    Q4T_CHECK(Run(objects, health).value.GetInt("objects", -1) == 0);
  }
  ObjectStore reopened(f.options);
  Q4T_CHECK(Read(reopened, item).http_status == 404 &&
            Read(reopened, extra).http_status == 404);
  return true;
}

Q4T_TEST(object_retention_batch_limits_and_multiple_owners_require_new_plan) {
  Fixture f;
  ObjectStore objects(f.options);
  for (const auto& scope : {"client:a", "client:b"})
    for (const auto& id : {"item_1", "item_2"})
      Q4T_CHECK(Ok(Run(objects, Item(id, scope))));
  const auto cutoff = std::time(nullptr);
  for (int remaining : {4, 3, 2, 1}) {
    const auto plan = Inspect(objects, cutoff, {}, 1);
    Q4T_CHECK(Ok(plan) && plan.value.GetInt("eligible_objects") == remaining &&
              plan.value.GetArray("candidates")->array.size() == 1 &&
              plan.value.GetBool("has_more") == (remaining > 1));
    const auto result =
        Inspect(objects, cutoff, plan.value.GetString("plan_sha256"), 1);
    Q4T_CHECK(Ok(result) && result.value.GetInt("deleted_objects") == 1);
  }
  Q4T_CHECK(Inspect(objects, cutoff).value.GetInt("eligible_objects", -1) == 0);
  Q4T_CHECK(Inspect(objects, cutoff + 3600).http_status == 400);
  Q4T_CHECK(Inspect(objects, cutoff, "bad").http_status == 400);
  return true;
}

Q4T_TEST(
    object_retention_unknown_state_preserves_owner_without_blocking_reads) {
  Fixture f;
  ObjectStore objects(f.options);
  auto item = Item("item_1"), unknown = Item("resp_1");
  item.ttl_seconds = unknown.ttl_seconds = 1;
  unknown.kind = ObjectKind::kResponse;
  unknown.data = Parse(R"({"response":{"status":"future_status"}})");
  auto created = Run(objects, item), other = Run(objects, unknown);
  Q4T_CHECK(Ok(created) && Ok(other));
  Expired(std::max(created.value.GetInt("expires_at"),
                   other.value.GetInt("expires_at")));
  Q4T_CHECK(Ok(Read(objects, item)) && Ok(Read(objects, unknown)));
  auto plan = Inspect(objects);
  Q4T_CHECK(Ok(plan) && plan.value.GetInt("protected_uncertain") == 2 &&
            plan.value.GetInt("eligible_objects") == 0);
  Q4T_CHECK(Ok(Erase(objects, unknown)) &&
            Read(objects, item).http_status == 404);
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(object_retention_partial_atomic_collection_recovers_without_replay) {
  Fixture f;
  f.options.io_timeout_ms = 250;
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-apply-pause");
  ObjectStore objects(f.options);
  Q4T_CHECK(Ok(Run(objects, Item("item_1"))) &&
            Ok(Run(objects, Item("item_2"))));
  const auto cutoff = std::time(nullptr);
  const auto plan = Inspect(objects, cutoff);
  Q4T_CHECK(Ok(plan));
  const auto lost =
      Inspect(objects, cutoff, plan.value.GetString("plan_sha256"));
  Q4T_CHECK(!lost.ok() && lost.commit_unknown);
  const auto deadline = Clock::now() + 1s;
  while (objects.Health().pending && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(std::filesystem::exists(std::filesystem::path(f.options.directory) /
                                    ".q4t-transaction"));
  auto recovered = Inspect(objects, cutoff);
  Q4T_CHECK(Ok(recovered) && recovered.value.GetInt("eligible_objects") == 0 &&
            objects.Health().workers_started == 2);
  Q4T_CHECK(Read(objects, Item("item_1")).http_status == 404 &&
            Read(objects, Item("item_2")).http_status == 404);
  Q4T_CHECK(
      Inspect(objects, cutoff, plan.value.GetString("plan_sha256")).code ==
      "retention_plan_changed");
  return true;
}
#endif
