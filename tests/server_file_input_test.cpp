#include "q4t/server/file_input.h"

#include <cstdlib>
#include <filesystem>

#include "../src/server/object_protocol.h"
#include "q4t/server/files.h"
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
    const auto root =
        fs::absolute(output ? output : ".q4t-work/file-input-cpu");
    fs::create_directories(root);
    auto path = (root / "input-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("input fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:file-inputs") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
Json Part(std::string data = "YWJj") {
  return Object({{"type", String("input_file")},
                 {"file_data", String(std::move(data))},
                 {"filename", String("document.txt")}});
}
bool Parse(const Json& part, FileInputSource* source,
           FileInputProfile profile = FileInputProfile::kResponses) {
  auto result =
      ParseFileInputSource(part, profile, "input[0].content[0]", source);
  if (!result.ok()) std::fprintf(stderr, "%s\n", result.message.c_str());
  return result.ok();
}
ObjectRequest Request(std::string id, ObjectKind kind = ObjectKind::kToolRun,
                      ObjectOperation operation = ObjectOperation::kGet) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.id = std::move(id);
  request.kind = kind;
  request.operation = operation;
  request.ttl_seconds = 0;
  return request;
}
int64_t Count(ObjectStore& objects) {
  return objects
      .Execute(Request("", ObjectKind::kFile, ObjectOperation::kHealth),
               Access().deadline)
      .value.GetInt("objects", -1);
}
std::string Upload(ObjectStore& objects, std::string_view bytes) {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=f");
  const std::string head =
      "--f\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
      "user_data\r\n--f\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"real.txt\"\r\nContent-Type: text/plain\r\n\r\n";
  auto result = upload.Feed(head, Access().deadline);
  if (result.ok()) result = upload.Feed(bytes, Access().deadline);
  if (result.ok()) result = upload.Feed("\r\n--f--\r\n", Access().deadline);
  if (result.ok()) result = upload.Finish();
  if (!result.ok()) throw std::runtime_error(result.message);
  return result.value.GetString("id");
}
Json Stored(std::string id) {
  return Object(
      {{"type", String("input_file")}, {"file_id", String(std::move(id))}});
}
}  // namespace

Q4T_TEST(file_input_source_profiles_preserve_borrowed_data_and_metadata) {
  auto value = Part();
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source) &&
            source.value.data() == value.Find("file_data")->str.data() &&
            source.filename == "document.txt" && !source.pdf_only &&
            source.detail == "auto");
  auto chat =
      Object({{"type", String("file")},
              {"file", Object({{"file_id", String("file-original")},
                               {"filename", String("../metadata.pdf")}})}});
  Q4T_CHECK(Parse(chat, &source, FileInputProfile::kChat) && source.pdf_only &&
            source.value == "file-original" &&
            source.filename == "../metadata.pdf");
  auto file = *chat.Find("file");
  Set(&file, "file_url", String("https://example.com/a.pdf"));
  Set(&chat, "file", std::move(file));
  auto failed = ParseFileInputSource(chat, FileInputProfile::kChat,
                                     "messages[0].content[0]", &source);
  Q4T_CHECK(failed.http_status == 400 &&
            failed.value.GetString("param") ==
                "messages[0].content[0].file.file_url");
  return true;
}

Q4T_TEST(file_input_sources_reject_ambiguity_bad_fields_and_unsafe_urls) {
  for (const auto& value :
       {Object({{"type", String("input_file")}}),
        Object({{"type", String("input_file")}, {"file_id", String("")}}),
        Object(
            {{"type", String("input_file")}, {"file_id", String("../host")}}),
        Object({{"type", String("input_file")},
                {"file_data", String("")},
                {"file_id", String("file_x")}}),
        Object({{"type", String("input_file")},
                {"file_url", String("file:///etc/passwd")}}),
        Object({{"type", String("input_file")},
                {"file_url", String("https://user:pass@example.com/a")}}),
        Object({{"type", String("input_file")},
                {"file_url", String("https://example.com/\r\nx")}}),
        Object({{"type", String("input_file")}, {"file_data", Number(1)}})}) {
    FileInputSource source;
    auto result = ParseFileInputSource(value, FileInputProfile::kResponses,
                                       "input", &source);
    Q4T_CHECK(result.http_status == 400 && result.code == "invalid_request");
  }
  for (const auto* url : {"https://example.com/a.pdf?version=1#page=2",
                          "http://[::1]:8000/a.txt"}) {
    auto value =
        Object({{"type", String("input_file")}, {"file_url", String(url)}});
    FileInputSource source;
    Q4T_CHECK(Parse(value, &source) &&
              source.kind == FileInputSource::Kind::kUrl);
  }
  return true;
}

