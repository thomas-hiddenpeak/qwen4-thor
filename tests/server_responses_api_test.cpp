#include "q4t/server/responses_api.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;
Json Parse(const std::string& text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("Response API fixture JSON");
  return result;
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
StateAccess Access() { return {"client:cpu-a", Clock::now() + 5s, {}}; }
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root = fs::absolute(output ? output : ".q4t-work/response-api");
    fs::create_directories(root);
    auto name = (root / "api-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("Response API fixture");
    options.directory = name + "/objects";
  }
};
bool Begin(ResponsesExecution* run, std::string* wire,
           const std::string& extra = "") {
  ChatPlanConfig config;
  config.model = "cpu";
  return Ok(run->Prepare(
             Parse(
                 R"({"input":"first question","reasoning":{"effort":"none"},)"
                 R"("stream":true,"stream_options":{"include_obfuscation":false})" +
                 extra + "}"),
             config, Access(), 42)) &&
         Ok(run->Begin(wire));
}
bool Finish(ResponsesExecution* run, std::string* wire) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "actual response";
  std::string part;
  if (!run->Append(delta, "null", &part)) return false;
  *wire += part;
  if (!Ok(run->Finish(false, 0, {5, 0, 2, 0}, 43, &part))) return false;
  *wire += part;
  return true;
}

Q4T_TEST(responses_api_cancel_route_and_owner_boundary) {
  ResponsesReadPlan plan;
  const auto path = "/v1/responses/resp_1/cancel";
  Q4T_CHECK(Ok(PlanResponsesRead("POST", path, "", &plan)));
  Q4T_CHECK(plan.operation == ResponsesReadPlan::Operation::kCancel);
  Q4T_CHECK(ResponsesMethodAllowed("POST", path));
  Q4T_CHECK(!ResponsesMethodAllowed("GET", path));
  Q4T_CHECK(PlanResponsesRead("GET", path, "", &plan).http_status == 405);
  Q4T_CHECK(PlanResponsesRead("POST", path, "{}", &plan).http_status == 400);
  Q4T_CHECK(PlanResponsesRead("POST", std::string(path) + "?x=1", "", &plan)
                .http_status == 400);
  Q4T_CHECK(ClassifyHttpConnection("POST", path) ==
            HttpConnectionClass::kControl);
  Q4T_CHECK(ClassifyHttpConnection("GET", "/v1/responses/resp_1?stream=true") ==
            HttpConnectionClass::kObservation);
  Q4T_CHECK(ClassifyHttpConnection("DELETE", "/v1/responses/resp_1") ==
            HttpConnectionClass::kObservation);
  Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
            HttpConnectionClass::kBusiness);
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesReplay replay;
  Q4T_CHECK(Ok(PlanResponsesRead("POST", path, "", &plan)));
  Q4T_CHECK(ReadResponsesState(objects, plan, Access(), &replay).code ==
            "response_executor_required");
  Q4T_CHECK(!std::filesystem::exists(f.options.directory));
  return true;
}
ObjectReply Read(ObjectStore& objects, std::string method, std::string target,
                 ResponsesReplay* replay = nullptr,
                 StateAccess access = Access()) {
  ResponsesReadPlan plan;
  auto result = PlanResponsesRead(method, target, "", &plan);
  if (!result.ok()) return result;
  ResponsesReplay local;
  return ReadResponsesState(objects, plan, access, replay ? replay : &local);
}
std::string Receive(int fd) {
  std::string wire;
  char bytes[4096];
  for (ssize_t n; (n = read(fd, bytes, sizeof(bytes))) > 0;)
    wire.append(bytes, n);
  return wire;
}
}  // namespace

