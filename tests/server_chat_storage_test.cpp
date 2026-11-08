#include "q4t/server/chat_storage.h"

#include <cstdlib>
#include <ctime>
#include <filesystem>

#include "q4t/server/application_routes.h"
#include "q4t/server/application_query.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/token_logprobs.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("fixture JSON");
  return value;
}
Json String(std::string text) {
  Json value;
  value.type = Json::Type::kString;
  value.str = std::move(text);
  return value;
}
void Set(Json* value, std::string name, Json item) {
  for (auto& [key, current] : value->object) {
    if (key == name) {
      current = std::move(item);
      return;
    }
  }
  value->object.emplace_back(std::move(name), std::move(item));
}
StateAccess Access(std::string scope = "client:a") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root =
        fs::absolute(output ? output : ".q4t-work/chat-storage-host");
    fs::create_directories(root);
    auto name = (root / "stored-chat-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("fixture directory");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
};
Json Input(std::string metadata = R"({"project":"q4t"})") {
  return Parse(
      R"({"store":true,"metadata":)" + metadata +
      R"(,"messages":[{"role":"user","name":"reader","content":"read project records"}]})");
}
Json Output(const std::string& id = "chatcmpl_test", int64_t age = 0) {
  return Parse(
      "{\"id\":\"" + id +
      "\",\"created\":" + std::to_string(std::time(nullptr) - age) +
      R"(,"object":"chat.completion","model":"q4t","choices":[{"index":0,"finish_reason":"stop","logprobs":null,"message":{"role":"assistant","content":"资料✅"}}],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}})");
}
ObjectReply ReadRecord(ObjectStore& objects, std::string id) {
  ObjectRequest request;
  request.kind = ObjectKind::kChat;
  request.scope = "client:a";
  request.id = std::move(id);
  return objects.Execute(request, Clock::now() + 5s);
}
}  // namespace

Q4T_TEST(chat_storage_options_nullable_metadata_and_unicode_limits) {
  ChatStorageOptions options;
  std::string param;
  Q4T_CHECK(ParseChatStorageOptions(Parse("{}"), &options, &param));
  Q4T_CHECK(!options.store && !options.has_metadata);
  Q4T_CHECK(ParseChatStorageOptions(Parse(R"({"store":null,"metadata":null})"),
                                    &options, &param));
  Q4T_CHECK(!options.store && options.has_metadata &&
            options.metadata.IsNull());
  Q4T_CHECK(
      !ParseChatStorageOptions(Parse(R"({"store":1})"), &options, &param) &&
      param == "store");
  std::string key, value;
  for (int i = 0; i < 64; ++i) key += "键";
  for (int i = 0; i < 512; ++i) value += "✅";
  auto input = Input("{}");
  auto metadata = Parse("{}");
  Set(&metadata, key, String(value));
  Set(&input, "metadata", metadata);
  Q4T_CHECK(ParseChatStorageOptions(input, &options, &param));
  Set(&metadata, key, String(value + "x"));
  Set(&input, "metadata", metadata);
  Q4T_CHECK(!ParseChatStorageOptions(input, &options, &param));
  metadata = Parse("{}");
  for (int i = 0; i < 17; ++i) Set(&metadata, std::to_string(i), String("x"));
  Set(&input, "metadata", metadata);
  Q4T_CHECK(!ParseChatStorageOptions(input, &options, &param) &&
            param == "metadata");
  return true;
}

Q4T_TEST(chat_storage_disabled_does_not_open_storage) {
  ObjectStoreOptions options;
  options.directory = "/not-opened/stored-chats";
  ObjectStore objects(options);
  auto request = Input();
  Set(&request, "store", Parse("false"));
  const auto response = Output();
  auto done =
      FinalizeStoredChat(&objects, request, OutputJson(response), Access());
  Q4T_CHECK(Ok(done) &&
            done.value.Find("metadata")->GetString("project") == "q4t");
  Q4T_CHECK(
      done.value.GetArray("choices")->array.front().Find("message")->GetString(
          "content") == "资料✅");
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(
      Ok(FinalizeStoredChat(nullptr, request, OutputJson(response), Access())));
  return true;
}

