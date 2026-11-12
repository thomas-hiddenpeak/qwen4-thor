#include "q4t/server/vector_stores.h"

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <numeric>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!out) throw std::runtime_error("Missing vector store test output");
    auto path = (std::filesystem::path(out) / "vector-stores-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Vector store fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:vectors") {
  return {std::move(scope), Clock::now() + 15s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
std::string Upload(ObjectStore& objects, std::string scope = "client:vectors") {
  auto access = Access(scope);
  FileUpload upload(objects, access, "multipart/form-data; boundary=f");
  const auto wire =
      "--f\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nuser_data\r\n"
      "--f\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"MODEL.md\"\r\n"
      "Content-Type: text/markdown\r\n\r\n" +
      std::string(251, 'a') + "\r\n--f--\r\n";
  if (!Ok(upload.Feed(wire, access.deadline)))
    throw std::runtime_error("Upload feed");
  auto result = upload.Finish();
  if (!Ok(result)) throw std::runtime_error("Upload finish");
  return result.value.GetString("id");
}
Json AttachInput(const std::string& file_id) {
  return Object(
      {{"file_id", String(file_id)},
       {"chunking_strategy",
        Object({{"type", String("static")},
                {"static", Object({{"max_chunk_size_tokens", Number(100)},
                                   {"chunk_overlap_tokens", Number(50)}})}})}});
}
ObjectReply Raw(ObjectStore& objects, ObjectKind kind, std::string id,
                std::string scope = "client:vectors") {
  ObjectRequest request;
  request.kind = kind;
  request.id = std::move(id);
  request.scope = std::move(scope);
  return objects.Execute(request, Clock::now() + 5s);
}
ObjectReply Health(ObjectStore& objects) {
  ObjectRequest request;
  request.operation = ObjectOperation::kHealth;
  request.scope = "client:vectors";
  return objects.Execute(request, Clock::now() + 5s);
}
std::string Create(VectorStores& stores) {
  auto result = stores.Create(Access(), Object());
  if (!Ok(result)) throw std::runtime_error("Create store");
  return result.value.GetString("id");
}
VectorIndex Index(const std::string& file_id) {
  const std::string text(251, 'a');
  VectorIndexIdentity identity{file_id,
                               "MODEL.md",
                               Digest(text),
                               Digest(text),
                               VectorEmbeddingIdentity(32),
                               32,
                               {100, 50}};
  std::vector<uint32_t> tokens(text.size(), 64);
  std::vector<size_t> boundaries(text.size() + 1);
  std::iota(boundaries.begin(), boundaries.end(), 0);
  VectorIndex index;
  uint64_t used;
  auto result = BuildVectorIndex(
      identity, text, tokens, boundaries,
      [](auto input, auto* vector, auto* count) {
        vector->assign(32, 0);
        vector->front() = 1;
        *count = input.size();
        return ObjectReply{};
      },
      {}, &index, &used);
  if (!Ok(result)) throw std::runtime_error("Index fixture");
  return index;
}
Json Scan(ObjectStore& objects) {
  auto rows = Array();
  std::string cursor;
  for (int page = 0; page < 100; ++page) {
    auto result = objects.ScanVectorFiles(cursor, Clock::now() + 5s);
    if (!Ok(result)) throw std::runtime_error("Recovery scan");
    const auto* values = result.value.GetArray("data");
    rows.array.insert(rows.array.end(), values->array.begin(),
                      values->array.end());
    if (!result.value.GetBool("has_more")) return rows;
    cursor = result.value.GetString("cursor");
  }
  throw std::runtime_error("Recovery scan did not terminate");
}
struct Injection {
  explicit Injection(std::string entry) { ObjectWorkerEntryForTesting(entry); }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
}  // namespace

Q4T_TEST(vector_stores_create_contract_scope_and_atomic_source_validation) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto file = Upload(objects), other = Upload(objects, "client:other");
  auto body = Object(
      {{"name", String("本项目资料")},
       {"metadata", Object({{"项目", String("Thor")}})},
       {"expires_after",
        Object({{"anchor", String("last_active_at")}, {"days", Number(365)}})},
       {"file_ids", Array({String(file), String(other)})}});
  Q4T_CHECK(stores.Create(Access(), body).http_status == 404);
  Q4T_CHECK(stores.List(Access(), {}).value.GetArray("data")->array.empty());
  Set(&body, "file_ids", Array({String(file)}));
  const auto created = stores.Create(Access(), body);
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  Q4T_CHECK(id.starts_with("vs_") &&
            created.value.GetString("status") == "in_progress" &&
            created.value.Find("file_counts")->GetInt("total") == 1 &&
            created.value.GetInt("expires_at") -
                    created.value.GetInt("last_active_at") ==
                365 * 86400);
  Q4T_CHECK(stores.Retrieve(Access("client:other"), id).http_status == 404);
  Q4T_CHECK(stores.RetrieveFile(Access("client:other"), id, file).http_status ==
            404);
  Q4T_CHECK(stores.Create(Access(), Object({{"unknown", Boolean(true)}}))
                .http_status == 400);
  Q4T_CHECK(stores
                .Update(Access(), id,
                        Object({{"expires_after", Json()}, {"name", Json()}}))
                .ok());
  Q4T_CHECK(stores.Retrieve(Access(), id).value.Find("expires_at")->IsNull());
  return true;
}

Q4T_TEST(vector_stores_quota_rejection_never_publishes_partial_members) {
  Fixture fixture;
  std::string file;
  int64_t count;
  {
    ObjectStore objects(fixture.options);
    file = Upload(objects);
    count = Health(objects).value.GetInt("objects");
  }
  fixture.options.max_client_objects = count + 1;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  auto reply =
      stores.Create(Access(), Object({{"file_ids", Array({String(file)})}}));
  Q4T_CHECK(!reply.ok() && !reply.commit_unknown);
  Q4T_CHECK(stores.List(Access(), {}).value.GetArray("data")->array.empty());
  Q4T_CHECK(Scan(objects).array.empty() &&
            Files(objects).Retrieve(Access(), file).ok());
  Q4T_CHECK(Health(objects).value.GetInt("objects") == count);
  return true;
}

Q4T_TEST(
    vector_stores_concurrent_attach_preserves_counts_and_independent_jobs) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores), a = Upload(objects), b = Upload(objects);
  ObjectReply first, second;
  std::thread one([&] { first = stores.Attach(Access(), id, AttachInput(a)); });
  std::thread two(
      [&] { second = stores.Attach(Access(), id, AttachInput(b)); });
  one.join();
  two.join();
  Q4T_CHECK(Ok(first) && Ok(second));
  const auto read = stores.Retrieve(Access(), id);
  Q4T_CHECK(read.value.Find("file_counts")->GetInt("in_progress") == 2 &&
            Scan(objects).array.size() == 2);
  Q4T_CHECK(stores.Attach(Access(), id, AttachInput(a)).code ==
            "vector_store_file_exists");
  return true;
}