Q4T_TEST(responses_api_query_contract_and_routes_before_storage) {
  struct Bad {
    const char* method;
    const char* path;
    int code;
  };
  const Bad cases[] = {
      {"GET", "/v1/responses", 405},
      {"POST", "/v1/responses/resp_1", 405},
      {"DELETE", "/v1/responses/resp_1/input_items", 405},
      {"GET", "/v1/responses/..", 400},
      {"GET", "/v1/responses/resp_1/missing", 404},
      {"GET", "/v1/responses-extra", 404},
      {"GET", "/v1/responses/resp_1?stream=1", 400},
      {"GET", "/v1/responses/resp_1?stream=false&stream=true", 400},
      {"GET", "/v1/responses/resp_1?starting_after=3", 400},
      {"GET", "/v1/responses/resp_1?stream=true&starting_after=-1", 400},
      {"GET", "/v1/responses/resp_1?stream=true&starting_after=1.0", 400},
      {"GET",
       "/v1/responses/resp_1?stream=true&starting_after=9007199254740992", 400},
      {"GET", "/v1/responses/resp_1?include_obfuscation=null", 400},
      {"GET", "/v1/responses/resp_1?include[]=invalid", 400},
      {"GET", "/v1/responses/resp_1?include%5B%5D=%00", 400},
      {"GET",
       "/v1/responses/"
       "resp_1?include[]=message.output_text.logprobs&include=message.output_"
       "text.logprobs",
       400},
      {"GET", "/v1/responses/resp_1?limit=2", 400},
      {"GET", "/v1/responses/resp_1/input_items?stream=true", 400},
      {"GET", "/v1/responses/resp_1/input_items?limit=0", 400},
      {"DELETE", "/v1/responses/resp_1?stream=false", 400},
  };
  for (const auto& test : cases) {
    ResponsesReadPlan plan;
    const auto result = PlanResponsesRead(test.method, test.path, "", &plan);
    if (result.http_status != test.code)
      std::fprintf(stderr, "%s: %d %s\n", test.path, result.http_status,
                   result.message.c_str());
    Q4T_CHECK(result.http_status == test.code);
  }
  Q4T_CHECK(ResponsesMethodAllowed("POST", "/v1/responses"));
  Q4T_CHECK(ResponsesMethodAllowed("GET", "/v1/responses/resp_1?stream=true"));
  Q4T_CHECK(ResponsesMethodAllowed("GET", "/v1/responses/resp_1/input_items"));
  Q4T_CHECK(
      !ResponsesMethodAllowed("POST", "/v1/responses/resp_1/input_items"));
  Q4T_CHECK(!ResponsesMethodAllowed("GET", "/v1/responses/../input_items"));
  ResponsesReadPlan plan;
  Q4T_CHECK(PlanResponsesRead("GET", "/v1/responses/resp_1", "{}", &plan)
                .http_status == 400);
  return true;
}

Q4T_TEST(responses_api_body_capacity_is_charged_before_reading) {
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  const std::string header =
      "POST /v1/responses HTTP/1.1\r\nHost: local\r\n"
      "Content-Length: 8192\r\n\r\n";
  Q4T_CHECK(send(fd[0], header.data(), header.size(), MSG_NOSIGNAL) > 0);
  HttpBodyBudget budget(8192);
  HttpBodyLease occupied(budget), lease(budget);
  Q4T_CHECK(occupied.Acquire(1));
  HttpRequestHeaders headers;
  std::string method, target, body;
  int status = 0;
  const bool ok = ReadHttpRequest(fd[1], &method, &target, &body, 500, &lease,
                                  &status, &headers);
  close(fd[0]);
  close(fd[1]);
  Q4T_CHECK(!ok && status == 503 && budget.Used() == 1);
  Q4T_CHECK(headers.failure == HttpReceiveFailure::kBodyCapacity);
  Q4T_CHECK(ClassifyHttpConnection("POST", target) ==
            HttpConnectionClass::kBusiness);
  return true;
}

Q4T_TEST(responses_api_history_pagination_and_scope_survive_worker_restart) {
  Fixture fixture;
  std::string id;
  {
    ObjectStore store(fixture.options);
    ResponsesExecution first(store);
    std::string wire;
    Q4T_CHECK(Begin(&first, &wire) && Finish(&first, &wire));
    ResponsesExecution second(store);
    Q4T_CHECK(Begin(&second, &wire,
                    ",\"previous_response_id\":\"" + first.Id() + '"'));
    Q4T_CHECK(Finish(&second, &wire));
    id = second.Id();
  }
  ObjectStore reopened(fixture.options);
  const auto root = "/v1/responses/" + id;
  const auto restored = Read(reopened, "GET", root);
  Q4T_CHECK(Ok(restored) && restored.value.GetString("id") == id);
  auto page = Read(reopened, "GET", root + "/input_items?order=asc&limit=1");
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  Q4T_CHECK(page.value.GetArray("data")->array[0].GetString("role") == "user");
  auto next = Read(reopened, "GET",
                   root + "/input_items?order=asc&limit=1&after=" +
                       page.value.GetString("last_id"));
  Q4T_CHECK(Ok(next) && next.value.GetArray("data")->array[0].GetString(
                            "role") == "assistant");
  const auto last = Read(reopened, "GET",
                         root + "/input_items?order=asc&limit=1&after=" +
                             next.value.GetString("last_id"));
  Q4T_CHECK(Ok(last) && !last.value.GetBool("has_more"));
  auto stranger = Access();
  stranger.scope = "client:cpu-b";
  Q4T_CHECK(Read(reopened, "GET", root, nullptr, stranger).http_status == 404);
  Q4T_CHECK(Read(reopened, "DELETE", root, nullptr, stranger).http_status ==
            404);
  Q4T_CHECK(Read(reopened, "GET", root + "/input_items", nullptr, stranger)
                .http_status == 404);
  Q4T_CHECK(
      Read(reopened, "GET", root + "/input_items?after=unknown").http_status ==
      400);
  const auto deleted = Read(reopened, "DELETE", root);
  Q4T_CHECK(Ok(deleted) &&
            deleted.value.GetString("object") == "response.deleted");
  Q4T_CHECK(Read(reopened, "GET", root).http_status == 404);
  return true;
}

