#include "q4t/server/input_snapshot.h"

#include <array>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
const std::string kBinding =
    Digest("original input part and processing profile");
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root = fs::absolute(out ? out : ".q4t-work/input-snapshot-cpu");
    fs::create_directories(root);
    auto path = (root / "snapshot-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("snapshot fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:input") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
ObjectRequest Request(ObjectKind kind, std::string id = {},
                      ObjectOperation operation = ObjectOperation::kGet) {
  ObjectRequest request;
  request.scope = "client:input";
  request.kind = kind;
  request.id = std::move(id);
  request.operation = operation;
  request.ttl_seconds = 0;
  return request;
}
ObjectReply Run(ObjectStore& objects, const ObjectRequest& request) {
  return objects.Execute(request, Clock::now() + 10s);
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
int64_t Count(ObjectStore& objects) {
  return Run(objects, Request(ObjectKind::kFile, {}, ObjectOperation::kHealth))
      .value.GetInt("objects", -1);
}
ObjectReply Save(InputSnapshotStore& snapshots, InputSnapshotRef* reference,
                 InputSnapshotHold* hold,
                 const BinaryAccess& access = Access()) {
  const std::string_view bytes = "actual payload";
  return snapshots.Save(access, kBinding, Object({{"text", String("中文")}}),
                        std::span(&bytes, 1), {}, 60, reference, hold);
}
ObjectRequest Parent(ObjectKind kind, const InputSnapshotRef& reference) {
  auto request = Request(kind, NewObjectId(kind), ObjectOperation::kPut);
  auto binding = Object({{"item_id", String("msg_original")},
                         {"part", Number(0)},
                         {"snapshot", InputSnapshotReference(reference)}});
  request.data = Object({{"input_snapshots", Array({binding})}});
  if (kind == ObjectKind::kResponse)
    Set(&request.data, "response",
        Object({{"status", String("completed")}, {"output", Array()}}));
  if (kind == ObjectKind::kChat) Set(&request.data, "messages", Array());
  if (kind == ObjectKind::kConversation) Set(&request.data, "items", Array());
  if (kind == ObjectKind::kConversationItem)
    Set(&request.data, "item", Object({{"type", String("message")}}));
  return request;
}
struct Injection {
  explicit Injection(std::string entry) {
    ObjectWorkerEntryForTesting(std::move(entry));
  }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
void Reaped(ObjectStore& objects) {
  const auto deadline = Clock::now() + 1s;
  while (objects.Health().pending && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  if (objects.Health().pending) throw std::runtime_error("snapshot worker");
}
}  // namespace

Q4T_TEST(input_snapshot_exact_binary_metadata_and_empty_parts_survive_restart) {
  Fixture fixture;
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  std::string bytes(kBinaryChunkBytes + 23, '\0');
  for (size_t i = 0; i < bytes.size(); ++i) bytes[i] = i % 256;
  const auto metadata = Object({{"text", String("实际文档 42")},
                                {"url", String("https://example.com/original")},
                                {"source_sha256", String(Digest(bytes))}});
  const std::array<std::string_view, 3> parts{bytes, "", "actual page PNG"};
  {
    ObjectStore objects(fixture.options);
    InputSnapshotStore snapshots(objects);
    Q4T_CHECK(Ok(snapshots.Save(Access(), kBinding, metadata, parts, {}, 60,
                                &reference, &hold)));
    Q4T_CHECK(
        Run(objects, Request(ObjectKind::kFile, {}, ObjectOperation::kList))
            .value.GetArray("data")
            ->array.empty());
    Q4T_CHECK(!hold.commit_unknown &&
              reference.id.starts_with("input_snapshot_"));
  }
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshot value;
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &value)) &&
            OutputJson(value.metadata) == OutputJson(metadata) &&
            value.parts ==
                std::vector<std::string>(parts.begin(), parts.end()));
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)) && Count(objects) == 0);
  return true;
}

Q4T_TEST(
    input_snapshot_live_application_roots_retain_and_last_delete_reclaims) {
  Fixture fixture;
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  std::vector<ObjectRequest> parents;
  {
    ObjectStore objects(fixture.options);
    InputSnapshotStore snapshots(objects);
    Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
    for (auto kind : {ObjectKind::kResponse, ObjectKind::kConversation,
                      ObjectKind::kConversationItem, ObjectKind::kChat}) {
      parents.push_back(Parent(kind, reference));
      Q4T_CHECK(Ok(Run(objects, parents.back())));
    }
    Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  }
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  for (auto& parent : parents) {
    InputSnapshot value;
    Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &value)) &&
              value.parts[0] == "actual payload");
    parent.operation = ObjectOperation::kErase;
    Q4T_CHECK(Ok(Run(objects, parent)));
  }
  Q4T_CHECK(Count(objects) == 0);
  InputSnapshot value;
  Q4T_CHECK(
      snapshots.Read(Access(), reference, kBinding, {}, &value).http_status ==
          404 &&
      value.parts.empty());
  return true;
}

