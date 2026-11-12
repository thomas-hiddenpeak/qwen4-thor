#include "q4t/server/files.h"
#include "q4t/server/files_http.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/http_response.h"
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
    auto root = fs::absolute(output ? output : ".q4t-work/files-cpu");
    fs::create_directories(root);
    auto path = (root / "files-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("files fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:files") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%d %s: %s\n", reply.http_status, reply.code.c_str(),
                 reply.message.c_str());
  return reply.ok();
}
std::string Text(std::string name, std::string text) {
  return "--f\r\nContent-Disposition: form-data; name=\"" + name +
         "\"\r\n\r\n" + text + "\r\n";
}
std::string File(std::string bytes, std::string filename = "资料.txt") {
  return "--f\r\nContent-Disposition: form-data; name=\"file\"; "
         "filename=\"" +
         filename + "\"\r\nContent-Type: application/octet-stream\r\n\r\n" +
         bytes + "\r\n";
}
ObjectReply Upload(ObjectStore& objects, const std::string& wire,
                   std::string scope = "client:files") {
  auto access = Access(scope);
  FileUpload upload(objects, access, "multipart/form-data; boundary=f");
  auto reply = upload.Feed(wire, access.deadline);
  if (reply.ok()) reply = upload.Finish();
  if (!reply.ok()) upload.Abort(Access(scope));
  return reply;
}
ObjectRequest EmptyFile(int index, std::string purpose = "user_data") {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = ObjectKind::kFile;
  request.scope = "client:files";
  request.id = "file_" + std::to_string(index);
  request.created_at = std::time(nullptr) - 200 + index;
  request.ttl_seconds = 0;
  request.data =
      Object({{"format", String("q4t.file.v1")},
              {"blob", BinaryManifestJson({})},
              {"file", Object({{"id", String(request.id)},
                               {"object", String("file")},
                               {"created_at", Number(request.created_at)},
                               {"bytes", Number(0)},
                               {"filename", String("empty.jsonl")},
                               {"purpose", String(purpose)},
                               {"status", String("processed")}})}});
  return request;
}
int64_t Count(ObjectStore& objects) {
  auto request = EmptyFile(1);
  request.operation = ObjectOperation::kHealth;
  return objects.Execute(request, Access().deadline)
      .value.GetInt("objects", -1);
}
std::string Read(const fs::path& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) throw std::runtime_error("missing actual SDK fixture");
  return {std::istreambuf_iterator<char>(input), {}};
}
struct Sockets {
  int fd[2];
  Sockets() {
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, fd))
      throw std::runtime_error("socketpair");
  }
  ~Sockets() {
    close(fd[0]);
    close(fd[1]);
  }
  std::string Take() {
    std::string out;
    char buffer[4096];
    for (;;) {
      const auto size = recv(fd[1], buffer, sizeof(buffer), MSG_DONTWAIT);
      if (size <= 0) return out;
      out.append(buffer, size);
    }
  }
};
}  // namespace

Q4T_TEST(files_actual_sdk_upload_retrieve_and_binary_content) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto directory =
      fs::path(__FILE__).parent_path().parent_path() /
      "docs/evidence/serve-multipart-cpu-20261110/multipart-cpu-02/sdk";
  Json contract;
  Q4T_CHECK(q4t::io::ParseJson(Read(directory / "file.json"), &contract));
  const auto wire = Read(directory / "file.body");
  FileUpload upload(objects, Access(), contract.GetString("content_type"));
  for (size_t i = 0; i < wire.size(); i += 3)
    Q4T_CHECK(Ok(
        upload.Feed(std::string_view(wire).substr(i, 3), Access().deadline)));
  Files files(objects);
  Q4T_CHECK(
      files.List(Access(), Object()).value.GetArray("data")->array.empty());
  auto result = upload.Finish();
  Q4T_CHECK(Ok(result) && result.value.GetInt("bytes") == 256 &&
            result.value.GetString("filename") == "资料.bin" &&
            result.value.GetString("purpose") == "user_data" &&
            result.value.GetInt("expires_at") -
                    result.value.GetInt("created_at") ==
                3600);
  const auto id = result.value.GetString("id");
  Q4T_CHECK(OutputJson(files.Retrieve(Access(), id).value) ==
            OutputJson(result.value));
  Q4T_CHECK(!upload.Finish().ok());
  BinaryRead read;
  Q4T_CHECK(Ok(files.Open(Access(), id, &read)));
  BinaryStore binary(objects);
  std::string content;
  Q4T_CHECK(Ok(binary.Read(Access(), read, 0, &content)) &&
            content == Read(directory / "binary.bin"));
  Q4T_CHECK(Ok(binary.Close(Access(), &read)));
  return true;
}