Q4T_TEST(responses_api_replay_uses_original_sequence_and_obfuscation_choice) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  std::string wire;
  Q4T_CHECK(Begin(&run, &wire) && Finish(&run, &wire));
  const auto root = "/v1/responses/" + run.Id();
  ResponsesReplay replay;
  Q4T_CHECK(Ok(Read(store, "GET",
                    root + "?stream=true&include_obfuscation=false&include[]="
                           "message.output_text.logprobs",
                    &replay)));
  Q4T_CHECK(replay.terminal && replay.wire == wire);
  const auto final = replay.last_sequence;
  Q4T_CHECK(final > 3);
  const auto first_end = wire.find("\r\n\r\n") + 4;
  Q4T_CHECK(Ok(Read(store, "GET",
                    root + "?stream=true&include_obfuscation=false&starting_"
                           "after=0&include[]=message.output_text.logprobs",
                    &replay)));
  Q4T_CHECK(replay.wire == wire.substr(first_end));
  Q4T_CHECK(Ok(Read(store, "GET", root + "?stream=true", &replay)));
  Q4T_CHECK(replay.wire.find("\"obfuscation\"") != replay.wire.npos);
  Q4T_CHECK(Ok(Read(
      store, "GET",
      root + "?stream=true&starting_after=" + std::to_string(final), &replay)));
  Q4T_CHECK(replay.terminal && replay.wire.empty());
  Q4T_CHECK(
      Read(store, "GET",
           root + "?stream=true&starting_after=" + std::to_string(final + 1),
           &replay)
          .http_status == 400);
  Q4T_CHECK(replay.wire.find("[DONE]") == replay.wire.npos);
  return true;
}

Q4T_TEST(
    responses_api_include_projects_without_changing_retained_probabilities) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  std::string wire;
  Q4T_CHECK(
      Begin(&run, &wire,
            R"(,"include":["message.output_text.logprobs"],"top_logprobs":1)"));
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "x";
  Q4T_CHECK(run.Append(
      delta,
      R"({"content":[{"token":"x","bytes":[120],"logprob":-0.5,"top_logprobs":[{"token":"x","bytes":[120],"logprob":-0.5}]}]})",
      &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {5, 0, 1, 0}, 43, &wire)));
  const auto root = "/v1/responses/" + run.Id();
  auto hidden = Read(store, "GET", root);
  Q4T_CHECK(Ok(hidden) &&
            OutputJson(hidden.value).find("\"logprob\":") == std::string::npos);
  auto visible =
      Read(store, "GET", root + "?include%5B%5D=message.output_text.logprobs");
  Q4T_CHECK(Ok(visible) && OutputJson(visible.value).find("\"logprob\":-0.5") !=
                               std::string::npos);
  Q4T_CHECK(
      OutputJson(ResponsesState(store).Retrieve(run.Id(), Access()).value) ==
      run.Output().Json());
  return true;
}

Q4T_TEST(
    responses_api_subscriber_timeout_never_cancels_generation_or_fakes_terminal) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  std::string wire;
  Q4T_CHECK(Begin(&run, &wire));
  const auto root = "/v1/responses/" + run.Id();
  Q4T_CHECK(Read(store, "DELETE", root).http_status == 409);
  int fd[2];
  Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
  auto access = Access();
  access.deadline = Clock::now() + 40ms;
  {
    detail::HttpResponseContext http(fd[0]);
    ServeResponsesState(fd[0], store, "GET", root + "?stream=true", "", access);
    Q4T_CHECK(!http.ResponsesSequence());
  }
  shutdown(fd[0], SHUT_WR);
  const auto received = Receive(fd[1]);
  close(fd[0]);
  close(fd[1]);
  Q4T_CHECK(received.find("event: error") != received.npos);
  Q4T_CHECK(received.find("response_stream_timeout") != received.npos);
  Q4T_CHECK(received.find("response.failed") == received.npos &&
            received.find("[DONE]") == received.npos);
  Q4T_CHECK(Read(store, "GET", root).value.GetString("status") ==
            "in_progress");
  Q4T_CHECK(Finish(&run, &wire));
  Q4T_CHECK(Read(store, "GET", root).value.GetString("status") == "completed");
  return true;
}