Q4T_TEST(vector_stores_file_pagination_attributes_and_terminal_counts) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores), a = Upload(objects), b = Upload(objects);
  Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(a))) &&
            Ok(stores.Attach(Access(), id, AttachInput(b))));
  Json attributes;
  Q4T_CHECK(
      Parse(R"({"attributes":{"revision":9007199254740993,"部门":"研发"}})",
            &attributes));
  Q4T_CHECK(Ok(stores.UpdateFile(Access(), id, a, attributes)));
  Q4T_CHECK(OutputJson(stores.RetrieveFile(Access(), id, a).value)
                .find("9007199254740993") != std::string::npos);
  const auto first = stores.ListFiles(
      Access(), id, Object({{"limit", Number(1)}, {"order", String("asc")}}));
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more"));
  const auto last = first.value.GetString("last_id");
  const auto second = stores.ListFiles(
      Access(), id,
      Object({{"after", String(last)}, {"order", String("asc")}}));
  Q4T_CHECK(Ok(second) && second.value.GetArray("data")->array.size() == 1 &&
            second.value.GetString("first_id") != last);
  VectorIngestionClaim claim;
  Q4T_CHECK(Ok(stores.Claim(Access(), id, a, "executor", 60, &claim)));
  Q4T_CHECK(
      Ok(stores.Fail(Access(), claim, "invalid_file", "Fixture rejection")));
  auto failed =
      stores.ListFiles(Access(), id, Object({{"filter", String("failed")}}));
  Q4T_CHECK(Ok(failed) && failed.value.GetArray("data")->array.size() == 1 &&
            failed.value.GetString("first_id") == a);
  const auto root = stores.Retrieve(Access(), id);
  Q4T_CHECK(root.value.Find("file_counts")->GetInt("failed") == 1 &&
            root.value.Find("file_counts")->GetInt("in_progress") == 1);
  return true;
}