Q4T_TEST(
    input_snapshot_abandoned_preparation_expires_without_permanent_orphans) {
  Fixture fixture;
  int64_t expires = 0;
  {
    ObjectStore objects(fixture.options);
    InputSnapshotStore snapshots(objects);
    InputSnapshotRef reference;
    InputSnapshotHold hold;
    auto access = Access();
    access.deadline = Clock::now() + 500ms;
    Q4T_CHECK(Ok(snapshots.Save(access, kBinding, Object(), {}, {}, 2,
                                &reference, &hold)));
    expires = hold.expires_at;
    Q4T_CHECK(Count(objects) == 3);
  }
  const auto limit = Clock::now() + 3s;
  while (std::time(nullptr) < expires && Clock::now() < limit)
    std::this_thread::sleep_for(5ms);
  Q4T_CHECK(std::time(nullptr) >= expires);
  ObjectStore objects(fixture.options);
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_scope_binding_root_digest_and_pinned_metadata) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  const auto count = Count(objects);
  InputSnapshot value;
  Q4T_CHECK(snapshots.Read(Access("other"), reference, kBinding, {}, &value)
                    .http_status == 404 &&
            value.parts.empty());
  Q4T_CHECK(!snapshots.Release(Access("other"), &hold).ok());
  Q4T_CHECK(
      snapshots.Read(Access(), reference, Digest("different input"), {}, &value)
          .code == "input_snapshot_inconsistent");
  auto wrong = reference;
  wrong.sha256 = Digest("other root");
  Q4T_CHECK(snapshots.Read(Access(), wrong, kBinding, {}, &value).code ==
                "input_snapshot_inconsistent" &&
            value.parts.empty());
  Q4T_CHECK(Count(objects) == count);
  BinaryStore binary(objects);
  BinaryRead read;
  Json pinned;
  Q4T_CHECK(Ok(binary.Open(Access(), ObjectKind::kInputSnapshot, reference.id,
                           60, &read, &pinned)) &&
            Digest(OutputJson(pinned)) == reference.sha256);
  Q4T_CHECK(Ok(binary.Close(Access(), &read)));
  auto request = Request(ObjectKind::kInputSnapshot, reference.id);
  const auto record = Run(objects, request);
  Q4T_CHECK(Ok(record));
  request.data = *record.value.Find("data");
  request.operation = ObjectOperation::kPut;
  request.expected_revision = record.value.GetInt("revision");
  Set(&request.data, "binding_sha256", String(Digest("tampered")));
  Q4T_CHECK(Run(objects, request).code == "input_snapshot_immutable");
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &value)) &&
            value.parts == std::vector<std::string>{"actual payload"});
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)) && Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_allocation_budgets_and_known_quota_failure_are_atomic) {
  Fixture fixture;
  fixture.options.max_client_objects = 4;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  const std::array<std::string_view, 2> parts{"first", "second"};
  const auto failed = snapshots.Save(Access(), kBinding, Object(), parts, {},
                                     60, &reference, &hold);
  Q4T_CHECK(failed.code == "object_quota_exceeded" && !failed.commit_unknown &&
            reference.id.empty() && hold.id.empty() && Count(objects) == 0);
  InputSnapshotLimits limits;
  limits.max_parts = 1;
  Q4T_CHECK(snapshots
                .Save(Access(), kBinding, Object(), parts, limits, 60,
                      &reference, &hold)
                .http_status == 413);
  limits = {};
  limits.max_bytes = limits.max_metadata_bytes = 16;
  Q4T_CHECK(snapshots
                .Save(Access(), kBinding,
                      Object({{"text", String(std::string(32, 'x'))}}), {},
                      limits, 60, &reference, &hold)
                .http_status == 413);
  Q4T_CHECK(Count(objects) == 0);
  Q4T_CHECK(Ok(snapshots.Save(Access(), kBinding, Object(), {}, {}, 60,
                              &reference, &hold)));
  InputSnapshot value;
  limits.max_bytes = limits.max_metadata_bytes = 1;
  Q4T_CHECK(snapshots.Read(Access(), reference, kBinding, limits, &value)
                    .http_status == 413 &&
            value.metadata.IsNull());
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)) && Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_cancel_between_durable_chunks_aborts_only_own_stage) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  auto access = Access();
  bool observed = false;
  access.cancelled = [&] {
    // Cancel between acknowledged IO operations, not during an unknown commit.
    if (objects.Health().pending) return false;
    size_t files = 0;
    if (fs::exists(fixture.options.directory))
      for (const auto& item : fs::directory_iterator(fixture.options.directory))
        files += item.path().extension() == ".json";
    observed = observed || files >= 2;
    return observed;
  };
  const std::string bytes(3 * kBinaryChunkBytes, 'x');
  const std::string_view part(bytes);
  const auto failed =
      snapshots.Save(access, kBinding, Object(), std::span(&part, 1), {}, 60,
                     &reference, &hold);
  Q4T_CHECK(failed.http_status == 499 && observed && !failed.commit_unknown &&
            reference.id.empty() && hold.id.empty() && Count(objects) == 0);
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  InputSnapshot value;
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(
      snapshots.Read(expired, reference, kBinding, {}, &value).http_status ==
          408 &&
      value.parts.empty());
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  return true;
}