Q4T_TEST(files_schema_emits_actual_records_and_expiration) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Files files(objects);
  auto evals = Upload(objects, File("actual document bytes") +
                                  Text("purpose", "evals") + "--f--");
  auto batch = Upload(objects, File("{}\n", "batch.jsonl") +
                                  Text("purpose", "batch") + "--f--");
  Q4T_CHECK(Ok(evals) && Ok(batch));
  auto listing = files.List(Access(), Object());
  auto deleted = files.Erase(Access(), evals.value.GetString("id"));
  Q4T_CHECK(Ok(listing) && Ok(deleted));
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto directory = fs::path(output ? output : ".q4t-work/files-cpu");
  fs::create_directories(directory);
  std::ofstream file(directory / "files-contract.json");
  file << OutputJson(Object({{"evals", evals.value}, {"batch", batch.value},
                            {"list", listing.value}, {"deleted", deleted.value}}));
  Q4T_CHECK(file.good());
  return true;
}

Q4T_TEST(files_all_purposes_defaults_and_explicit_retention) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  for (const auto* purpose :
       {"assistants", "batch", "fine-tune", "vision", "user_data", "evals"}) {
    const auto result =
        Upload(objects, Text("purpose", purpose) + File("{}\n", "data.jsonl") +
                            "--f--\r\n");
    Q4T_CHECK(Ok(result) && result.value.GetString("purpose") == purpose);
    if (std::string_view(purpose) == "batch")
      Q4T_CHECK(result.value.GetInt("expires_at") -
                    result.value.GetInt("created_at") ==
                2592000);
    else
      Q4T_CHECK(!result.value.Has("expires_at"));
  }
  const auto explicit_ttl = Upload(
      objects, Text("expires_after[seconds]", "3600") + File("") +
                   Text("purpose", "user_data") +
                   Text("expires_after[anchor]", "created_at") + "--f--");
  Q4T_CHECK(Ok(explicit_ttl) && explicit_ttl.value.GetInt("bytes") == 0 &&
            explicit_ttl.value.GetInt("expires_at") -
                    explicit_ttl.value.GetInt("created_at") ==
                3600);
  return true;
}

Q4T_TEST(files_invalid_form_never_publishes_and_cleanup_reclaims_staging) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto file = File("private staged bytes");
  const auto purpose = Text("purpose", "user_data");
  for (const auto& wire :
       {file + "--f--", purpose + "--f--", file + purpose + purpose + "--f--",
        file + purpose + Text("owner", "client:victim") + "--f--",
        file + Text("purpose", "null") + "--f--", file + purpose,
        file + purpose + "--f-", file + file + purpose + "--f--",
        File("x", "") + purpose + "--f--",
        file + Text("purpose", "fine-tune") + "--f--"}) {
    Q4T_CHECK(!Upload(objects, wire).ok());
    Q4T_CHECK(Count(objects) == 0);
  }
  return true;
}

Q4T_TEST(files_expiration_contract_rejects_missing_and_noninteger_fields) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto base = File("x") + Text("purpose", "user_data");
  for (const auto* seconds :
       {"", "null", "3599", "2592001", "3600.0", "+3600"}) {
    const auto result =
        Upload(objects, base + Text("expires_after[anchor]", "created_at") +
                            Text("expires_after[seconds]", seconds) + "--f--");
    Q4T_CHECK(result.http_status == 400 &&
              result.value.GetString("param") == "expires_after" &&
              Count(objects) == 0);
  }
  for (const auto& expiration :
       {Text("expires_after[seconds]", "3600"),
        Text("expires_after[anchor]", "created_at"),
        Text("expires_after[anchor]", "last_active_at") +
            Text("expires_after[seconds]", "3600")})
    Q4T_CHECK(!Upload(objects, base + expiration + "--f--").ok() &&
              Count(objects) == 0);
  return true;
}