Q4T_TEST(
    vector_stores_verified_index_is_retained_across_restart_and_collected_after_delete) {
  Fixture fixture;
  std::string id, file;
  InputSnapshotRef reference;
  VectorIndexIdentity identity;
  {
    ObjectStore objects(fixture.options);
    VectorStores stores(objects);
    file = Upload(objects);
    id = Create(stores);
    Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
    VectorIngestionClaim claim;
    Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "executor", 60, &claim)));
    auto index = Index(file);
    identity = index.identity;
    InputSnapshotStore snapshots(objects);
    InputSnapshotHold hold;
    Q4T_CHECK(
        Ok(SaveVectorIndex(snapshots, Access(), index, 60, &reference, &hold)));
    Q4T_CHECK(Ok(stores.Finish(Access(), claim, identity, reference)));
    Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
    auto root = stores.Retrieve(Access(), id);
    Q4T_CHECK(root.value.GetString("status") == "completed" &&
              root.value.GetInt("usage_bytes") > 251 &&
              Scan(objects).array.empty());
  }
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  InputSnapshotStore snapshots(objects);
  VectorIndex index;
  Q4T_CHECK(
      Ok(ReadVectorIndex(snapshots, Access(), reference, identity, &index)));
  Q4T_CHECK(Ok(stores.UpdateFile(
      Access(), id, file,
      Object({{"attributes", Object({{"tag", String("new")}})}}))));
  Q4T_CHECK(Ok(stores.LoadFile(Access(), id, file)));
  Q4T_CHECK(Ok(stores.Erase(Access(), id)));
  for (int i = 0; i < 4; ++i) Q4T_CHECK(Ok(Health(objects)));
  Q4T_CHECK(
      Raw(objects, ObjectKind::kInputSnapshot, reference.id).http_status ==
      404);
  Q4T_CHECK(Files(objects).Retrieve(Access(), file).ok());
  return true;
}

Q4T_TEST(vector_stores_detach_and_reattach_rejects_stale_executor) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores), file = Upload(objects);
  Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
  VectorIngestionClaim old, fresh;
  Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "old", 60, &old)));
  Q4T_CHECK(Ok(stores.Detach(Access(), id, file)));
  Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
  Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "new", 60, &fresh)));
  Q4T_CHECK(old.member_id != fresh.member_id &&
            stores.Cancel(Access(), old).http_status == 409 &&
            stores.RequeueAfterExit(Access(), old).http_status == 409);
  Q4T_CHECK(Ok(stores.LoadClaim(Access(), fresh)));
  return true;
}

Q4T_TEST(vector_stores_deleted_source_cannot_be_loaded_or_published) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores), file = Upload(objects);
  Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
  VectorIngestionClaim claim;
  Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "executor", 60, &claim)));
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), file)));
  Q4T_CHECK(stores.LoadClaim(Access(), claim).http_status == 404 &&
            stores.LoadFile(Access(), id, file).http_status == 404);
  Q4T_CHECK(
      stores.Finish(Access(), claim, Index(file).identity, {}).http_status ==
      404);
  Q4T_CHECK(Ok(stores.Fail(Access(), claim, "invalid_file", "Source deleted")));
  return true;
}

Q4T_TEST(
    vector_stores_recovery_requires_explicit_owner_exit_and_bounds_retries) {
  Fixture fixture;
  std::string id, file;
  VectorIngestionClaim claim;
  {
    ObjectStore objects(fixture.options);
    VectorStores stores(objects);
    id = Create(stores);
    file = Upload(objects);
    Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
    Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "worker:one", 1, &claim)));
  }
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto end = Clock::now() + 2s;
  while (std::time(nullptr) < claim.deadline_at && Clock::now() < end)
    std::this_thread::sleep_for(5ms);
  VectorIngestionClaim contender;
  Q4T_CHECK(stores.Claim(Access(), id, file, "worker:two", 60, &contender)
                .http_status == 409);
  auto rows = Scan(objects);
  Q4T_CHECK(rows.array.size() == 1 &&
            rows.array.front().GetString("owner") == "worker:one");
  Q4T_CHECK(OutputJson(rows).find("MODEL.md") == std::string::npos &&
            OutputJson(rows).find("input_snapshots") == std::string::npos);
  for (int attempt = 1; attempt <= 3; ++attempt) {
    Q4T_CHECK(Ok(stores.RequeueAfterExit(Access(), claim)));
    if (attempt < 3)
      Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "recovered", 60, &claim)));
  }
  Q4T_CHECK(stores.RetrieveFile(Access(), id, file).value.GetString("status") ==
            "failed");
  Q4T_CHECK(Scan(objects).array.empty());
  return true;
}