Q4T_TEST(input_snapshot_unknown_stage_commit_is_not_replayed_or_erased) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  Injection injection("--q4t-object-worker-lost-ack");
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  const auto failed = Save(snapshots, &reference, &hold);
  Q4T_CHECK(failed.commit_unknown && hold.commit_unknown &&
            failed.value.GetString("id") == hold.id && reference.id.empty());
  Q4T_CHECK(Save(snapshots, &reference, &hold).commit_unknown &&
            snapshots.Release(Access(), &hold).commit_unknown);
  Reaped(objects);
  Q4T_CHECK(Count(objects) == 1 && objects.Health().workers_started == 2);
  return true;
}

Q4T_TEST(input_snapshot_unknown_publish_recovers_original_root_and_hold) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  Injection injection("--q4t-object-worker-snapshot-lost-ack");
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  const auto failed = Save(snapshots, &reference, &hold);
  Q4T_CHECK(failed.commit_unknown && hold.commit_unknown &&
            failed.value.GetString("id") == reference.id);
  Q4T_CHECK(Save(snapshots, &reference, &hold).commit_unknown &&
            snapshots.Release(Access(), &hold).commit_unknown);
  Reaped(objects);
  Q4T_CHECK(Count(objects) == 4);
  InputSnapshot value;
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &value)) &&
            value.parts == std::vector<std::string>{"actual payload"});
  Q4T_CHECK(Count(objects) == 4 && objects.Health().workers_started == 2);
  return true;
}

Q4T_TEST(input_snapshot_retention_uses_only_private_typed_bindings) {
  const InputSnapshotRef reference{"input_snapshot_original", Digest("root")};
  auto parent = Parent(ObjectKind::kChat, reference);
  const auto info = InspectObjectRetention("chat", parent.data);
  Q4T_CHECK(!info.uncertain &&
            info.references == std::vector<std::string>{reference.id});
  auto unrelated = Object({{"messages", Array()}, {"metadata", parent.data}});
  Q4T_CHECK(InspectObjectRetention("chat", unrelated).references.empty());
  auto& snapshot =
      Field(Field(parent.data, "input_snapshots").array[0], "snapshot");
  Set(&snapshot, "sha256", String("bad digest"));
  Q4T_CHECK(InspectObjectRetention("chat", parent.data).uncertain);
  return true;
}

