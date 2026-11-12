#include "q4t/server/binary_store.h"

#include <sys/stat.h>

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root = fs::absolute(output ? output : ".q4t-work/binary-cpu");
    fs::create_directories(root);
    auto path = (root / "binary-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("binary fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:files") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
ObjectRequest Request(std::string id, ObjectKind kind = ObjectKind::kFile,
                      ObjectOperation op = ObjectOperation::kGet) {
  ObjectRequest request;
  request.id = std::move(id);
  request.kind = kind;
  request.operation = op;
  request.scope = "client:files";
  request.ttl_seconds = 0;
  return request;
}
ObjectRequest File(std::string id = "file_binary") {
  auto request = Request(id, ObjectKind::kFile, ObjectOperation::kPut);
  request.data = Object({{"format", String("q4t.file.v1")},
                         {"file", Object({{"id", String(id)}})}});
  return request;
}
ObjectReply Run(ObjectStore& store, const ObjectRequest& request) {
  return store.Execute(request, Clock::now() + 10s);
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
int64_t Count(ObjectStore& store) {
  return Run(store, Request("", ObjectKind::kFile, ObjectOperation::kHealth))
      .value.GetInt("objects", -1);
}
void Expired(int64_t expires) {
  const auto limit = Clock::now() + 2s;
  while (std::time(nullptr) < expires && Clock::now() < limit)
    std::this_thread::sleep_for(5ms);
  if (std::time(nullptr) < expires) throw std::runtime_error("expiry fixture");
}
struct Injection {
  explicit Injection(std::string entry) {
    ObjectWorkerEntryForTesting(std::move(entry));
  }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
void Reaped(ObjectStore& store) {
  const auto limit = Clock::now() + 1s;
  while (store.Health().pending && Clock::now() < limit)
    std::this_thread::sleep_for(1ms);
  if (store.Health().pending) throw std::runtime_error("worker not reaped");
}
}  // namespace

Q4T_TEST(binary_manifest_rejects_inconsistent_or_unbounded_content) {
  BinaryManifest manifest{3, {{"trun_one", Digest("abc"), 3}}}, parsed;
  auto json = BinaryManifestJson(manifest);
  Q4T_CHECK(ParseBinaryManifest(json, &parsed) && parsed.bytes == 3 &&
            parsed.chunks.size() == 1);
  for (int mutation = 0; mutation < 7; ++mutation) {
    auto bad = json;
    auto& chunk = Field(bad, "chunks").array[0];
    if (mutation == 0) Set(&bad, "bytes", Number(4));
    if (mutation == 1) Set(&chunk, "id", String("../private"));
    if (mutation == 2) Set(&chunk, "sha256", String("invalid"));
    if (mutation == 3) Set(&chunk, "bytes", Number(kBinaryChunkBytes + 1));
    if (mutation == 4) Field(bad, "chunks").array.push_back(chunk);
    if (mutation == 5) Set(&bad, "bytes", Number(kBinaryMaxBytes + 1));
    if (mutation == 6) Set(&chunk, "bytes", Number(0));
    Q4T_CHECK(!ParseBinaryManifest(bad, &parsed) && parsed.bytes == 3);
  }
  return true;
}

Q4T_TEST(binary_durable_chunks_exact_bytes_private_files_and_restart) {
  Fixture fixture;
  std::string payload(kBinaryChunkBytes, '\0');
  for (size_t i = 0; i < payload.size(); ++i) payload[i] = i % 256;
  const auto tail = payload.substr(0, 257);
  {
    ObjectStore objects(fixture.options);
    BinaryStore binary(objects);
    BinaryStage stage;
    Q4T_CHECK(
        Ok(binary.Begin(Access(), payload.size() + tail.size(), 60, &stage)));
    Q4T_CHECK(Ok(binary.Append(Access(), &stage, payload)) &&
              Ok(binary.Append(Access(), &stage, tail)));
    auto list = Request("", ObjectKind::kFile, ObjectOperation::kList);
    Q4T_CHECK(Run(objects, list).value.GetArray("data")->array.empty());
    Q4T_CHECK(Ok(binary.Publish(Access(), &stage, File())) && stage.closed);
    Q4T_CHECK(Run(objects, list).value.GetArray("data")->array.size() == 1);
    Q4T_CHECK(!binary.Publish(Access(), &stage, File("file_duplicate")).ok());
    for (const auto& entry :
         fs::directory_iterator(fixture.options.directory)) {
      struct stat info {};
      Q4T_CHECK(!stat(entry.path().c_str(), &info) &&
                (info.st_mode & 0777) == 0600 && info.st_nlink == 1);
    }
  }
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryRead read;
  Q4T_CHECK(
      Ok(binary.Open(Access(), ObjectKind::kFile, "file_binary", 60, &read)) &&
      read.manifest.chunks.size() == 2);
  std::string bytes;
  Q4T_CHECK(Ok(binary.Read(Access(), read, 0, &bytes)) && bytes == payload);
  Q4T_CHECK(Ok(binary.Read(Access(), read, 1, &bytes)) && bytes == tail);
  Q4T_CHECK(Ok(binary.Close(Access(), &read)) && Count(objects) == 3);
  Q4T_CHECK(!binary.Read(Access(), read, 0, &bytes).ok() && bytes.empty());
  return true;
}

Q4T_TEST(binary_empty_file_and_scope_isolation) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), 0, 60, &stage)));
  Q4T_CHECK(!binary.Append(Access("client:other"), &stage, "x").ok());
  Q4T_CHECK(Ok(binary.Publish(Access(), &stage, File())));
  BinaryRead other, own;
  Q4T_CHECK(binary
                .Open(Access("client:other"), ObjectKind::kFile, "file_binary",
                      60, &other)
                .http_status == 404);
  Q4T_CHECK(
      Ok(binary.Open(Access(), ObjectKind::kFile, "file_binary", 60, &own)) &&
      own.manifest.chunks.empty());
  Q4T_CHECK(!binary.Close(Access("client:other"), &own).ok() && !own.closed);
  Q4T_CHECK(Ok(binary.Close(Access(), &own)) && Count(objects) == 1);
  return true;
}

