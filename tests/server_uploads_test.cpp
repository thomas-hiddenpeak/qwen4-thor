#include "q4t/server/uploads.h"

#include <barrier>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/native_crypto.h"
#include "q4t/server/object_retention.h"
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
    auto root = fs::absolute(output ? output : ".q4t-work/uploads-cpu");
    fs::create_directories(root);
    auto path = (root / "uploads-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("uploads fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
    options.max_queue = 32;
  }
};
BinaryAccess Access(std::string scope = "client:uploads") {
  return {std::move(scope), Clock::now() + 30s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%d %s: %s\n", reply.http_status, reply.code.c_str(),
                 reply.message.c_str());
  return reply.ok();
}
ObjectRequest Request(std::string id,
                      ObjectOperation operation = ObjectOperation::kGet) {
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.id = std::move(id);
  request.scope = Access().scope;
  request.operation = operation;
  request.ttl_seconds = 0;
  return request;
}
Json Body(int64_t bytes = 3, std::string purpose = "user_data") {
  return Object({{"bytes", Number(bytes)},
                 {"filename", String("资料.txt")},
                 {"purpose", String(purpose)},
                 {"mime_type", String("text/plain")}});
}
Json CompleteBody(std::initializer_list<std::string> parts,
                  std::string md5 = {}) {
  Json ids = Array();
  for (const auto& part : parts) ids.array.push_back(String(part));
  auto body = Object({{"part_ids", std::move(ids)}});
  if (!md5.empty()) Set(&body, "md5", String(md5));
  return body;
}
std::string Wire(std::string_view bytes) {
  return "--p\r\nContent-Disposition: form-data; name=\"data\"; "
         "filename=\"part.bin\"\r\nContent-Type: "
         "application/octet-stream\r\n\r\n" +
         std::string(bytes) + "\r\n--p--\r\n";
}
ObjectReply Part(ObjectStore& objects, const std::string& upload_id,
                 std::string_view bytes, std::string scope = "client:uploads") {
  auto access = Access(scope);
  UploadPart part(objects, access, upload_id,
                  "multipart/form-data; boundary=p");
  auto result = part.Feed(Wire(bytes), access.deadline);
  if (result.ok()) result = part.Finish();
  if (!result.ok()) part.Abort(Access(scope));
  return result;
}
std::string Content(ObjectStore& objects, const std::string& file_id) {
  BinaryRead read;
  Files files(objects);
  if (!Ok(files.Open(Access(), file_id, &read)))
    throw std::runtime_error("open completed file");
  BinaryStore binary(objects);
  std::string output;
  for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
    std::string bytes;
    if (!Ok(binary.Read(Access(), read, i, &bytes)))
      throw std::runtime_error("read completed file");
    output += bytes;
  }
  if (!Ok(binary.Close(Access(), &read)))
    throw std::runtime_error("close completed file");
  return output;
}
std::string Create(Uploads& uploads, const Json& body) {
  auto result = uploads.Create(Access(), body);
  if (!Ok(result)) throw std::runtime_error("create upload");
  return result.value.GetString("id");
}
std::string Add(ObjectStore& objects, const std::string& id,
                std::string_view bytes) {
  auto result = Part(objects, id, bytes);
  if (!Ok(result)) throw std::runtime_error("add part");
  return result.value.GetString("id");
}
int64_t Count(ObjectStore& objects) {
  return objects
      .Execute(Request("", ObjectOperation::kHealth), Access().deadline)
      .value.GetInt("objects", -1);
}
struct Injection {
  explicit Injection(std::string entry) {
    ObjectWorkerEntryForTesting(std::move(entry));
  }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
void Reaped(ObjectStore& objects) {
  const auto deadline = Clock::now() + 2s;
  while (objects.Health().workers_reaped == 0 && Clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
}
ObjectRequest Copy(const Json& stored) {
  auto request = Request(stored.GetString("id"), ObjectOperation::kPut);
  request.expected_revision = stored.GetInt("revision");
  request.created_at = stored.GetInt("created_at");
  request.expires_at = stored.GetInt("expires_at");
  request.data = *stored.Find("data");
  return request;
}
}  // namespace

Q4T_TEST(uploads_fields_capacity_mime_defaults_and_scope) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  for (const auto* purpose :
       {"assistants", "batch", "fine-tune", "vision", "user_data", "evals"}) {
    auto body = Body(0, purpose);
    if (std::string_view(purpose) == "batch" ||
        std::string_view(purpose) == "fine-tune")
      Set(&body, "filename", String("input.jsonl"));
    if (std::string_view(purpose) == "vision")
      Set(&body, "mime_type", String("image/png"));
    const auto id = Create(uploads, body);
    auto result = uploads.Retrieve(Access(), id);
    Q4T_CHECK(Ok(result) && id.starts_with("upload_") &&
              result.value.GetInt("expires_at") -
                      result.value.GetInt("created_at") ==
                  3600 &&
              result.value.GetString("status") == "pending" &&
              result.value.GetString("purpose") == purpose);
    Q4T_CHECK(uploads.Retrieve(Access("client:other"), id).http_status == 404);
    const auto complete = uploads.Complete(Access(), id, CompleteBody({}));
    Q4T_CHECK(Ok(complete));
    const auto& file = *complete.value.Find("file");
    Q4T_CHECK(file.GetInt("expires_at") ==
              (std::string_view(purpose) == "batch"
                   ? file.GetInt("created_at") + 2592000
                   : 0));
  }
  Q4T_CHECK(Ok(uploads.Create(Access(), Body(kBinaryMaxBytes))));
  Q4T_CHECK(uploads.Create(Access(), Body(kBinaryMaxBytes + 1)).http_status ==
            413);
  for (int mutation = 0; mutation < 14; ++mutation) {
    auto body = Body();
    if (mutation == 0) Set(&body, "bytes", Number(-1));
    if (mutation == 1) Set(&body, "bytes", Boolean(true));
    if (mutation == 2) Set(&body, "bytes", Json());
    if (mutation == 3) Set(&body, "bytes", String("3"));
    if (mutation == 4) Field(body, "bytes").number = 1.5;
    if (mutation == 5) Set(&body, "filename", String(""));
    if (mutation == 6) Set(&body, "purpose", String("output_only"));
    if (mutation == 7) Set(&body, "mime_type", String("bad\r\nvalue"));
    if (mutation == 8) Set(&body, "purpose", String("vision"));
    if (mutation == 9) Set(&body, "expires_after", Json());
    if (mutation == 10) Set(&body, "extra", Boolean(true));
    if (mutation == 11) body.object.push_back({"bytes", Number(3)});
    if (mutation == 12) Set(&body, "purpose", String("batch"));
    if (mutation == 13) Set(&body, "filename", String(std::string(1025, 'x')));
    Q4T_CHECK(!uploads.Create(Access(), body).ok());
  }
  return true;
}

