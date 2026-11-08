#include "q4t/server/conversation_api.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>

#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/request_json.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;
StateAccess Access() { return {"cpu-client-a", Clock::now() + 5s, {}}; }
struct Invalid {
  const char* method;
  const char* target;
  const char* body;
  int status;
};

// Only this CPU fixture synthesizes an output. The actual shared generation
// path is verified separately with the local model under the GPU lease.
ObjectReply FixtureChat(ObjectStore& objects, const std::string& body,
                        const StateAccess& access, bool* stream) {
  q4t::io::Json request;
  std::string param;
  auto status = q4t::io::ParseJson(body, &request, true, kSchemaJsonLimits);
  if (status) status = ValidateChatContract(request, "q4t", true, &param);
  if (!status) {
    ObjectReply reply;
    reply.http_status = 400;
    reply.code = "invalid_request";
    reply.message = status.message();
    reply.value.type = q4t::io::Json::Type::kObject;
    q4t::io::Json field;
    field.type = q4t::io::Json::Type::kString;
    field.str = param;
    reply.value.object.emplace_back("param", std::move(field));
    return reply;
  }
  *stream = request.GetBool("stream", false);
  const auto n = static_cast<int>(request.GetInt("n", 1));
  ChatChoices choices(n, false);
  for (int i = 0; i < n; ++i) {
    status =
        choices.Add(i, R"({"role":"assistant","content":"CPU fixture 42"})",
                    "stop", "null", 10, 3, 0, false, -1, 0, 0);
    if (!status) throw std::runtime_error(status.message());
  }
  std::string output;
  status = choices.Json(detail::NewRequestId("chatcmpl_"), "q4t",
                        std::time(nullptr), &output);
  if (!status) throw std::runtime_error(status.message());
  return FinalizeStoredChat(&objects, request, output, access);
}
}  // namespace

Q4T_TEST(conversation_api_rejects_invalid_wire_contract_before_storage) {
  ObjectStoreOptions options;
  options.directory = "/unconfigured/not-opened";
  ObjectStore objects(options);
  const Invalid cases[] = {
      {"GET", "/v1/conversations", "", 405},
      {"PUT", "/v1/conversations/conv_1", "{}", 405},
      {"POST", "/v1/conversations/conv_1/items/item_1", "{}", 405},
      {"GET", "/v1/conversations/../item", "", 400},
      {"GET", "/v1/conversations/conv_1/other", "", 404},
      {"POST", "/v1/conversations", "{", 400},
      {"POST", "/v1/conversations", R"({"items":3})", 400},
      {"POST", "/v1/conversations", R"({"metadata":{"a":1}})", 400},
      {"POST", "/v1/conversations", R"({"items":[],"items":[]})", 400},
      {"POST", "/v1/conversations", R"({"user":"foreign"})", 400},
      {"POST", "/v1/conversations/conv_1", "{}", 400},
      {"GET", "/v1/conversations/conv_1", "{}", 400},
      {"DELETE", "/v1/conversations/conv_1?x=1", "", 400},
      {"GET", "/v1/conversations/conv_1/items?limit=0", "", 400},
      {"GET", "/v1/conversations/conv_1/items?limit=1.0", "", 400},
      {"GET", "/v1/conversations/conv_1/items?limit=1&limit=2", "", 400},
      {"GET", "/v1/conversations/conv_1/items?after=%ZZ", "", 400},
      {"GET", "/v1/conversations/conv_1/items?after=%00", "", 400},
      {"GET", "/v1/conversations/conv_1/items?limit=2&", "", 400},
      {"GET", "/v1/conversations/conv_1/items?include[]=wrong", "", 400},
      {"GET", "/v1/conversations/conv_1/items/item_1?limit=2", "", 400},
  };
  for (const auto& input : cases) {
    auto result = HandleConversationRequest(objects, input.method, input.target,
                                            input.body, Access());
    if (result.http_status != input.status)
      std::fprintf(stderr, "%s %s: %d %s\n", input.method, input.target,
                   result.http_status, result.message.c_str());
    Q4T_CHECK(result.http_status == input.status);
  }
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(!IsConversationTarget("/v1/conversations-extra"));
  Q4T_CHECK(IsConversationTarget("/v1/conversations/conv_1/items?limit=2"));
  return true;
}