Q4T_TEST(responses_api_missing_or_corrupt_event_journal_is_not_reconstructed) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  std::string wire;
  Q4T_CHECK(Begin(&run, &wire) && Finish(&run, &wire));
  ObjectRequest request;
  request.id = run.Id();
  request.scope = Access().scope;
  auto stored = store.Execute(request, Clock::now() + 5s);
  Q4T_CHECK(Ok(stored));
  request.operation = ObjectOperation::kPut;
  request.expected_revision = stored.value.GetInt("revision");
  request.data = *stored.value.Find("data");
  for (auto& [name, value] : request.data.object)
    if (name == "events") value = Parse("[]");
  Q4T_CHECK(Ok(store.Execute(request, Clock::now() + 5s)));
  const auto root = "/v1/responses/" + run.Id();
  Q4T_CHECK(Read(store, "GET", root + "?stream=true").code ==
            "response_events_unavailable");
  Q4T_CHECK(Read(store, "GET", root).value.GetString("status") == "completed");
  ++request.expected_revision;
  request.data = *stored.value.Find("data");
  for (auto& [name, value] : request.data.object)
    if (name == "events")
      for (auto& [field, content] : value.array.front().object)
        if (field == "sequence_number") content = Parse("9");
  Q4T_CHECK(Ok(store.Execute(request, Clock::now() + 5s)));
  Q4T_CHECK(Read(store, "GET", root + "?stream=true").code ==
            "response_state_invalid");
  return true;
}

// HTTP/SDK fixture: protocol/state are production code; generated tokens are
// synthetic CPU input. This is not model, production auth or GPU acceptance.
int ResponsesApiFixture(int argc, char** argv) {
  if (argc != 3 || std::string_view(argv[1]) != "--responses-api-fixture")
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
    HttpBodyBudget budget(kAggregateChatBodyBytes);
    for (int count = 0; count < 100; ++count) {
      pollfd pending{listener, POLLIN, 0};
      if (poll(&pending, 1, 15000) != 1) return 2;
      const int fd = accept4(listener, nullptr, nullptr, SOCK_CLOEXEC);
      if (fd < 0) return 1;
      ScopeExit close_peer([&] { close(fd); });
      detail::HttpResponseContext http(fd);
      std::string method, target, body;
      HttpRequestHeaders headers;
      HttpBodyLease lease(budget);
      int failure = 400;
      if (!ReadHttpRequest(fd, &method, &target, &body, 5000, &lease, &failure,
                           &headers)) {
        detail::SendError(fd, failure, "Fixture transport rejected request");
        continue;
      }
      if (headers.authorization != "Bearer cpu-a" &&
          headers.authorization != "Bearer cpu-b") {
        detail::SendError(fd, 401, "Fixture authentication failed");
        continue;
      }
      if (target == "/__fixture/stop" && method == "POST") {
        detail::SendSimple(fd, 200, "OK", "{}", "application/json");
        return 0;
      }
      StateAccess access{headers.authorization == "Bearer cpu-a"
                             ? "client:cpu-a"
                             : "client:cpu-b",
                         Clock::now() + 5s,
                         {}};
      if (target != "/v1/responses" || method != "POST") {
        ServeResponsesState(fd, objects, method, target, body, access);
        continue;
      }
      ResponsesExecution run(objects);
      ChatPlanConfig config;
      config.model = "cpu";
      auto result = run.Prepare(Parse(body), config, access, 42);
      std::string wire, delta;
      if (result.ok()) result = run.Begin(&wire);
      if (result.ok()) {
        ResponseDelta content;
        content.kind = ResponseDelta::Kind::kContent;
        content.text = "CPU fixture answer";
        if (!run.Append(content, "null", &delta)) return 1;
        wire += delta;
        result = run.Finish(false, 0, {5, 0, 2, 0}, 43, &delta);
        wire += delta;
      }
      if (!result.ok()) {
        detail::SendError(fd, result.http_status, result.message, {},
                          result.code, result.value.GetString("param"));
        run.CloseUnfinished();
        continue;
      }
      if (run.Envelope().stream) {
        if (detail::BeginStreamingResponse(fd, http.State()))
          detail::WriteAll(fd, wire);
      } else {
        detail::SendSimple(fd, 200, "OK", run.Output().Json(),
                           "application/json");
      }
    }
  } catch (const std::exception& error) {
    std::fprintf(stderr, "Response API fixture: %s\n", error.what());
    return 1;
  }
  return 2;
}