Q4T_TEST(uploads_ordered_parts_md5_atomic_file_and_receipt_reconciliation) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  auto body = Body(6);
  Set(&body, "expires_after",
      Object({{"anchor", String("created_at")}, {"seconds", Number(3600)}}));
  const auto id = Create(uploads, body);
  const auto second = Add(objects, id, "def"), first = Add(objects, id, "abc");
  Q4T_CHECK(Files(objects)
                .List(Access(), Object())
                .value.GetArray("data")
                ->array.empty());
  auto request =
      CompleteBody({first, second}, "e80b5017098950fc58aad83c8c14978e");
  const auto complete = uploads.Complete(Access(), id, request);
  Q4T_CHECK(Ok(complete) && complete.value.GetString("status") == "completed");
  const auto& file = *complete.value.Find("file");
  const auto file_id = file.GetString("id");
  Q4T_CHECK(Content(objects, file_id) == "abcdef" &&
            file.GetInt("expires_at") - file.GetInt("created_at") == 3600);
  Q4T_CHECK(OutputJson(uploads.Complete(Access(), id, request).value) ==
            OutputJson(complete.value));
  Q4T_CHECK(uploads.Complete(Access(), id, CompleteBody({second, first}))
                .http_status == 409);
  Q4T_CHECK(uploads.Cancel(Access(), id).http_status == 409);
  Q4T_CHECK(Part(objects, id, "late").http_status == 409);
  Q4T_CHECK(Files(objects)
                .List(Access(), Object())
                .value.GetArray("data")
                ->array.size() == 1);
  Q4T_CHECK(objects.Execute(Request(first), Access().deadline).http_status ==
            404);
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), file_id)));
  Q4T_CHECK(Count(objects) ==
            1);  // Receipt does not retain deleted file/chunks.
  return true;
}