Q4T_TEST(files_standard_list_crosses_worker_pages_and_filters_before_limit) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  std::vector<ObjectRequest> batch;
  for (int i = 1; i <= 105; ++i)
    batch.push_back(EmptyFile(i, i % 2 ? "evals" : "user_data"));
  Q4T_CHECK(Ok(objects.ExecuteBatch(batch, Access().deadline)));
  Files files(objects);
  auto all = files.List(Access(), Object());
  Q4T_CHECK(Ok(all) && all.value.GetArray("data")->array.size() == 105 &&
            !all.value.GetBool("has_more") &&
            all.value.GetString("first_id") == "file_105");
  auto query = Object({{"purpose", String("evals")},
                       {"limit", Number(2)},
                       {"order", String("asc")}});
  auto first = files.List(Access(), query);
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more") &&
            first.value.GetString("first_id") == "file_1" &&
            first.value.GetString("last_id") == "file_3");
  Set(&query, "after", String("file_3"));
  auto second = files.List(Access(), query);
  Q4T_CHECK(Ok(second) && second.value.GetString("first_id") == "file_5");
  Q4T_CHECK(files.List(Access("client:other"), query).http_status == 404);
  auto filtered =
      files.List(Access(), Object({{"purpose", String("arbitrary-no-match")}}));
  Q4T_CHECK(Ok(filtered) && filtered.value.GetArray("data")->array.empty() &&
            !filtered.value.GetBool("has_more"));
  return true;
}

Q4T_TEST(files_projection_hides_internal_records_and_binary_manifests) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto file = Upload(
      objects, File("file data") + Text("purpose", "user_data") + "--f--");
  Q4T_CHECK(Ok(file));
  auto hidden = EmptyFile(1);
  hidden.id = "internal_keyring";
  hidden.data = Object({{"private", String("not public file metadata")}});
  Q4T_CHECK(Ok(objects.Execute(hidden, Access().deadline)));
  Files files(objects);
  auto list = files.List(Access(), Object());
  Q4T_CHECK(Ok(list) && list.value.GetArray("data")->array.size() == 1 &&
            OutputJson(list.value).find("base64") == std::string::npos);
  Q4T_CHECK(files.Retrieve(Access(), hidden.id).http_status == 404);
  auto projected = EmptyFile(1);
  projected.operation = ObjectOperation::kList;
  projected.file_metadata_only = true;
  auto page = objects.Execute(projected, Access().deadline);
  Q4T_CHECK(Ok(page));
  for (const auto& record : page.value.GetArray("data")->array)
    Q4T_CHECK(!record.Find("data")->Has("blob"));
  projected.kind = ObjectKind::kResponse;
  Q4T_CHECK(objects.Execute(projected, Access().deadline).http_status == 400);
  return true;
}

Q4T_TEST(files_scope_delete_and_metadata_only_filename) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto result =
      Upload(objects, File("data", "../never-a-path.txt") +
                          Text("purpose", "user_data") + "--f--");
  Q4T_CHECK(Ok(result) &&
            result.value.GetString("filename") == "../never-a-path.txt");
  Q4T_CHECK(!fs::exists(fs::path(fixture.options.directory).parent_path() /
                        "never-a-path.txt"));
  Files files(objects);
  const auto id = result.value.GetString("id");
  Q4T_CHECK(files.Retrieve(Access("client:other"), id).http_status == 404 &&
            files.Erase(Access("client:other"), id).http_status == 404);
  const auto erased = files.Erase(Access(), id);
  Q4T_CHECK(Ok(erased) && erased.value.GetBool("deleted") &&
            erased.value.GetString("object") == "file");
  Q4T_CHECK(files.Retrieve(Access(), id).http_status == 404 &&
            Count(objects) == 0);
  return true;
}

Q4T_TEST(files_invalid_queries_and_routes_do_not_open_storage) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  Files files(objects);
  for (const auto& query :
       {Object({{"limit", Number(0)}}), Object({{"limit", Number(10001)}}),
        Object({{"limit", Json()}}), Object({{"limit", String("10")}}),
        Object({{"order", String("unknown")}}),
        Object({{"purpose", Number(1)}}),
        Object({{"after", String("../file")}}),
        Object({{"unknown", String("field")}})})
    Q4T_CHECK(files.List(Access(), query).http_status == 400);
  for (const auto& target :
       {"/v1/files/x/unknown", "/v1/files/..", "/v1/files-extra"}) {
    FileHttpRequest request(objects, Access());
    HttpBodyStream stream;
    Q4T_CHECK(request.Prepare("GET", target, "", &stream).http_status == 404);
  }
  FileHttpRequest bad_method(objects, Access());
  HttpBodyStream stream;
  auto failure = bad_method.Prepare("PUT", "/v1/files/file_a", "", &stream);
  Q4T_CHECK(failure.http_status == 405 &&
            failure.value.GetString("allow") == "GET, DELETE");
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(ClassifyHttpConnection("GET", "/v1/files") ==
                HttpConnectionClass::kBusiness &&
            ClassifyHttpConnection("GET", "/v1/files/file_a/content") ==
                HttpConnectionClass::kBusiness &&
            ClassifyHttpConnection("GET", "/v1/files/file_a") ==
                HttpConnectionClass::kObservation);
  return true;
}