Q4T_TEST(input_snapshot_long_parent_deadline_preserves_absolute_hold) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  auto access = Access();
  access.deadline = Clock::now() + 8h;
  InputSnapshotRef reference;
  InputSnapshotHold hold, pin;
  const auto before = std::time(nullptr);
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold, access)));
  Q4T_CHECK(hold.expires_at >= before + 60 &&
            hold.expires_at <= std::time(nullptr) + 60);
  Q4T_CHECK(Ok(snapshots.Pin(access, reference, kBinding, 60, &pin)));
  Q4T_CHECK(pin.expires_at <= std::time(nullptr) + 60);
  InputSnapshot restored;
  Q4T_CHECK(Ok(snapshots.Read(access, reference, kBinding, {}, &restored)) &&
            restored.parts == std::vector<std::string>{"actual payload"});
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)) &&
            Ok(snapshots.Release(Access(), &pin)) && Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_actual_lost_cleanup_receipt_keeps_verified_content) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 100;
  Injection injection("--q4t-object-worker-read-close-lost-ack");
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  InputSnapshot restored;
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &restored)));
  Q4T_CHECK(restored.parts == std::vector<std::string>{"actual payload"} &&
            restored.metadata.GetString("text") == "中文" &&
            objects.Health().auxiliary_cleanup_failures == 1);
  Reaped(objects);
  // A real content validation error retains its cause even when the second
  // read lease's cleanup also loses its receipt in the replacement IO worker.
  const auto bad = snapshots.Read(Access(), reference, Digest("wrong binding"),
                                  {}, &restored);
  Q4T_CHECK(bad.code == "input_snapshot_inconsistent" &&
            restored.parts.empty() &&
            objects.Health().auxiliary_cleanup_failures == 2);
  Reaped(objects);
  Q4T_CHECK(objects.Health().workers_started == 2);
  return true;
}

Q4T_TEST(input_snapshot_parent_publication_requires_owned_exact_live_root) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  auto wrong = reference;
  wrong.sha256 = Digest("different immutable content");
  for (const auto kind :
       {ObjectKind::kResponse, ObjectKind::kChat, ObjectKind::kConversation,
        ObjectKind::kConversationItem}) {
    auto parent = Parent(kind, wrong);
    Q4T_CHECK(Run(objects, parent).code == "input_snapshot_reference_conflict");
    parent.operation = ObjectOperation::kGet;
    Q4T_CHECK(Run(objects, parent).http_status == 404);
  }
  auto parent = Parent(ObjectKind::kChat, reference);
  parent.scope = "client:other";
  Q4T_CHECK(Run(objects, parent).code == "input_snapshot_reference_conflict");
  parent.scope = Access().scope;
  auto& binding = Field(parent.data, "input_snapshots").array[0];
  Set(&binding, "binding_sha256", String(Digest("wrong original part")));
  Q4T_CHECK(Run(objects, parent).code == "input_snapshot_reference_conflict");
  Set(&binding, "binding_sha256", String(kBinding));
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)) && Count(objects) == 0);
  Q4T_CHECK(Run(objects, parent).code == "input_snapshot_reference_conflict" &&
            Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_failed_reference_update_leaves_atomic_group_unchanged) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  auto parent = Parent(ObjectKind::kChat, reference);
  Q4T_CHECK(Ok(Run(objects, parent)));
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  auto original = parent.data;
  parent.expected_revision = 1;
  auto& snapshot =
      Field(Field(parent.data, "input_snapshots").array[0], "snapshot");
  Set(&snapshot, "sha256", String(Digest("wrong root identity")));
  auto sibling =
      Request(ObjectKind::kChat, "chatcmpl_sibling", ObjectOperation::kPut);
  sibling.data = Object({{"messages", Array()}});
  const auto failed =
      objects.ExecuteBatch(std::array{sibling, parent}, Access().deadline);
  Q4T_CHECK(failed.code == "input_snapshot_reference_conflict" &&
            !failed.commit_unknown);
  sibling.operation = ObjectOperation::kGet;
  Q4T_CHECK(Run(objects, sibling).http_status == 404);
  parent.operation = ObjectOperation::kGet;
  const auto read = Run(objects, parent);
  Q4T_CHECK(Ok(read) && read.value.GetInt("revision") == 1 &&
            OutputJson(*read.value.Find("data")) == OutputJson(original));
  return true;
}