Q4T_TEST(uploads_subset_repeated_parts_preserve_exact_order_and_binary_bytes) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const std::string bytes("a\0\xff", 3);
  const auto id = Create(uploads, Body(6));
  const auto part = Add(objects, id, bytes);
  Add(objects, id, "unused");
  auto complete = uploads.Complete(Access(), id, CompleteBody({part, part}));
  Q4T_CHECK(Ok(complete) &&
            Content(objects, complete.value.Find("file")->GetString("id")) ==
                bytes + bytes);
  Q4T_CHECK(Count(objects) == 3);  // Receipt, File, one immutable chunk.
  BinaryManifest repeated{
      6, {{"chunk_1", Digest(bytes), 3}, {"chunk_1", Digest(bytes), 3}}},
      parsed;
  Q4T_CHECK(ParseBinaryManifest(BinaryManifestJson(repeated), &parsed));
  repeated.chunks[1].sha256 = Digest("xyz");
  Q4T_CHECK(!ParseBinaryManifest(BinaryManifestJson(repeated), &parsed));
  return true;
}

Q4T_TEST(
    uploads_bad_md5_bytes_foreign_parts_and_crypto_failure_do_not_publish) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto id = Create(uploads, Body()), foreign = Create(uploads, Body());
  const auto part = Add(objects, id, "abc"),
             other = Add(objects, foreign, "xyz");
  for (const auto& body :
       {CompleteBody({part}, std::string(32, '0')), CompleteBody({}),
        CompleteBody({other}), CompleteBody({part, part}),
        Object({{"part_ids", Array({Number(1)})}}),
        Object({{"part_ids", Array()}, {"md5", Json()}})})
    Q4T_CHECK(!uploads.Complete(Access(), id, body).ok());
  Q4T_CHECK(uploads.Complete(Access("client:other"), id, CompleteBody({part}))
                .http_status == 404);
  SetCryptoUnavailableForTesting(true);
  const auto failure = uploads.Complete(
      Access(), id, CompleteBody({part}, "900150983cd24fb0d6963f7d28e17f72"));
  // Restore even if the assertion below fails.
  SetCryptoUnavailableForTesting(false);
  Q4T_CHECK(failure.code == "upload_checksum_unavailable" &&
            uploads.Retrieve(Access(), id).value.GetString("status") ==
                "pending" &&
            Files(objects)
                .List(Access(), Object())
                .value.GetArray("data")
                ->array.empty());
  SetCryptoUnavailableForTesting(true);
  const auto without_md5 = uploads.Complete(Access(), id, CompleteBody({part}));
  SetCryptoUnavailableForTesting(false);
  Q4T_CHECK(Ok(without_md5));
  return true;
}

Q4T_TEST(uploads_parallel_parts_then_complete_and_late_part_cancel) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto id = Create(uploads, Body(8));
  std::barrier ready(8);
  std::array<ObjectReply, 8> replies;
  std::vector<std::thread> threads;
  for (size_t i = 0; i < replies.size(); ++i) {
    threads.emplace_back([&, i] {
      UploadPart part(objects, Access(), id, "multipart/form-data; boundary=p");
      auto result = part.Feed(Wire(std::string(1, 'a' + i)), Access().deadline);
      ready.arrive_and_wait();
      replies[i] = result.ok() ? part.Finish() : result;
    });
  }
  for (auto& thread : threads) thread.join();
  Json ordered = Array();
  for (const auto& reply : replies) {
    Q4T_CHECK(Ok(reply));
    ordered.array.push_back(String(reply.value.GetString("id")));
  }
  auto complete =
      uploads.Complete(Access(), id, Object({{"part_ids", ordered}}));
  Q4T_CHECK(Ok(complete) &&
            Content(objects, complete.value.Find("file")->GetString("id")) ==
                "abcdefgh");
  const auto cancel_id = Create(uploads, Body());
  UploadPart late(objects, Access(), cancel_id,
                  "multipart/form-data; boundary=p");
  Q4T_CHECK(Ok(late.Feed(Wire("abc"), Access().deadline)));
  Q4T_CHECK(Ok(uploads.Cancel(Access(), cancel_id)));
  Q4T_CHECK(late.Finish().http_status == 409 && Ok(late.Abort(Access())));
  Q4T_CHECK(uploads.Cancel(Access(), cancel_id).value.GetString("status") ==
            "cancelled");
  return true;
}