Q4T_TEST(binary_quota_rejection_is_atomic_and_reclaimed_after_delete) {
  Fixture fixture;
  fixture.options.max_client_objects = 2;
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &stage)) &&
            Ok(binary.Append(Access(), &stage, "first")));
  const auto before =
      Run(objects, Request("", ObjectKind::kFile, ObjectOperation::kHealth));
  Q4T_CHECK(before.value.GetInt("bytes") > 5 && Count(objects) == 2);
  const auto failed = binary.Append(Access(), &stage, "second");
  Q4T_CHECK(failed.code == "object_quota_exceeded" && !failed.commit_unknown &&
            stage.manifest.bytes == 5 && stage.revision == 2 &&
            Count(objects) == 2);
  Q4T_CHECK(Ok(binary.Publish(Access(), &stage, File())) &&
            Count(objects) == 2);
  Q4T_CHECK(Ok(Run(objects, Request("file_binary", ObjectKind::kFile,
                                    ObjectOperation::kErase))));
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(binary_publication_parent_conflict_preserves_staging) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &stage)) &&
            Ok(binary.Append(Access(), &stage, "source")));
  auto parent =
      Request("conv_parent", ObjectKind::kConversation, ObjectOperation::kPut);
  parent.data = Object({{"items", Array()}});
  Q4T_CHECK(Ok(Run(objects, parent)));
  parent.expected_revision = 2;
  const std::array parents{parent};
  const auto failed = binary.Publish(Access(), &stage, File(), parents);
  Q4T_CHECK(failed.http_status == 409 && !failed.commit_unknown &&
            !stage.closed);
  Q4T_CHECK(Run(objects, Request("file_binary")).http_status == 404);
  Q4T_CHECK(Ok(Run(objects, Request(stage.id, ObjectKind::kToolRun))));
  Q4T_CHECK(Ok(binary.Publish(Access(), &stage, File())));
  return true;
}