Q4T_TEST(chat_storage_crud_restart_and_metadata_retention) {
  Fixture f;
  int64_t expiry = 0, created = 0;
  {
    ObjectStore objects(f.options);
    StoredChats chats(objects);
    const auto stored = chats.Save(Input(), Output(), Access());
    Q4T_CHECK(Ok(stored));
    const auto record = ReadRecord(objects, "chatcmpl_test");
    Q4T_CHECK(Ok(record));
    expiry = record.value.GetInt("expires_at");
    created = record.value.GetInt("created_at");
    auto updated = chats.Update("chatcmpl_test",
                                Parse(R"({"next":"verified"})"), Access());
    Q4T_CHECK(Ok(updated) &&
              updated.value.Find("metadata")->GetString("next") == "verified");
    const auto after = ReadRecord(objects, "chatcmpl_test");
    Q4T_CHECK(after.value.GetInt("expires_at") == expiry &&
              after.value.GetInt("created_at") == created &&
              after.value.GetInt("revision") == 2);
  }
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  const auto restored = chats.Retrieve("chatcmpl_test", Access());
  Q4T_CHECK(Ok(restored) && restored.value.GetInt("created") == created);
  Q4T_CHECK(restored.value.Find("metadata")->GetString("next") == "verified");
  Q4T_CHECK(Ok(chats.Update("chatcmpl_test", Json(), Access())));
  Q4T_CHECK(chats.Retrieve("chatcmpl_test", Access())
                .value.Find("metadata")
                ->IsNull());
  Q4T_CHECK(ReadRecord(objects, "chatcmpl_test").value.GetInt("expires_at") ==
            expiry);
  const auto deleted = chats.Erase("chatcmpl_test", Access());
  Q4T_CHECK(Ok(deleted) && deleted.value.GetBool("deleted") &&
            deleted.value.GetString("object") == "chat.completion.deleted");
  Q4T_CHECK(chats.Retrieve("chatcmpl_test", Access()).http_status == 404);
  Q4T_CHECK(chats.Messages("chatcmpl_test", {}, Access()).http_status == 404);
  return true;
}

Q4T_TEST(chat_storage_client_isolation_and_duplicate_never_overwrites) {
  Fixture f;
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  Q4T_CHECK(Ok(chats.Save(Input(), Output(), Access())));
  Q4T_CHECK(chats.Save(Input(R"({"project":"changed"})"), Output(), Access())
                .http_status == 409);
  Q4T_CHECK(chats.Retrieve("chatcmpl_test", Access())
                .value.Find("metadata")
                ->GetString("project") == "q4t");
  Q4T_CHECK(chats.Retrieve("chatcmpl_test", Access("client:b")).http_status ==
            404);
  Q4T_CHECK(
      chats.Update("chatcmpl_test", Json(), Access("client:b")).http_status ==
      404);
  Q4T_CHECK(
      chats.Messages("chatcmpl_test", {}, Access("client:b")).http_status ==
      404);
  Q4T_CHECK(chats.Erase("chatcmpl_test", Access("client:b")).http_status ==
            404);
  const auto empty = chats.List({}, Access("client:b"));
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty() &&
            empty.value.Find("first_id")->IsNull());
  ChatListOptions cursor;
  cursor.after = "chatcmpl_test";
  Q4T_CHECK(chats.List(cursor, Access("client:b")).http_status == 400);
  Q4T_CHECK(Ok(chats.Save(Input(), Output(), Access("client:b"))));
  Q4T_CHECK(chats.Retrieve("chatcmpl_test", Access("client:b"))
                .value.GetString("id") == "chatcmpl_test");
  return true;
}