Q4T_TEST(uploads_actual_sdk_multipart_truncation_and_no_early_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto directory =
      fs::path(__FILE__).parent_path().parent_path() /
      "docs/evidence/serve-multipart-cpu-20261110/multipart-cpu-02/sdk";
  const auto read_file = [](const fs::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("missing SDK encoding fixture");
    return std::string(std::istreambuf_iterator<char>(input), {});
  };
  Json contract;
  Q4T_CHECK(q4t::io::ParseJson(read_file(directory / "part.json"), &contract));
  const auto wire = read_file(directory / "part.body");
  const auto id = Create(uploads, Body(256));
  UploadPart part(objects, Access(), id, contract.GetString("content_type"));
  for (size_t i = 0; i < wire.size(); i += 3)
    Q4T_CHECK(
        Ok(part.Feed(std::string_view(wire).substr(i, 3), Access().deadline)));
  auto raw = objects.Execute(Request(id), Access().deadline);
  Q4T_CHECK(raw.value.Find("data")->GetArray("parts")->array.empty());
  auto result = part.Finish();
  Q4T_CHECK(Ok(result));
  auto completed = uploads.Complete(
      Access(), id, CompleteBody({result.value.GetString("id")}));
  Q4T_CHECK(Ok(completed) &&
            Content(objects, completed.value.Find("file")->GetString("id")) ==
                read_file(directory / "binary.bin"));
  const auto broken = Create(uploads, Body(256));
  UploadPart truncated(objects, Access(), broken,
                       contract.GetString("content_type"));
  Q4T_CHECK(
      Ok(truncated.Feed(wire.substr(0, wire.size() - 5), Access().deadline)));
  Q4T_CHECK(!truncated.Finish().ok() && Ok(truncated.Abort(Access())));
  raw = objects.Execute(Request(broken), Access().deadline);
  Q4T_CHECK(raw.value.Find("data")->GetArray("parts")->array.empty());
  return true;
}

Q4T_TEST(uploads_durable_restart_then_complete_retains_file_independently) {
  Fixture fixture;
  std::string id, first;
  {
    ObjectStore objects(fixture.options);
    Uploads uploads(objects);
    id = Create(uploads, Body(6));
    first = Add(objects, id, "abc");
  }
  std::string file_id;
  {
    ObjectStore objects(fixture.options);
    Uploads uploads(objects);
    const auto second = Add(objects, id, "def");
    const auto result =
        uploads.Complete(Access(), id, CompleteBody({first, second}));
    Q4T_CHECK(Ok(result));
    file_id = result.value.Find("file")->GetString("id");
    Q4T_CHECK(Ok(objects.Execute(Request(id, ObjectOperation::kErase),
                                 Access().deadline)));
  }
  ObjectStore objects(fixture.options);
  Q4T_CHECK(Content(objects, file_id) == "abcdef");
  return true;
}

Q4T_TEST(uploads_expired_receipt_releases_parts_without_a_client_request) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  // Create the durable form with an already expired public deadline. A fresh
  // identity avoids mutating immutable created_at just to advance test time.
  const auto original = Create(uploads, Body());
  Add(objects, original, "abc");
  auto raw = objects.Execute(Request(original), Access().deadline);
  auto expired = Copy(raw.value);
  expired.id = "upload_expired";
  expired.expected_revision = 0;
  expired.created_at = std::time(nullptr) - 3601;
  expired.expires_at = expired.created_at + 3600 + 86400;
  auto& upload = Field(expired.data, "upload");
  Set(&upload, "id", String(expired.id));
  Set(&upload, "created_at", Number(expired.created_at));
  Set(&upload, "expires_at", Number(expired.created_at + 3600));
  Q4T_CHECK(Ok(objects.Execute(expired, Access().deadline)));
  Q4T_CHECK(Ok(objects.Execute(Request(original, ObjectOperation::kErase),
                               Access().deadline)));
  Q4T_CHECK(Count(objects) == 1);
  Q4T_CHECK(uploads.Retrieve(Access(), expired.id).value.GetString("status") ==
            "expired");
  Q4T_CHECK(uploads.Cancel(Access(), expired.id).value.GetString("status") ==
            "expired");
  Q4T_CHECK(
      Part(objects, expired.id, "late").http_status == 409 &&
      uploads.Complete(Access(), expired.id, CompleteBody({})).http_status ==
          409);
  return true;
}