Q4T_TEST(binary_download_pin_survives_file_deletion_then_releases_chunks) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &stage)) &&
            Ok(binary.Append(Access(), &stage, "keep until reader closes")) &&
            Ok(binary.Publish(Access(), &stage, File())));
  BinaryRead read;
  Q4T_CHECK(
      Ok(binary.Open(Access(), ObjectKind::kFile, "file_binary", 60, &read)));
  Q4T_CHECK(Ok(Run(objects, Request("file_binary", ObjectKind::kFile,
                                    ObjectOperation::kErase))));
  Q4T_CHECK(Count(objects) == 2);
  std::string bytes;
  Q4T_CHECK(Ok(binary.Read(Access(), read, 0, &bytes)) &&
            bytes == "keep until reader closes");
  Q4T_CHECK(Ok(binary.Close(Access(), &read)) && Count(objects) == 0);
  return true;
}

Q4T_TEST(binary_abandoned_upload_and_reader_expire_after_restart) {
  Fixture fixture;
  int64_t expires = 0;
  {
    ObjectStore objects(fixture.options);
    BinaryStore binary(objects);
    BinaryStage stage, file;
    Q4T_CHECK(Ok(binary.Begin(Access(), 100, 1, &stage)) &&
              Ok(binary.Append(Access(), &stage, "unfinished")));
    Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &file)) &&
              Ok(binary.Append(Access(), &file, "read later")) &&
              Ok(binary.Publish(Access(), &file, File())));
    BinaryRead read;
    Q4T_CHECK(
        Ok(binary.Open(Access(), ObjectKind::kFile, "file_binary", 1, &read)));
    Q4T_CHECK(Ok(Run(objects, Request("file_binary", ObjectKind::kFile,
                                      ObjectOperation::kErase))));
    expires = std::max(stage.expires, read.expires);
  }
  Expired(expires);
  ObjectStore objects(fixture.options);
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(binary_abort_capacity_cancellation_and_deadline_do_not_publish) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(binary.Begin(cancelled, 100, 60, &stage).http_status == 499 &&
            stage.id.empty());
  auto expired = Access();
  expired.deadline = Clock::now();
  Q4T_CHECK(binary.Begin(expired, 100, 60, &stage).http_status == 504);
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(Ok(binary.Begin(Access(), kBinaryChunkBytes + 1, 60, &stage)));
  Q4T_CHECK(
      binary.Append(Access(), &stage, std::string(kBinaryChunkBytes + 1, 'x'))
              .http_status == 413 &&
      stage.manifest.bytes == 0);
  Q4T_CHECK(Ok(binary.Append(Access(), &stage, "do not publish")) &&
            Ok(binary.Abort(Access(), &stage)) && stage.closed);
  Q4T_CHECK(Count(objects) == 0 &&
            !binary.Publish(Access(), &stage, File()).ok());
  return true;
}

Q4T_TEST(binary_inner_checksum_and_canonical_encoding_precede_output) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &stage)) &&
            Ok(binary.Append(Access(), &stage, "f")) &&
            Ok(binary.Publish(Access(), &stage, File())));
  BinaryRead read;
  Q4T_CHECK(
      Ok(binary.Open(Access(), ObjectKind::kFile, "file_binary", 60, &read)));
  auto chunk = Request(read.manifest.chunks[0].id, ObjectKind::kToolRun);
  for (const auto* encoded : {"Zh==", "Zw=="}) {
    auto stored = Run(objects, chunk);
    Q4T_CHECK(Ok(stored));
    auto change = chunk;
    change.operation = ObjectOperation::kPut;
    change.expected_revision = stored.value.GetInt("revision");
    change.data = *stored.value.Find("data");
    Set(&change.data, "base64", String(encoded));
    Q4T_CHECK(Ok(Run(objects, change)));
    std::string bytes = "old buffer must clear";
    Q4T_CHECK(binary.Read(Access(), read, 0, &bytes).code ==
                  "binary_state_invalid" &&
              bytes.empty());
  }
  return true;
}