Q4T_TEST(chat_storage_filtered_pagination_crosses_nonmatching_pages) {
  Fixture f;
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  for (int i = 0; i < 103; ++i) {
    auto response = Output("chatcmpl_" + std::to_string(i), 1000 - i);
    Q4T_CHECK(Ok(chats.Save(
        Input(i >= 100 ? R"({"project":"match"})" : R"({"project":"skip"})"),
        response, Access())));
  }
  ChatListOptions options;
  options.limit = 2;
  options.metadata = Parse(R"({"project":"match"})");
  options.model = "q4t";
  auto first = chats.List(options, Access());
  Q4T_CHECK(Ok(first) && first.value.GetArray("data")->array.size() == 2 &&
            first.value.GetBool("has_more"));
  Q4T_CHECK(first.value.GetString("first_id") == "chatcmpl_100" &&
            first.value.GetString("last_id") == "chatcmpl_101");
  options.after = first.value.GetString("last_id");
  auto last = chats.List(options, Access());
  Q4T_CHECK(Ok(last) && last.value.GetArray("data")->array.size() == 1 &&
            !last.value.GetBool("has_more"));
  Q4T_CHECK(last.value.GetString("first_id") == "chatcmpl_102");
  options.after.clear();
  options.descending = true;
  auto reverse = chats.List(options, Access());
  Q4T_CHECK(Ok(reverse) &&
            reverse.value.GetString("first_id") == "chatcmpl_102");
  options.model = "missing";
  auto empty = chats.List(options, Access());
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty() &&
            !empty.value.GetBool("has_more") &&
            empty.value.Find("last_id")->IsNull());
  return true;
}

Q4T_TEST(chat_storage_original_messages_preserve_parts_and_tool_history) {
  Fixture f;
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  auto input = Input();
  Set(&input, "messages",
      Parse(
          R"([{"role":"developer","content":"read"},{"role":"user","content":[{"type":"text","text":"first"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AA==","detail":"high"}},{"type":"text","text":"second"}]},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"read","arguments":"{\"n\":9007199254740993}"}}]},{"role":"tool","tool_call_id":"call_1","content":"result"}])"));
  Q4T_CHECK(Ok(chats.Save(input, Output(), Access())));
  ChatListOptions options;
  options.limit = 2;
  auto first = chats.Messages("chatcmpl_test", options, Access());
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more"));
  const auto& rows = first.value.GetArray("data")->array;
  Q4T_CHECK(rows[0].GetString("role") == "developer");
  Q4T_CHECK(rows[1].GetString("content") == "firstsecond");
  Q4T_CHECK(rows[1]
                .GetArray("content_parts")
                ->array[1]
                .Find("image_url")
                ->GetString("detail") == "high");
  options.after = first.value.GetString("last_id");
  auto last = chats.Messages("chatcmpl_test", options, Access());
  Q4T_CHECK(Ok(last) && !last.value.GetBool("has_more"));
  Q4T_CHECK(last.value.GetArray("data")
                ->array[0]
                .GetArray("tool_calls")
                ->array[0]
                .GetString("id") == "call_1");
  Q4T_CHECK(last.value.GetArray("data")->array[1].GetString("tool_call_id") ==
            "call_1");
  Q4T_CHECK(Ok(chats.Save(input, Output("chatcmpl_other"), Access())));
  Q4T_CHECK(chats.Messages("chatcmpl_other", options, Access()).http_status ==
            400);
  Q4T_CHECK(Ok(chats.Update("chatcmpl_test", Json(), Access())));
  Q4T_CHECK(
      OutputJson(chats.Messages("chatcmpl_test", options, Access()).value) ==
      OutputJson(last.value));
  return true;
}

Q4T_TEST(chat_storage_incomplete_or_quota_refusal_never_publishes) {
  Fixture f;
  f.options.max_client_objects = 1;
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  auto incomplete = Output();
  auto choices = *incomplete.GetArray("choices");
  Set(&choices.array[0], "finish_reason", Json());
  Set(&incomplete, "choices", choices);
  Q4T_CHECK(chats.Save(Input(), incomplete, Access()).http_status == 400);
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(Ok(chats.Save(Input(), Output(), Access())));
  const auto excess = chats.Save(Input(), Output("chatcmpl_2"), Access());
  Q4T_CHECK(excess.http_status == 429 && !excess.commit_unknown);
  Q4T_CHECK(chats.Retrieve("chatcmpl_2", Access()).http_status == 404);
  Q4T_CHECK(chats.List({}, Access()).value.GetArray("data")->array.size() == 1);
  return true;
}