Q4T_TEST(uploads_mutation_cutoff_rejects_entire_publication_transaction) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto original = Request("receipt", ObjectOperation::kPut);
  original.data = Object({{"value", Number(1)}});
  Q4T_CHECK(Ok(objects.Execute(original, Access().deadline)));
  auto update = original;
  update.expected_revision = 1;
  update.mutate_before = std::time(nullptr);
  update.data = Object({{"value", Number(2)}});
  auto extra = original;
  extra.id = "new_receipt";
  const std::array requests{extra, update};
  Q4T_CHECK(objects.ExecuteBatch(requests, Access().deadline).code ==
            "object_mutation_expired");
  Q4T_CHECK(objects.Execute(Request(extra.id), Access().deadline).http_status ==
            404);
  Q4T_CHECK(objects.Execute(update, Access().deadline).code ==
            "object_mutation_expired");
  auto current = objects.Execute(Request(original.id), Access().deadline);
  Q4T_CHECK(current.value.GetInt("revision") == 1 &&
            current.value.Find("data")->GetInt("value") == 1);
  update.mutate_before = std::time(nullptr) + 3600;
  Q4T_CHECK(Ok(objects.Execute(update, Access().deadline)));
  return true;
}

Q4T_TEST(uploads_corrupt_part_bytes_cannot_be_published_as_complete_file) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto id = Create(uploads, Body());
  const auto part = Add(objects, id, "abc");
  auto raw = objects.Execute(Request(part), Access().deadline);
  const auto chunk = raw.value.Find("data")
                         ->Find("blob")
                         ->GetArray("chunks")
                         ->array[0]
                         .GetString("id");
  auto bytes = objects.Execute(Request(chunk), Access().deadline);
  auto corrupt = Copy(bytes.value);
  Set(&corrupt.data, "base64",
      String("eHl6"));  // Valid outer JSON, wrong content.
  Q4T_CHECK(Ok(objects.Execute(corrupt, Access().deadline)));
  Q4T_CHECK(uploads.Complete(Access(), id, CompleteBody({part})).code ==
            "binary_state_invalid");
  Q4T_CHECK(uploads.Retrieve(Access(), id).value.GetString("status") ==
                "pending" &&
            Files(objects)
                .List(Access(), Object())
                .value.GetArray("data")
                ->array.empty());
  return true;
}

Q4T_TEST(uploads_lost_create_receipt_keeps_original_identity_after_recovery) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  Injection injection("--q4t-object-worker-lost-ack");
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto result = uploads.Create(Access(), Body(0));
  Q4T_CHECK(result.commit_unknown &&
            result.value.GetString("id").starts_with("upload_"));
  Reaped(objects);
  const auto stored = uploads.Retrieve(Access(), result.value.GetString("id"));
  Q4T_CHECK(Ok(stored) && stored.value.GetString("status") == "pending" &&
            Count(objects) == 1);
  return true;
}

Q4T_TEST(uploads_lost_part_receipt_recovery_has_one_part_and_no_replay) {
  Fixture fixture;
  std::string id;
  {
    ObjectStore objects(fixture.options);
    Uploads uploads(objects);
    id = Create(uploads, Body(0));
  }
  fixture.options.io_timeout_ms = 250;
  std::string part_id;
  {
    Injection injection("--q4t-object-worker-apply-pause");
    ObjectStore objects(fixture.options);
    UploadPart part(objects, Access(), id, "multipart/form-data; boundary=p");
    Q4T_CHECK(Ok(part.Feed(Wire(""), Access().deadline)));
    const auto failed = part.Finish();
    Q4T_CHECK(failed.commit_unknown &&
              failed.value.GetString("id").starts_with("part_"));
    part_id = failed.value.GetString("id");
    Q4T_CHECK(part.Finish().commit_unknown &&
              part.Abort(Access()).commit_unknown);
    Reaped(objects);
  }
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  auto stored = objects.Execute(Request(id), Access().deadline);
  Q4T_CHECK(Ok(stored) &&
            stored.value.Find("data")->GetArray("parts")->array.size() == 1);
  Q4T_CHECK(Ok(uploads.Complete(Access(), id, CompleteBody({part_id}))));
  return true;
}