// Test-only real HTTP transport around the production handler and storage.
// The fixed synthetic bearer->scope mapping is not production authentication.
int ConversationApiFixture(int argc, char** argv) {
  const bool chat =
      argc == 3 && std::string_view(argv[1]) == "--chat-storage-api-fixture";
  if (argc != 3 ||
      (!chat && std::string_view(argv[1]) != "--conversation-api-fixture"))
    return -1;
  try {
    const auto directory = fs::absolute(argv[2]);
    fs::create_directories(directory);
    ObjectStoreOptions options;
    options.directory = (directory / "objects").string();
    ObjectStore objects(options);
    const int listener = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0) return 1;
    ScopeExit close_listener([&] { close(listener); });
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(listener, reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) ||
        listen(listener, 8))
      return 1;
    socklen_t size = sizeof(address);
    if (getsockname(listener, reinterpret_cast<sockaddr*>(&address), &size))
      return 1;
    {
      std::ofstream ready(directory / "ready.json");
      ready << "{\"port\":" << ntohs(address.sin_port)
            << ",\"pid\":" << getpid() << "}\n";
    }
    std::ofstream trace(directory / "requests.jsonl", std::ios::app);
    HttpBodyBudget budget(kAggregateChatBodyBytes);
    for (int count = 0; count < 100; ++count) {
      pollfd pending{listener, POLLIN, 0};
      if (poll(&pending, 1, 15000) != 1) return 2;
      const int fd = accept4(listener, nullptr, nullptr, SOCK_CLOEXEC);
      if (fd < 0) return 1;
      ScopeExit close_peer([&] { close(fd); });
      detail::HttpResponseContext context(fd);
      std::string method, target, body;
      HttpRequestHeaders headers;
      HttpBodyLease lease(budget);
      int failure = 400;
      if (!ReadHttpRequest(fd, &method, &target, &body, 5000, &lease, &failure,
                           &headers)) {
        detail::SendError(fd, failure, "Fixture transport rejected request");
        continue;
      }
      const auto scope =
          headers.authorization == "Bearer cpu-client-a"   ? "cpu-client-a"
          : headers.authorization == "Bearer cpu-client-b" ? "cpu-client-b"
                                                           : "";
      if (std::string_view(scope).empty()) {
        detail::SendError(fd, 401, "Fixture credential missing");
        continue;
      }
      if (method == "POST" && target == "/__fixture/stop") {
        detail::SendSimple(fd, 200, "OK", "{}", "application/json");
        return 0;
      }
      const StateAccess access{scope, Clock::now() + 5s, {}};
      bool stream = false;
      auto result = chat ? (method == "POST" && target == "/v1/chat/completions"
                                ? FixtureChat(objects, body, access, &stream)
                                : HandleStoredChatRequest(objects, method,
                                                          target, body, access))
                         : HandleConversationRequest(objects, method, target,
                                                     body, access);
      const auto output =
          result.ok()
              ? OutputJson(result.value)
              : detail::ErrorJson(result.http_status, result.ClientMessage(),
                                  result.ClientCode(),
                                  result.value.GetString("param"));
      q4t::io::Json record;
      record.type = q4t::io::Json::Type::kObject;
      const auto string = [](std::string text) {
        q4t::io::Json value;
        value.type = q4t::io::Json::Type::kString;
        value.str = std::move(text);
        return value;
      };
      record.object = {{"method", string(method)},
                       {"target", string(target)},
                       {"scope", string(scope)},
                       {"body", string(body)},
                       {"response", string(output)}};
      trace << OutputJson(record) << '\n';
      trace.flush();
      if (stream && result.ok()) {
        const auto& value = result.value;
        if (!detail::BeginStreamingResponse(fd, context.State())) return 3;
        for (const auto& choice : value.GetArray("choices")->array) {
          const auto index = static_cast<int>(choice.GetInt("index"));
          const auto id = value.GetString("id");
          const auto created = value.GetInt("created");
          if (!detail::WriteAll(
                  fd, detail::SseChunk(id, "q4t", "assistant", "CPU fixture 42",
                                       "", index, created)) ||
              !detail::WriteAll(fd, detail::SseChunk(id, "q4t", "", "", "stop",
                                                     index, created)))
            return 3;
        }
        if (!detail::WriteAll(fd, "data: [DONE]\n\n")) return 3;
        continue;
      }
      detail::SendSimple(fd, result.http_status, result.ok() ? "OK" : "Error",
                         output, "application/json", {},
                         result.value.GetString("allow"));
    }
  } catch (const std::exception& error) {
    std::fprintf(stderr, "Conversation fixture: %s\n", error.what());
    return 1;
  }
  return 2;
}