Q4T_TEST(input_snapshot_parent_and_new_root_publish_in_either_batch_order) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  const auto stored =
      Run(objects, Request(ObjectKind::kInputSnapshot, reference.id));
  Q4T_CHECK(Ok(stored));
  std::vector<InputSnapshotRef> clones;
  std::vector<ObjectRequest> parents;
  for (const bool parent_first : {false, true}) {
    auto root =
        Request(ObjectKind::kInputSnapshot,
                NewObjectId(ObjectKind::kInputSnapshot), ObjectOperation::kPut);
    root.data = *stored.value.Find("data");
    InputSnapshotRef clone{root.id, reference.sha256};
    auto parent = Parent(ObjectKind::kChat, clone);
    const auto group =
        parent_first ? std::array{parent, root} : std::array{root, parent};
    Q4T_CHECK(Ok(objects.ExecuteBatch(group, Access().deadline)));
    clones.push_back(clone);
    parents.push_back(parent);
  }
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  for (size_t i = 0; i < clones.size(); ++i) {
    InputSnapshot restored;
    Q4T_CHECK(
        Ok(snapshots.Read(Access(), clones[i], kBinding, {}, &restored)) &&
        restored.parts == std::vector<std::string>{"actual payload"});
    parents[i].operation = ObjectOperation::kErase;
    Q4T_CHECK(Ok(Run(objects, parents[i])));
  }
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_direct_mutation_or_delete_cannot_break_live_parent) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  auto parent = Parent(ObjectKind::kChat, reference);
  Q4T_CHECK(Ok(Run(objects, parent)) && Ok(snapshots.Release(Access(), &hold)));
  auto root = Request(ObjectKind::kInputSnapshot, reference.id);
  const auto saved = Run(objects, root);
  Q4T_CHECK(Ok(saved));
  root.operation = ObjectOperation::kPut;
  root.data = *saved.value.Find("data");
  root.expected_revision = saved.value.GetInt("revision");
  Set(&root.data, "binding_sha256", String(Digest("changed")));
  Q4T_CHECK(Run(objects, root).code == "input_snapshot_immutable");
  Q4T_CHECK(objects.ExecuteBatch(std::span(&root, 1), Access().deadline).code ==
            "input_snapshot_immutable");
  root.data = *saved.value.Find("data");
  root.ttl_seconds = 60;
  Q4T_CHECK(Run(objects, root).code == "input_snapshot_immutable");
  root.operation = ObjectOperation::kErase;
  Q4T_CHECK(Run(objects, root).code == "input_snapshot_immutable");
  parent.expected_revision = 1;
  Q4T_CHECK(
      objects.ExecuteBatch(std::array{root, parent}, Access().deadline).code ==
      "input_snapshot_immutable");
  InputSnapshot restored;
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &restored)) &&
            restored.parts == std::vector<std::string>{"actual payload"});
  return true;
}

Q4T_TEST(input_snapshot_expired_preparation_cannot_publish_dangling_parent) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(snapshots.Save(Access(), kBinding, Object(), {}, {}, 2,
                              &reference, &hold)));
  auto parent = Parent(ObjectKind::kChat, reference);
  const auto until = Clock::now() + 3s;
  while (std::time(nullptr) < hold.expires_at && Clock::now() < until)
    std::this_thread::sleep_for(5ms);
  Q4T_CHECK(std::time(nullptr) >= hold.expires_at);
  // The next worker operation prunes the expired hold/root before deciding
  // whether the parent can commit. There is no accepted dangling reference.
  Q4T_CHECK(Run(objects, parent).code == "input_snapshot_reference_conflict" &&
            Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_prompt_private_bindings_participate_in_retention) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  auto parent = Parent(ObjectKind::kPrompt, reference);
  Set(&parent.data, "versions", Array());
  Q4T_CHECK(Ok(Run(objects, parent)) && Ok(snapshots.Release(Access(), &hold)));
  InputSnapshot restored;
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, kBinding, {}, &restored)) &&
            restored.parts == std::vector<std::string>{"actual payload"});
  parent.operation = ObjectOperation::kErase;
  Q4T_CHECK(Ok(Run(objects, parent)) && Count(objects) == 0);
  return true;
}

Q4T_TEST(input_snapshot_conditioned_parent_write_keeps_single_object_receipt) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, &reference, &hold)));
  auto parent = Parent(ObjectKind::kChat, reference);
  parent.mutate_before = std::time(nullptr) + 60;
  const auto first = Run(objects, parent);
  Q4T_CHECK(Ok(first) && first.value.GetString("id") == parent.id &&
            first.value.GetInt("revision") == 1 &&
            OutputJson(*first.value.Find("data")) == OutputJson(parent.data));
  Set(&parent.data, "content", String(std::string(300 * 1024, 'x')));
  parent.expected_revision = 1;
  const auto large = Run(objects, parent);
  Q4T_CHECK(Ok(large) && !large.commit_unknown &&
            large.value.GetString("id") == parent.id &&
            large.value.GetInt("revision") == 2 &&
            OutputJson(*large.value.Find("data")) == OutputJson(parent.data));
  parent.expected_revision = 2;
  parent.mutate_before = std::time(nullptr) - 1;
  const auto late = Run(objects, parent);
  Q4T_CHECK(late.code == "object_mutation_expired" && !late.commit_unknown);
  parent.operation = ObjectOperation::kGet;
  parent.mutate_before = 0;
  Q4T_CHECK(Run(objects, parent).value.GetInt("revision") == 2);
  return true;
}