Q4T_TEST(file_input_nullable_variants_cache_and_data_uri_are_explicit) {
  auto value = Part("data:TEXT/PLAIN;charset=utf-8;base64,YWJj");
  Set(&value, "file_id", Json{});
  Set(&value, "detail", String("high"));
  Set(&value, "prompt_cache_breakpoint",
      Object({{"mode", String("explicit")}, {"ttl", String("12h")}}));
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source) && source.value == "YWJj" &&
            source.mime_type == "text/plain" && source.detail == "high" &&
            source.cache_breakpoint && source.cache_ttl == 43200);
  Set(&value, "filename", Json{});
  Q4T_CHECK(!ParseFileInputSource(value, FileInputProfile::kResponses, "input",
                                  &source)
                 .ok());
  Q4T_CHECK(Parse(value, &source, FileInputProfile::kResponsesStored));
  Set(&value, "prompt_cache_breakpoint", Json{});
  Q4T_CHECK(Parse(value, &source, FileInputProfile::kResponsesStored) &&
            !source.cache_breakpoint);
  Set(&value, "detail", Json{});
  Q4T_CHECK(!ParseFileInputSource(value, FileInputProfile::kResponsesStored,
                                  "input", &source)
                 .ok());
  for (const auto* payload : {"data:text/plain,abc", "data:text/plain;base64",
                              "data:text/plain;x=y;base64,YQ=="}) {
    const auto invalid = Part(payload);
    Q4T_CHECK(!ParseFileInputSource(invalid, FileInputProfile::kResponses,
                                    "input", &source)
                   .ok());
  }
  return true;
}

Q4T_TEST(file_input_inline_bytes_and_digest_require_no_storage) {
  auto value = Part("AP8NYQ==");
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source));
  FileInputBudget budget;
  FileInputContent content;
  const std::string bytes("\0\xff\ra", 4);
  Q4T_CHECK(
      ReadLocalFileInput(nullptr, Access(), source, &budget, &content).ok());
  Q4T_CHECK(content.bytes == bytes && content.sha256 == Digest(bytes) &&
            content.file_id.empty() && budget.used_bytes == 4 &&
            budget.used_files == 1);
  value = Part("");
  Q4T_CHECK(Parse(value, &source));
  Q4T_CHECK(
      ReadLocalFileInput(nullptr, Access(), source, &budget, &content).ok() &&
      content.bytes.empty() && content.sha256 == Digest("") &&
      budget.used_files == 2);
  return true;
}

Q4T_TEST(file_input_base64_rejects_noncanonical_or_partial_content) {
  for (const auto* data : {"YQ", "YR==", "YWJ=", "Y Q=", "YQ__",
                           "=Q==", "Y===", "YQ==YWJj", "YQ=A"}) {
    auto value = Part(data);
    FileInputSource source;
    Q4T_CHECK(Parse(value, &source));
    FileInputBudget budget;
    FileInputContent content{"old bytes", "old name", "old mime", "old id",
                             "old sha"};
    auto result =
        ReadLocalFileInput(nullptr, Access(), source, &budget, &content);
    Q4T_CHECK(result.http_status == 400 && content.bytes.empty() &&
              content.sha256.empty() &&
              result.value.GetString("param") ==
                  "input[0].content[0].file_data");
  }
  return true;
}

Q4T_TEST(file_input_aggregate_byte_and_item_budgets_do_not_refund_attempts) {
  auto value = Part();
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source));
  FileInputBudget budget{5, 0, 2, 0};
  FileInputContent content;
  Q4T_CHECK(
      ReadLocalFileInput(nullptr, Access(), source, &budget, &content).ok());
  auto result =
      ReadLocalFileInput(nullptr, Access(), source, &budget, &content);
  Q4T_CHECK(result.http_status == 413 && content.bytes.empty() &&
            budget.used_bytes == 3);
  value = Part("YQ==");
  Q4T_CHECK(Parse(value, &source));
  Q4T_CHECK(
      ReadLocalFileInput(nullptr, Access(), source, &budget, &content).ok());
  Q4T_CHECK(ReadLocalFileInput(nullptr, Access(), source, &budget, &content)
                .http_status == 413);
  FileInputBudget invalid{kFileInputBytes + 1};
  Q4T_CHECK(ReadLocalFileInput(nullptr, Access(), source, &invalid, &content)
                .http_status == 500);
  return true;
}

