#include "q4t/server/application_payload.h"

#include <cstdlib>
#include <filesystem>
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
const std::string kField(kApplicationPayloadField);
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(out ? out : ".q4t-work/application-payload-cpu");
    fs::create_directories(root);
    auto path = (root / "payload-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("payload fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:payload") {
  return {std::move(scope), Clock::now() + 30s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
Json Data(std::string value) {
  return Object(
      {{"messages", Array()},
       {"request", Object({{"file_data", String(std::move(value))}})}});
}
ObjectRequest Request(std::string id, ObjectOperation operation) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.id = std::move(id);
  request.kind = ObjectKind::kChat;
  request.operation = operation;
  request.ttl_seconds = 0;
  return request;
}
ObjectReply Save(ObjectStore& objects, const std::string& id, Json data) {
  auto request = Request(id, ObjectOperation::kPut);
  request.data = std::move(data);
  return objects.Execute(request, Access().deadline);
}
Json& FixtureField(Json& value, const std::string& name) {
  for (auto& [key, child] : value.object)
    if (key == name) return child;
  throw std::runtime_error("missing payload fixture field");
}
Json& Ref(Json& value) {
  return FixtureField(FixtureField(value, kField), "strings").array[0];
}
ApplicationPayloadLimits Small() {
  ApplicationPayloadLimits limits;
  limits.inline_bytes = 1024;
  return limits;
}
int64_t Count(ObjectStore& objects) {
  auto request = Request({}, ObjectOperation::kHealth);
  return objects.Execute(request, Access().deadline)
      .value.GetInt("objects", -1);
}
}  // namespace

Q4T_TEST(application_payload_plain_values_remain_exact_without_storage_io) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto original = Data("原始资料\nquote \" / ~ and \\ unchanged");
  ApplicationPayloads payloads(objects, Access());
  Json encoded, restored;
  Q4T_CHECK(Ok(payloads.Encode(original, &encoded)));
  Q4T_CHECK(OutputJson(encoded) == OutputJson(original) &&
            !encoded.Find(kField));
  Q4T_CHECK(
      Ok(RestoreApplicationPayload(objects, Access(), encoded, {}, &restored)));
  Q4T_CHECK(OutputJson(restored) == OutputJson(original));
  Q4T_CHECK(objects.Health().workers_started == 0);
  payloads.Published(false);
  Q4T_CHECK(!payloads.Encode(original, &encoded).ok());
  return true;
}

Q4T_TEST(application_payload_over_16mib_exact_bytes_dedup_and_restart) {
  Fixture fixture;
  std::string bytes(17ULL << 20, 'A');
  bytes.replace(0, 10, "原字节\n");
  bytes += "last-byte";
  const auto original = Object({{"messages", Array()},
                                {"request", Data(bytes)},
                                {"history", Array({Data(bytes)})}});
  const auto id = NewObjectId(ObjectKind::kChat);
  std::string snapshot;
  {
    ObjectStore objects(fixture.options);
    ApplicationPayloads payloads(objects, Access());
    Json encoded;
    Q4T_CHECK(Ok(payloads.Encode(original, &encoded)));
    Q4T_CHECK(OutputJson(encoded).size() < 4096);
    std::vector<ApplicationStringRef> references;
    Q4T_CHECK(ParseApplicationPayloadReferences(encoded, &references) &&
              references.size() == 2);
    Q4T_CHECK(references[0].snapshot.id == references[1].snapshot.id &&
              references[0].bytes == bytes.size() &&
              references[0].sha256 == Digest(bytes));
    snapshot = references[0].snapshot.id;
    Q4T_CHECK(Ok(Save(objects, id, encoded)));
    payloads.Published(false);
    const auto retention = InspectObjectRetention("chat", encoded);
    Q4T_CHECK(!retention.uncertain &&
              retention.references == std::vector<std::string>{snapshot});
  }
  ObjectStore objects(fixture.options);
  auto stored =
      objects.Execute(Request(id, ObjectOperation::kGet), Access().deadline);
  Q4T_CHECK(Ok(stored));
  Json restored;
  Q4T_CHECK(Ok(RestoreApplicationPayload(
      objects, Access(), *stored.value.Find("data"), {}, &restored)));
  Q4T_CHECK(OutputJson(*restored.Find("request")) ==
            OutputJson(*original.Find("request")));
  Q4T_CHECK(OutputJson(*restored.Find("history")) ==
            OutputJson(*original.Find("history")));
  auto files = Request({}, ObjectOperation::kList);
  files.kind = ObjectKind::kFile;
  Q4T_CHECK(objects.Execute(files, Access().deadline)
                .value.GetArray("data")
                ->array.empty());
  return true;
}