Q4T_TEST(
    vector_stores_lost_create_and_claim_receipts_recover_original_identity) {
  Fixture first;
  first.options.io_timeout_ms = 250;
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(first.options);
    VectorStores stores(objects);
    const auto reply = stores.Create(Access(), Object());
    Q4T_CHECK(reply.commit_unknown &&
              reply.value.GetString("id").starts_with("vs_"));
    Q4T_CHECK(Ok(stores.Retrieve(Access(), reply.value.GetString("id"))));
    Q4T_CHECK(stores.List(Access(), {}).value.GetArray("data")->array.size() ==
              1);
  }
  Fixture second;
  second.options.io_timeout_ms = 250;
  std::string id, file;
  {
    ObjectStore objects(second.options);
    VectorStores stores(objects);
    id = Create(stores);
    file = Upload(objects);
    Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(second.options);
  VectorStores stores(objects);
  VectorIngestionClaim claim;
  const auto reply = stores.Claim(Access(), id, file, "executor", 60, &claim);
  Q4T_CHECK(reply.commit_unknown && !claim.token.empty());
  Q4T_CHECK(Ok(stores.LoadClaim(Access(), claim)));
  Q4T_CHECK(Scan(objects).array.size() == 1);
  return true;
}

Q4T_TEST(vector_stores_expiry_releases_dependencies_without_resurrection) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores);
  auto original = Raw(objects, ObjectKind::kVectorStore, id);
  Q4T_CHECK(Ok(original) && Ok(stores.Erase(Access(), id)));
  ObjectRequest write;
  write.scope = Access().scope;
  write.kind = ObjectKind::kVectorStore;
  write.operation = ObjectOperation::kPut;
  write.id = id;
  write.created_at = std::time(nullptr) - 2 * 86400;
  write.ttl_seconds = 0;
  write.data = *original.value.Find("data");
  auto& store = Field(write.data, "vector_store");
  Set(&store, "created_at", Number(write.created_at));
  Set(&store, "last_active_at", Number(write.created_at));
  Set(&store, "expires_after",
      Object({{"anchor", String("last_active_at")}, {"days", Number(1)}}));
  Set(&store, "expires_at", Number(write.created_at + 86400));
  Q4T_CHECK(Ok(objects.Execute(write, Clock::now() + 5s)));
  Q4T_CHECK(stores.Retrieve(Access(), id).value.GetString("status") ==
            "expired");
  Q4T_CHECK(stores.Touch(Access(), id).code == "vector_store_expired");
  Q4T_CHECK(
      stores.Update(Access(), id, Object({{"expires_after", Json()}})).code ==
      "vector_store_expired");
  Q4T_CHECK(
      Ok(stores.Update(Access(), id, Object({{"name", String("归档")}}))));
  auto data = write.data;
  Set(&data, "members",
      Array({Object({{"id", String("vector_store_file_private")}})}));
  const auto root_info = InspectObjectRetention("vector_store", data);
  const auto child_info = InspectObjectRetention(
      "vector_store_file",
      Object({{"format", String("q4t.vector_store_file.v1")},
              {"file", Object({{"id", String("file_source")},
                               {"status", String("in_progress")}})}}));
  Q4T_CHECK(!root_info.uncertain && !child_info.uncertain &&
            child_info.collect_when_unreferenced);
  const std::string owner(64, 'a'), digest(64, 'b');
  const ObjectRetentionNode nodes[] = {
      {owner, "vector_store", id, digest, 1, 0, 100, &root_info},
      {owner, "vector_store_file", "vector_store_file_private", digest, 1, 0,
       100, &child_info}};
  ObjectRetentionPlan plan;
  Q4T_CHECK(PlanObjectRetention(nodes, std::time(nullptr), 0, 128, &plan) &&
            plan.eligible == 1 && plan.selected == std::vector<size_t>{1});
  return true;
}