Q4T_TEST(files_http_incremental_upload_and_list_use_production_handlers) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto wire = Text("purpose", "user_data") + File("raw bytes") + "--f--";
  Sockets upload_socket;
  {
    detail::HttpResponseContext context(upload_socket.fd[0]);
    context.SetKeepAlive(true);
    FileHttpRequest request(objects, Access());
    HttpBodyStream stream;
    Q4T_CHECK(Ok(request.Prepare("POST", "/v1/files",
                                 "multipart/form-data; boundary=f", &stream)) &&
              stream.reservation_bytes == kFileWorkingBytes);
    for (size_t i = 0; i < wire.size(); i += 7)
      Q4T_CHECK(stream.consume(std::string_view(wire).substr(i, 7),
                               Access().deadline));
    request.Serve(upload_socket.fd[0]);
    Q4T_CHECK(context.FinishResponse());
  }
  const auto response = upload_socket.Take();
  Q4T_CHECK(response.starts_with("HTTP/1.1 200") &&
            response.find("\"bytes\":9") != response.npos);
  Json created;
  Q4T_CHECK(q4t::io::ParseJson(response.substr(response.find("\r\n\r\n") + 4),
                               &created));
  Sockets content_socket;
  {
    detail::HttpResponseContext context(content_socket.fd[0]);
    context.SetKeepAlive(true);
    FileHttpRequest request(objects, Access());
    HttpBodyStream stream;
    Q4T_CHECK(Ok(request.Prepare(
        "GET", "/v1/files/" + created.GetString("id") + "/content", "",
        &stream)));
    request.Serve(content_socket.fd[0]);
    Q4T_CHECK(context.FinishResponse());
  }
  const auto content = content_socket.Take();
  Q4T_CHECK(content.substr(content.find("\r\n\r\n") + 4) == "raw bytes" &&
            Count(objects) == 2);
  Sockets list_socket;
  {
    detail::HttpResponseContext context(list_socket.fd[0]);
    context.SetKeepAlive(true);
    FileHttpRequest request(objects, Access());
    HttpBodyStream stream;
    Q4T_CHECK(Ok(request.Prepare("GET", "/v1/files", "", &stream)) &&
              request.WireLimit() == 0 &&
              stream.reservation_bytes == kFileListWorkingBytes);
    request.Serve(list_socket.fd[0]);
    Q4T_CHECK(context.FinishResponse());
  }
  const auto listed = list_socket.Take();
  Json list;
  Q4T_CHECK(
      listed.find("Content-Type: application/json") != listed.npos &&
      q4t::io::ParseJson(listed.substr(listed.find("\r\n\r\n") + 4), &list) &&
      list.GetArray("data")->array.size() == 1 && !list.GetBool("has_more"));
  return true;
}

Q4T_TEST(files_http_abandoned_complete_payload_never_publishes) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  {
    FileHttpRequest request(objects, Access());
    HttpBodyStream stream;
    Q4T_CHECK(Ok(request.Prepare("POST", "/v1/files",
                                 "multipart/form-data; boundary=f", &stream)));
    Q4T_CHECK(stream.consume(File("full payload but HTTP truncated") +
                                 Text("purpose", "user_data") + "--f--",
                             Access().deadline));
    // The HTTP framing check failed: do not call Serve/Finish, even though
    // the complete multipart payload was already staged.
    Q4T_CHECK(Count(objects) == 2);
  }
  Q4T_CHECK(Count(objects) == 0);
  return true;
}

Q4T_TEST(files_http_binary_download_and_source_failure_framing) {
  const std::string payload("a\0\xffz", 4);
  for (int failure = 0; failure < 4; ++failure) {
    Sockets sockets;
    detail::HttpResponseContext context(sockets.fd[0]);
    context.SetKeepAlive(true);
    int calls = 0;
    bool sent = false;
    try {
      sent =
          detail::SendBinaryResponse(sockets.fd[0], 4, [&](std::string* bytes) {
            ++calls;
            if (failure == 3) throw std::runtime_error("read error");
            *bytes = failure == 0 ? payload : failure == 1 ? "" : "oversized";
            return true;
          });
    } catch (const std::runtime_error&) {
      Q4T_CHECK(failure == 3);
    }
    Q4T_CHECK(sent == (failure == 0));
    Q4T_CHECK(context.FinishResponse() == (failure == 0));
    if (failure) detail::SendExceptionResponse(sockets.fd[0], *context.State());
    const auto wire = sockets.Take();
    Q4T_CHECK(wire.find("Content-Length: 4") != wire.npos && calls == 1);
    Q4T_CHECK(wire.substr(wire.find("\r\n\r\n") + 4) ==
              (failure ? "" : payload));
  }
  return true;
}