Q4T_TEST(binary_unknown_append_recovers_original_decision_without_replay) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  Injection inject("--q4t-object-worker-apply-pause");
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &stage)));
  auto failed = binary.Append(Access(), &stage, "durable original");
  Q4T_CHECK(!failed.ok() && failed.commit_unknown && stage.commit_unknown &&
            failed.value.GetString("id") == stage.id);
  Q4T_CHECK(binary.Append(Access(), &stage, "must not replay").commit_unknown);
  Q4T_CHECK(binary.Abort(Access(), &stage).commit_unknown);
  Reaped(objects);
  const auto recovered = Run(objects, Request(stage.id, ObjectKind::kToolRun));
  Q4T_CHECK(Ok(recovered) && recovered.value.GetInt("revision") == 2);
  BinaryManifest manifest;
  Q4T_CHECK(ParseBinaryManifest(*recovered.value.Find("data")->Find("blob"),
                                &manifest) &&
            manifest.bytes == 16 && manifest.chunks.size() == 1);
  Q4T_CHECK(Count(objects) == 2 && objects.Health().workers_started == 2);
  return true;
}

Q4T_TEST(binary_unknown_publication_recovers_file_and_removes_stage) {
  Fixture fixture;
  BinaryStage stage;
  {
    ObjectStore objects(fixture.options);
    BinaryStore binary(objects);
    Q4T_CHECK(Ok(binary.Begin(Access(), 100, 60, &stage)) &&
              Ok(binary.Append(Access(), &stage, "durable file")));
  }
  fixture.options.io_timeout_ms = 250;
  {
    Injection inject("--q4t-object-worker-apply-pause");
    ObjectStore objects(fixture.options);
    BinaryStore binary(objects);
    const auto failed = binary.Publish(Access(), &stage, File());
    Q4T_CHECK(!failed.ok() && failed.commit_unknown && stage.commit_unknown &&
              failed.value.GetString("id") == "file_binary" &&
              failed.value.GetString("stage_id") == stage.id);
    Q4T_CHECK(binary.Publish(Access(), &stage, File()).commit_unknown);
    Reaped(objects);
  }
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  Q4T_CHECK(Ok(Run(objects, Request("file_binary"))));
  Q4T_CHECK(Run(objects, Request(stage.id, ObjectKind::kToolRun)).http_status ==
            404);
  BinaryRead read;
  Q4T_CHECK(
      Ok(binary.Open(Access(), ObjectKind::kFile, "file_binary", 60, &read)));
  std::string bytes;
  Q4T_CHECK(Ok(binary.Read(Access(), read, 0, &bytes)) &&
            bytes == "durable file");
  return true;
}

Q4T_TEST(binary_unknown_stage_creation_keeps_identity_and_blocks_replay) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  Injection inject("--q4t-object-worker-lost-ack");
  ObjectStore objects(fixture.options);
  BinaryStore binary(objects);
  BinaryStage stage;
  const auto failed = binary.Begin(Access(), 100, 60, &stage);
  Q4T_CHECK(failed.commit_unknown && stage.commit_unknown &&
            !stage.id.empty() && failed.value.GetString("id") == stage.id);
  Q4T_CHECK(binary.Append(Access(), &stage, "x").commit_unknown &&
            binary.Abort(Access(), &stage).commit_unknown);
  Reaped(objects);
  Q4T_CHECK(Ok(Run(objects, Request(stage.id, ObjectKind::kToolRun))) &&
            Count(objects) == 1);
  return true;
}