Q4T_TEST(chat_storage_lost_final_receipt_is_retrievable_without_replay) {
  Fixture f;
  f.options.io_timeout_ms = 200;
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  ScopeExit clear([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  const auto result = chats.Save(Input(), Output(), Access());
  Q4T_CHECK(!result.ok() && result.commit_unknown);
  auto restored = chats.Retrieve("chatcmpl_test", Access());
  Q4T_CHECK(Ok(restored) && restored.value.GetString("id") == "chatcmpl_test");
  Q4T_CHECK(ReadRecord(objects, "chatcmpl_test").value.GetInt("revision") == 1);
  Q4T_CHECK(chats.List({}, Access()).value.GetArray("data")->array.size() == 1);
  return true;
}

Q4T_TEST(chat_storage_actual_creation_order_and_absolute_expiry) {
  Fixture f;
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  Q4T_CHECK(Ok(chats.Save(Input(), Output("chatcmpl_new", 1), Access())));
  Q4T_CHECK(Ok(chats.Save(Input(), Output("chatcmpl_old", 1000), Access())));
  Q4T_CHECK(chats.List({}, Access()).value.GetString("first_id") ==
            "chatcmpl_old");
  auto record = ReadRecord(objects, "chatcmpl_old");
  ObjectRequest change;
  change.kind = ObjectKind::kChat;
  change.scope = "client:a";
  change.id = "chatcmpl_old";
  change.operation = ObjectOperation::kPut;
  change.expected_revision = 1;
  change.data = *record.value.Find("data");
  change.created_at = std::time(nullptr);
  Q4T_CHECK(objects.Execute(change, Access().deadline).http_status == 400);
  change.created_at = 0;
  change.expires_at = std::time(nullptr) - 1;
  Q4T_CHECK(objects.Execute(change, Access().deadline).http_status == 404);
  Q4T_CHECK(ReadRecord(objects, "chatcmpl_old").value.GetInt("revision") == 1);
  change.expires_at = std::time(nullptr) + 100;
  const auto written = objects.Execute(change, Access().deadline);
  Q4T_CHECK(Ok(written));
  const auto expiry = written.value.GetInt("expires_at");
  Q4T_CHECK(Ok(chats.Update("chatcmpl_old", Json(), Access())));
  Q4T_CHECK(ReadRecord(objects, "chatcmpl_old").value.GetInt("expires_at") ==
            expiry);
  return true;
}

Q4T_TEST(chat_storage_api_contract_rejects_before_io) {
  Fixture f;
  ObjectStore objects(f.options);
  struct Case {
    const char* method;
    const char* path;
    const char* body;
    int status;
  };
  const Case cases[] = {
      {"GET", "/v1/chat/completions?limit=0", "", 400},
      {"GET", "/v1/chat/completions?order=up", "", 400},
      {"GET", "/v1/chat/completions?metadata[a]=x&metadata%5Ba%5D=y", "", 400},
      {"GET", "/v1/chat/completions?user=client-b", "", 400},
      {"GET", "/v1/chat/completions/chat_1?limit=1", "", 400},
      {"GET", "/v1/chat/completions/chat_1/messages?model=x", "", 400},
      {"GET", "/v1/chat/completions/chat_1/messages?after=%00", "", 400},
      {"GET", "/v1/chat/completions/../messages", "", 400},
      {"GET", "/v1/chat/completions/chat_1/other", "", 404},
      {"DELETE", "/v1/chat/completions/chat_1/messages", "", 405},
      {"POST", "/v1/chat/completions/chat_1", "{}", 400},
      {"POST", "/v1/chat/completions/chat_1", R"({"metadata":{},"model":"x"})",
       400},
      {"POST", "/v1/chat/completions/chat_1", R"({"metadata":{"x":1}})", 400},
      {"POST", "/v1/chat/completions/chat_1",
       R"({"metadata":{},"metadata":null})", 400},
      {"GET", "/v1/chat/completions", "{}", 400},
  };
  for (const auto& test : cases) {
    const auto result = HandleStoredChatRequest(objects, test.method, test.path,
                                                test.body, Access());
    if (result.http_status != test.status)
      std::fprintf(stderr, "%s -> %d %s\n", test.path, result.http_status,
                   result.message.c_str());
    Q4T_CHECK(result.http_status == test.status);
  }
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(StoredChatMethodAllowed("GET", "/v1/chat/completions?limit=1"));
  Q4T_CHECK(StoredChatMethodAllowed("POST", "/v1/chat/completions/chat_1"));
  Q4T_CHECK(!StoredChatMethodAllowed("DELETE",
                                     "/v1/chat/completions/chat_1/messages"));
  return true;
}

Q4T_TEST(chat_storage_api_query_filters_update_and_delete) {
  Fixture f;
  ObjectStore objects(f.options);
  StoredChats chats(objects);
  Q4T_CHECK(Ok(chats.Save(Input(R"({"topic":"a b"})"), Output(), Access())));
  const auto page = HandleStoredChatRequest(
      objects, "GET",
      "/v1/chat/completions?metadata%5Btopic%5D=a+b&model=q4t&limit=1", "",
      Access());
  Q4T_CHECK(Ok(page) && page.value.GetArray("data")->array.size() == 1 &&
            !page.value.GetBool("has_more"));
  const auto update = HandleStoredChatRequest(
      objects, "POST", "/v1/chat/completions/chatcmpl_test",
      R"({"metadata":null})", Access());
  Q4T_CHECK(Ok(update) && update.value.Find("metadata")->IsNull());
  const auto messages = HandleStoredChatRequest(
      objects, "GET", "/v1/chat/completions/chatcmpl_test/messages?order=desc",
      "", Access());
  Q4T_CHECK(Ok(messages) && messages.value.GetArray("data")->array.size() == 1);
  const auto deleted = HandleStoredChatRequest(
      objects, "DELETE", "/v1/chat/completions/chatcmpl_test", "", Access());
  Q4T_CHECK(Ok(deleted) && deleted.value.GetBool("deleted"));
  return true;
}

Q4T_TEST(chat_storage_query_utf8_and_nullable_metadata) {
  Fixture f;
  ObjectStore objects(f.options);
  for (const auto* suffix : {"metadata=object", "metadata=&metadata[a]=b",
                             "metadata[a]=b&metadata=", "metadata[a]=%FF",
                             "metadata[%C0%AF]=x", "metadata[a]=%ED%A0%80"}) {
    const auto reply = HandleStoredChatRequest(
        objects, "GET", std::string("/v1/chat/completions?") + suffix, "",
        Access());
    Q4T_CHECK(reply.http_status == 400);
  }
  Q4T_CHECK(objects.Health().workers_started == 0);
  auto query = ParseApplicationQuery("metadata%5Btopic%5D=%E8%B5%84%E6%96%99");
  Q4T_CHECK(Ok(query) && query.value.GetString("metadata[topic]") == "资料");
  Q4T_CHECK(Ok(StoredChats(objects).Save(Input(), Output(), Access())));
  const auto page = HandleStoredChatRequest(
      objects, "GET", "/v1/chat/completions?metadata=", "", Access());
  Q4T_CHECK(Ok(page) && page.value.GetArray("data")->array.size() == 1);
  const auto hidden = HandleStoredChatRequest(
      objects, "GET", "/v1/chat/completions?metadata=&model=missing", "",
      Access());
  Q4T_CHECK(Ok(hidden) && hidden.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(chat_storage_generation_contract_accepts_only_real_options) {
  auto input = Input();
  std::string param;
  Q4T_CHECK(ValidateChatContract(input, "q4t", false, &param));
  Set(&input, "store", Parse("null"));
  Q4T_CHECK(ValidateChatContract(input, "q4t", false, &param));
  Set(&input, "store", String("true"));
  Q4T_CHECK(!ValidateChatContract(input, "q4t", false, &param) &&
            param == "store");
  Set(&input, "store", Parse("true"));
  Set(&input, "metadata", Parse(R"({"key":42})"));
  Q4T_CHECK(!ValidateChatContract(input, "q4t", false, &param) &&
            param == "metadata.key");
  Set(&input, "metadata", Json());
  auto messages = *input.GetArray("messages");
  messages.array.resize(4097, messages.array.front());
  Set(&input, "messages", messages);
  Q4T_CHECK(!ValidateChatContract(input, "q4t", false, &param) &&
            param == "messages");
  return true;
}

Q4T_TEST(chat_storage_stream_collection_keeps_choices_and_token_probabilities) {
  Fixture f;
  ObjectStore objects(f.options);
  auto input = Input();
  Set(&input, "stream", Parse("true"));
  Set(&input, "n", Parse("2"));
  Set(&input, "enable_thinking", Parse("false"));
  ChatChoices choices(2, false);
  for (int i = 0; i < 2; ++i) {
    ChatResponseParser parser;
    Q4T_CHECK(parser.Init(input, "<think>\n\n</think>\n\n", "chat_test", true));
    TokenLogprobs probabilities(true);
    const std::string text = "资料✅";
    SampledToken token{7, -0.25, {{7, -0.25}}};
    Q4T_CHECK(probabilities.Push(token, text, [&](int32_t, std::string* value) {
      *value = text;
      return q4t::Status();
    }));
    detail::Utf8Stream utf8;
    std::vector<detail::Utf8Span> spans;
    const auto normalized = utf8.Append(text, &spans);
    probabilities.Map(spans);
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Append(normalized, &deltas));
    std::string sent;
    for (const auto& delta : deltas) {
      std::string probability;
      Q4T_CHECK(probabilities.Emit(delta, &probability));
      if (delta.kind == ResponseDelta::Kind::kContent) sent += delta.text;
    }
    deltas.clear();
    Q4T_CHECK(parser.Finish(false, &deltas));
    Q4T_CHECK(sent == text);
    Q4T_CHECK(choices.Add(i, parser.MessageJson(), "stop", probabilities.Json(),
                          10, 1, 0, false, -1, 0, 0));
  }
  std::string response;
  Q4T_CHECK(
      choices.Json("chatcmpl_stream", "q4t", std::time(nullptr), &response));
  const auto final = FinalizeStoredChat(&objects, input, response, Access());
  Q4T_CHECK(Ok(final));
  const auto restored =
      StoredChats(objects).Retrieve("chatcmpl_stream", Access());
  Q4T_CHECK(Ok(restored) &&
            OutputJson(restored.value) == OutputJson(final.value));
  Q4T_CHECK(restored.value.GetArray("choices")->array.size() == 2);
  for (const auto& choice : restored.value.GetArray("choices")->array) {
    Q4T_CHECK(choice.Find("message")->GetString("content") == "资料✅");
    const auto& probability =
        choice.Find("logprobs")->GetArray("content")->array.front();
    Q4T_CHECK(probability.GetNumber("logprob") == -0.25 &&
              probability.GetArray("bytes")->array.size() == 9);
  }
  Q4T_CHECK(restored.value.Find("usage")->GetInt("completion_tokens") == 2);
  return true;
}

Q4T_TEST(chat_storage_cancel_before_commit_never_creates_success) {
  Fixture f;
  ObjectStore objects(f.options);
  auto access = Access();
  access.cancelled = [] { return true; };
  const auto final =
      FinalizeStoredChat(&objects, Input(), OutputJson(Output()), access);
  Q4T_CHECK(final.http_status == 499 && !final.commit_unknown);
  Q4T_CHECK(
      StoredChats(objects).Retrieve("chatcmpl_test", Access()).http_status ==
      404);
  return true;
}