Q4T_TEST(vector_stores_foreign_or_wrong_index_never_marks_file_ready) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores), file = Upload(objects);
  Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
  VectorIngestionClaim claim;
  Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "executor", 60, &claim)));
  auto index = Index(file);
  InputSnapshotStore snapshots(objects);
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(SaveVectorIndex(snapshots, Access("other-client"), index, 60,
                               &reference, &hold)));
  Q4T_CHECK(
      stores.Finish(Access(), claim, index.identity, reference).http_status ==
      404);
  Q4T_CHECK(Ok(snapshots.Release(Access("other-client"), &hold)));
  reference = {};
  hold = {};
  index.identity.filename = "wrong.md";
  Q4T_CHECK(
      Ok(SaveVectorIndex(snapshots, Access(), index, 60, &reference, &hold)));
  Q4T_CHECK(stores.Finish(Access(), claim, index.identity, reference).code ==
            "vector_store_state_invalid");
  Q4T_CHECK(stores.RetrieveFile(Access(), id, file).value.GetString("status") ==
            "in_progress");
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  VectorIndexIdentity parsed;
  Q4T_CHECK(ParseVectorIndexIdentity(VectorIndexIdentityJson(index.identity),
                                     &parsed) &&
            VectorIndexBinding(parsed) == VectorIndexBinding(index.identity));
  return true;
}

Q4T_TEST(vector_stores_cancellation_and_corruption_remain_local) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(stores.Create(cancelled, Object()).http_status == 499);
  Q4T_CHECK(stores.List(Access(), {}).value.GetArray("data")->array.empty());
  const auto id = Create(stores), file = Upload(objects);
  Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
  VectorIngestionClaim claim;
  Q4T_CHECK(Ok(stores.Claim(Access(), id, file, "executor", 60, &claim)));
  Q4T_CHECK(Ok(stores.Cancel(Access(), claim)));
  Q4T_CHECK(stores.Retrieve(Access(), id)
                .value.Find("file_counts")
                ->GetInt("cancelled") == 1);
  auto raw = Raw(objects, ObjectKind::kVectorStore, id);
  ObjectRequest write;
  write.operation = ObjectOperation::kPut;
  write.kind = ObjectKind::kVectorStore;
  write.scope = Access().scope;
  write.id = id;
  write.ttl_seconds = 0;
  write.expected_revision = raw.value.GetInt("revision");
  write.data = *raw.value.Find("data");
  Set(&Field(write.data, "vector_store"), "usage_bytes", Number(123));
  Q4T_CHECK(Ok(objects.Execute(write, Clock::now() + 5s)));
  Q4T_CHECK(stores.Retrieve(Access(), id).code == "vector_store_state_invalid");
  Q4T_CHECK(Ok(Health(objects)) && Ok(Files(objects).Retrieve(Access(), file)));
  return true;
}

Q4T_TEST(vector_stores_expired_queued_job_finishes_without_model_execution) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  VectorStores stores(objects);
  const auto id = Create(stores);
  for (int mode = 0; mode < 2; ++mode) {
    const auto file = Upload(objects);
    Q4T_CHECK(Ok(stores.Attach(Access(), id, AttachInput(file))));
    auto raw = stores.LoadFile(Access(), id, file);
    Q4T_CHECK(Ok(raw));
    ObjectRequest write;
    write.operation = ObjectOperation::kPut;
    write.kind = ObjectKind::kVectorStoreFile;
    write.scope = Access().scope;
    write.id = raw.value.GetString("id");
    write.expected_revision = raw.value.GetInt("revision");
    write.ttl_seconds = 0;
    write.data = *raw.value.Find("data");
    auto& job = Field(write.data, "job");
    const auto deadline = raw.value.GetInt("created_at") + 1;
    if (mode == 0)
      Set(&job, "execution_deadline_at", Number(deadline));
    else
      Set(&job, "attempts", Number(3));
    Q4T_CHECK(Ok(objects.Execute(write, Clock::now() + 5s)));
    if (mode == 0) {
      const auto end = Clock::now() + 2s;
      while (std::time(nullptr) < deadline && Clock::now() < end)
        std::this_thread::sleep_for(5ms);
    }
    VectorIngestionClaim claim;
    Q4T_CHECK(stores.Claim(Access(), id, file, "executor", 60, &claim).code ==
                  "vector_ingestion_deadline" &&
              claim.token.empty());
    Q4T_CHECK(
        stores.RetrieveFile(Access(), id, file).value.GetString("status") ==
        "failed");
  }
  Q4T_CHECK(Scan(objects).array.empty());
  Q4T_CHECK(stores.Retrieve(Access(), id)
                .value.Find("file_counts")
                ->GetInt("failed") == 2);
  return true;
}