Q4T_TEST(uploads_lost_complete_receipt_recovers_original_file_atomically) {
  Fixture fixture;
  std::string id;
  {
    ObjectStore objects(fixture.options);
    Uploads uploads(objects);
    id = Create(uploads, Body(0));
  }
  fixture.options.io_timeout_ms = 250;
  std::string file_id;
  {
    Injection injection("--q4t-object-worker-apply-pause");
    ObjectStore objects(fixture.options);
    Uploads uploads(objects);
    const auto failed = uploads.Complete(Access(), id, CompleteBody({}));
    Q4T_CHECK(failed.commit_unknown && failed.value.GetString("id") == id &&
              failed.value.GetString("file_id").starts_with("file_"));
    file_id = failed.value.GetString("file_id");
    Reaped(objects);
  }
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto result = uploads.Complete(Access(), id, CompleteBody({}));
  Q4T_CHECK(Ok(result) &&
            result.value.Find("file")->GetString("id") == file_id &&
            Files(objects)
                    .List(Access(), Object())
                    .value.GetArray("data")
                    ->array.size() == 1 &&
            Count(objects) == 2);
  return true;
}

Q4T_TEST(uploads_streaming_md5_known_vectors_and_terminal_state) {
  for (const auto& [input, expected] :
       {std::pair{"", "d41d8cd98f00b204e9800998ecf8427e"},
        {"abc", "900150983cd24fb0d6963f7d28e17f72"},
        {"abcdefghijklmnopqrstuvwxyz", "c3fcd3d76192e4007dfb496cca67e13b"}}) {
    CryptoMd5 digest;
    std::string actual = "unchanged";
    Q4T_CHECK(digest.Update("bad") == CryptoResult::kInvalid &&
              digest.Finish(&actual) == CryptoResult::kInvalid &&
              actual == "unchanged");
    Q4T_CHECK(digest.Begin() == CryptoResult::kOk);
    for (const char c : std::string_view(input))
      Q4T_CHECK(digest.Update(std::string_view(&c, 1)) == CryptoResult::kOk);
    Q4T_CHECK(digest.Finish(&actual) == CryptoResult::kOk &&
              actual == expected);
    Q4T_CHECK(digest.Update("late") == CryptoResult::kInvalid &&
              digest.Finish(&actual) == CryptoResult::kInvalid &&
              actual == expected);
  }
  return true;
}

Q4T_TEST(
    uploads_part_64m_boundary_streams_and_overflow_keeps_parent_unchanged) {
  Fixture fixture;
  fixture.options.max_client_bytes = 256ULL << 20;
  fixture.options.max_total_bytes = 512ULL << 20;
  ObjectStore objects(fixture.options);
  Uploads uploads(objects);
  const auto id = Create(uploads, Body(kUploadPartBytes));
  const std::string header =
      "--p\r\nContent-Disposition: form-data; name=\"data\"\r\n\r\n";
  const std::string chunk(kBinaryChunkBytes, 'x');
  std::string accepted_id;
  for (bool overflow : {false, true}) {
    auto access = Access();
    access.deadline = Clock::now() + 90s;
    UploadPart part(objects, access, id, "multipart/form-data; boundary=p");
    Q4T_CHECK(Ok(part.Feed(header, access.deadline)));
    for (int i = 0; i < 64; ++i)
      Q4T_CHECK(Ok(part.Feed(chunk, access.deadline)));
    if (overflow) Q4T_CHECK(Ok(part.Feed("x", access.deadline)));
    auto ended = part.Feed("\r\n--p--\r\n", access.deadline);
    if (ended.ok()) ended = part.Finish();
    if (overflow) {
      Q4T_CHECK(ended.http_status == 413 && Ok(part.Abort(Access())));
    } else {
      Q4T_CHECK(Ok(ended));
      accepted_id = ended.value.GetString("id");
    }
  }
  auto state = objects.Execute(Request(id), Access().deadline);
  Q4T_CHECK(state.value.Find("data")->GetArray("parts")->array.size() == 1 &&
            state.value.Find("data")->GetInt("received_bytes") ==
                static_cast<int64_t>(kUploadPartBytes));
  const auto completed =
      uploads.Complete(Access(), id, CompleteBody({accepted_id}));
  Q4T_CHECK(Ok(completed) && completed.value.Find("file")->GetInt("bytes") ==
                                 static_cast<int64_t>(kUploadPartBytes));
  BinaryRead read;
  Q4T_CHECK(Ok(Files(objects).Open(
      Access(), completed.value.Find("file")->GetString("id"), &read)));
  std::string actual;
  BinaryStore binary(objects);
  Q4T_CHECK(Ok(binary.Read(Access(), read, 63, &actual)) && actual == chunk &&
            Ok(binary.Close(Access(), &read)));
  return true;
}