Q4T_TEST(application_payload_reuse_pins_root_across_old_parent_deletion) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto first = NewObjectId(ObjectKind::kChat);
  Json encoded;
  {
    ApplicationPayloads payloads(objects, Access(), Small());
    Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &encoded)));
    Q4T_CHECK(Ok(Save(objects, first, encoded)));
    payloads.Published(false);
  }
  Json restored;
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  Set(&restored, "metadata", Object({{"updated", Boolean(true)}}));
  ApplicationPayloads update(objects, Access(), Small());
  Json reused;
  Q4T_CHECK(Ok(update.Encode(restored, &reused)));
  Q4T_CHECK(Ref(reused).Find("snapshot")->GetString("id") ==
            Ref(encoded).Find("snapshot")->GetString("id"));
  Q4T_CHECK(Ok(objects.Execute(Request(first, ObjectOperation::kErase),
                               Access().deadline)));
  // Erase performs actual private-object collection. Only the new root pin
  // protects this snapshot after the previous durable parent disappears.
  Json during;
  Q4T_CHECK(Ok(
      RestoreApplicationPayload(objects, Access(), reused, Small(), &during)));
  const auto second = NewObjectId(ObjectKind::kChat);
  Q4T_CHECK(Ok(Save(objects, second, reused)));
  update.Published(false);
  Q4T_CHECK(Ok(objects.Execute(Request(second, ObjectOperation::kErase),
                               Access().deadline)));
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(application_payload_changed_strings_get_new_immutable_snapshots) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ApplicationPayloads first(objects, Access(), Small());
  Json encoded, restored;
  Q4T_CHECK(Ok(first.Encode(Data(std::string(4096, 'x')), &encoded)));
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  Set(&FixtureField(restored, "request"), "file_data",
      String(std::string(4096, 'y')));
  ApplicationPayloads second(objects, Access(), Small());
  Json changed;
  Q4T_CHECK(Ok(second.Encode(restored, &changed)));
  Q4T_CHECK(Ref(changed).Find("snapshot")->GetString("id") !=
            Ref(encoded).Find("snapshot")->GetString("id"));
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  Q4T_CHECK(restored.Find("request")->GetString("file_data") ==
            std::string(4096, 'x'));
  return true;
}

Q4T_TEST(application_payload_limits_cancel_and_deadline_precede_storage) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto data = Data(std::string(4096, 'x'));
  auto limits = Small();
  limits.max_data_bytes = 2048;
  ApplicationPayloads bounded(objects, Access(), limits);
  Json encoded;
  Q4T_CHECK(bounded.Encode(data, &encoded).http_status == 413 &&
            encoded.IsNull());
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  ApplicationPayloads stopped(objects, cancelled, Small());
  Q4T_CHECK(stopped.Encode(data, &encoded).http_status == 499);
  auto expired = Access();
  expired.deadline = Clock::now() - 1s;
  ApplicationPayloads late(objects, expired, Small());
  Q4T_CHECK(late.Encode(data, &encoded).http_status == 408);
  auto duplicate = data;
  duplicate.object.push_back(duplicate.object.front());
  ApplicationPayloads invalid(objects, Access(), Small());
  Q4T_CHECK(!invalid.Encode(duplicate, &encoded).ok());
  Q4T_CHECK(objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(application_payload_staging_budget_is_shared_across_batch_members) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto limits = Small();
  limits.max_staged_bytes = 4096;
  {
    ApplicationPayloads payloads(objects, Access(), limits);
    Json first, same, extra;
    Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &first)));
    Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &same)));
    Q4T_CHECK(OutputJson(first) == OutputJson(same));
    Q4T_CHECK(
        payloads.Encode(Data(std::string(4096, 'y')), &extra).http_status ==
        413);
    Q4T_CHECK(extra.IsNull());
  }
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(application_payload_reference_paths_are_exact_and_not_request_paths) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto original = Object(
      {{"messages", Array()},
       {"a/~", Array({Object({{"", String(std::string(4096, 'z'))}})})}});
  ApplicationPayloads payloads(objects, Access(), Small());
  Json encoded, restored;
  Q4T_CHECK(Ok(payloads.Encode(original, &encoded)));
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  Q4T_CHECK(OutputJson(*original.Find("a/~")) ==
            OutputJson(*restored.Find("a/~")));
  auto bad = encoded;
  FixtureField(Ref(bad), "path").array[1] = String("00");
  Q4T_CHECK(
      !RestoreApplicationPayload(objects, Access(), bad, Small(), &restored)
           .ok());
  Q4T_CHECK(restored.IsNull());
  return true;
}

Q4T_TEST(application_payload_all_targets_validate_before_any_read) {
  Fixture fixture;
  Json encoded;
  {
    ObjectStore objects(fixture.options);
    ApplicationPayloads payloads(objects, Access(), Small());
    Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &encoded)));
    payloads.Published(true);  // Explicit unknown-parent protocol receipt.
  }
  Fixture untouched;
  ObjectStore objects(untouched.options);
  auto extra = Ref(encoded);
  Set(&extra, "path", Array({String("absent")}));
  FixtureField(FixtureField(encoded, kField), "strings").array.push_back(extra);
  Json restored;
  Q4T_CHECK(
      RestoreApplicationPayload(objects, Access(), encoded, Small(), &restored)
          .code == "application_payload_invalid");
  Q4T_CHECK(restored.IsNull() && objects.Health().workers_started == 0);
  return true;
}