Q4T_TEST(file_input_cancellation_and_deadline_publish_no_partial_bytes) {
  auto value = Part(std::string(8192, 'A'));
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source));
  FileInputBudget budget;
  FileInputContent content;
  auto access = Access();
  int checks = 0;
  access.cancelled = [&] { return ++checks == 3; };
  auto result = ReadLocalFileInput(nullptr, access, source, &budget, &content);
  Q4T_CHECK(result.http_status == 499 && content.bytes.empty() &&
            budget.used_bytes == 6144);
  access = Access();
  access.deadline = Clock::now() - 1ms;
  result = ReadLocalFileInput(nullptr, access, source, &budget, &content);
  Q4T_CHECK(result.http_status == 408 && content.bytes.empty() &&
            budget.used_files == 1);
  access = Access("");
  Q4T_CHECK(ReadLocalFileInput(nullptr, access, source, &budget, &content)
                .http_status == 503);
  return true;
}

Q4T_TEST(file_input_stored_multichunk_preserves_original_mime_and_bytes) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  std::string bytes(kBinaryChunkBytes + 257, 'x');
  bytes[7] = '\0';
  bytes[8] = '\xff';
  const auto id = Upload(objects, bytes);
  auto value = Stored(id);
  Set(&value, "filename", String("../caller-metadata.pdf"));
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source));
  FileInputBudget budget;
  FileInputContent content;
  const auto before = Count(objects);
  Q4T_CHECK(
      ReadLocalFileInput(&objects, Access(), source, &budget, &content).ok());
  Q4T_CHECK(content.bytes == bytes && content.file_id == id &&
            content.filename == "real.txt" &&
            content.mime_type == "text/plain" &&
            content.sha256 == Digest(bytes) && Count(objects) == before &&
            budget.used_bytes == bytes.size());
  return true;
}

Q4T_TEST(file_input_stored_owner_and_size_gate_precede_pin_and_content) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto id = Upload(objects, "abc");
  const auto value = Stored(id);
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source));
  FileInputBudget budget{2};
  FileInputContent content;
  const auto before = Count(objects);
  auto result =
      ReadLocalFileInput(&objects, Access(), source, &budget, &content);
  Q4T_CHECK(result.http_status == 413 && Count(objects) == before &&
            budget.used_files == 0);
  budget.max_files = 0;
  Q4T_CHECK(ReadLocalFileInput(&objects, Access(), source, &budget, &content)
                    .http_status == 500 &&
            Count(objects) == before);
  budget = {};
  result = ReadLocalFileInput(&objects, Access("client:foreign"), source,
                              &budget, &content);
  Q4T_CHECK(result.http_status == 404 && content.bytes.empty() &&
            Count(objects) == before);
  Q4T_CHECK(ReadLocalFileInput(nullptr, Access(), source, &budget, &content)
                .http_status == 503);
  Q4T_CHECK(Files(objects).Erase(Access(), id).ok());
  Q4T_CHECK(ReadLocalFileInput(&objects, Access(), source, &budget, &content)
                .http_status == 404);
  return true;
}

Q4T_TEST(file_input_stored_corruption_does_not_publish_partial_content) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto id = Upload(objects, "abc");
  auto root =
      objects.Execute(Request(id, ObjectKind::kFile), Access().deadline);
  const auto chunk = root.value.Find("data")
                         ->Find("blob")
                         ->GetArray("chunks")
                         ->array[0]
                         .GetString("id");
  auto record = objects.Execute(Request(chunk), Access().deadline);
  auto update = Request(chunk, ObjectKind::kToolRun, ObjectOperation::kPut);
  update.expected_revision = record.value.GetInt("revision");
  update.data = *record.value.Find("data");
  Set(&update.data, "base64", String("ZGVm"));
  Q4T_CHECK(objects.Execute(update, Access().deadline).ok());
  const auto value = Stored(id);
  FileInputSource source;
  Q4T_CHECK(Parse(value, &source));
  FileInputBudget budget;
  FileInputContent content;
  const auto before = Count(objects);
  const auto result =
      ReadLocalFileInput(&objects, Access(), source, &budget, &content);
  Q4T_CHECK(result.http_status == 503 && content.bytes.empty() &&
            content.sha256.empty() && Count(objects) == before);
  return true;
}