Q4T_TEST(
    application_payload_scope_corruption_and_expansion_budget_fail_closed) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ApplicationPayloads payloads(objects, Access(), Small());
  Json encoded, restored;
  Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &encoded)));
  Q4T_CHECK(!RestoreApplicationPayload(objects, Access("client:other"), encoded,
                                       Small(), &restored)
                 .ok());
  Q4T_CHECK(restored.IsNull());
  auto bad = encoded;
  Set(&Ref(bad), "sha256", String(Digest("changed")));
  Q4T_CHECK(
      !RestoreApplicationPayload(objects, Access(), bad, Small(), &restored)
           .ok());
  Q4T_CHECK(restored.IsNull());
  auto limits = Small();
  limits.max_data_bytes = 4096;
  Q4T_CHECK(
      RestoreApplicationPayload(objects, Access(), encoded, limits, &restored)
          .http_status == 413);
  Q4T_CHECK(restored.IsNull());
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  Q4T_CHECK(restored.Find("request")->GetString("file_data") ==
            std::string(4096, 'x'));
  return true;
}

Q4T_TEST(application_payload_unknown_publication_keeps_root_and_blocks_replay) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Json encoded;
  {
    ApplicationPayloads payloads(objects, Access(), Small());
    Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &encoded)));
    payloads.Published(true);  // No assertion that a parent actually committed.
    Json repeated;
    Q4T_CHECK(!payloads.Encode(Data(std::string(4096, 'x')), &repeated).ok());
  }
  Q4T_CHECK(Count(objects) > 0);
  Json restored;
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  auto bad = encoded;
  FixtureField(FixtureField(bad, kField), "strings").array.push_back(Ref(bad));
  Q4T_CHECK(InspectObjectRetention("chat", bad).uncertain);
  return true;
}

Q4T_TEST(application_payload_actual_unknown_snapshot_write_is_not_replayed) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  struct Entry {
    Entry() { ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack"); }
    ~Entry() { ObjectWorkerEntryForTesting({}); }
  } entry;
  ObjectStore objects(fixture.options);
  {
    ApplicationPayloads payloads(objects, Access(), Small());
    Json encoded;
    const auto failed = payloads.Encode(Data(std::string(4096, 'x')), &encoded);
    Q4T_CHECK(failed.commit_unknown && encoded.IsNull());
    const auto repeated =
        payloads.Encode(Data(std::string(4096, 'x')), &encoded);
    Q4T_CHECK(repeated.commit_unknown &&
              repeated.value.GetString("id") == failed.value.GetString("id"));
  }
  const auto deadline = Clock::now() + 1s;
  while (objects.Health().pending && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(!objects.Health().pending && Count(objects) == 1);
  return true;
}

Q4T_TEST(application_payload_root_pin_validates_owner_binding_and_identity) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ApplicationPayloads payloads(objects, Access(), Small());
  Json encoded;
  Q4T_CHECK(Ok(payloads.Encode(Data(std::string(4096, 'x')), &encoded)));
  std::vector<ApplicationStringRef> refs;
  Q4T_CHECK(ParseApplicationPayloadReferences(encoded, &refs));
  auto request = Request(refs[0].snapshot.id, ObjectOperation::kGet);
  request.kind = ObjectKind::kInputSnapshot;
  const auto stored = objects.Execute(request, Access().deadline);
  Q4T_CHECK(Ok(stored));
  const auto binding = stored.value.Find("data")->GetString("binding_sha256");
  InputSnapshotStore snapshots(objects);
  InputSnapshotHold pin;
  Q4T_CHECK(
      !snapshots
           .Pin(Access("client:other"), refs[0].snapshot, binding, 60, &pin)
           .ok() &&
      pin.id.empty());
  Q4T_CHECK(
      !snapshots.Pin(Access(), refs[0].snapshot, Digest("wrong"), 60, &pin)
           .ok() &&
      pin.id.empty());
  auto wrong = refs[0].snapshot;
  wrong.sha256 = Digest("wrong root");
  Q4T_CHECK(!snapshots.Pin(Access(), wrong, binding, 60, &pin).ok() &&
            pin.id.empty());
  Q4T_CHECK(Ok(snapshots.Pin(Access(), refs[0].snapshot, binding, 60, &pin)));
  payloads.Published(false);
  Json restored;
  Q4T_CHECK(Ok(RestoreApplicationPayload(objects, Access(), encoded, Small(),
                                         &restored)));
  Q4T_CHECK(Ok(snapshots.Release(Access(), &pin)) && Count(objects) == 0);
  return true;
}
