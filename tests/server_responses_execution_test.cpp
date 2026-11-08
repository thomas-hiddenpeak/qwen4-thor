#include "q4t/server/responses_execution.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <thread>

#include "q4t/server/conversation_state.h"
#include "q4t/server/http_response.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_api.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& raw) {
  Json value;
  if (!q4t::io::ParseJson(raw, &value, true))
    throw std::runtime_error("execution fixture JSON");
  return value;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-execution-cpu");
    std::filesystem::create_directories(root);
    auto name = (root / "response-execution-XXXXXX").string();
    if (!mkdtemp(name.data()))
      throw std::runtime_error("execution fixture path");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access() { return {"client:cpu", Clock::now() + 5s, {}}; }
ChatPlanConfig Config() {
  ChatPlanConfig value;
  value.model = "cpu";
  return value;
}
bool Prepare(ResponsesExecution* run, std::string suffix = "") {
  return Ok(run->Prepare(
      Parse(R"({"input":"question","reasoning":{"effort":"none"})" + suffix +
            "}"),
      Config(), Access(), 42));
}
ResponseDelta Text(std::string text) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = std::move(text);
  return delta;
}
}  // namespace

Q4T_TEST(responses_execution_stateless_does_not_depend_on_storage) {
  ObjectStoreOptions options;
  options.directory = "invalid";
  ObjectStore store(options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run, R"(,"store":false)"));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && wire.find("response.created") != wire.npos);
  Q4T_CHECK(run.Append(Text("actual "), "null", &wire));
  Q4T_CHECK(run.Append(Text("answer"), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {7, 0, 2, 0}, 43, &wire)));
  Q4T_CHECK(wire.find("response.completed") != wire.npos &&
            wire.find("[DONE]") == wire.npos);
  Q4T_CHECK(run.Finished() &&
            run.Output().Snapshot().GetString("status") == "completed");
  Q4T_CHECK(store.Health().workers_started == 0);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_execution_echoes_public_tool_choice) {
  ObjectStoreOptions options;
  options.directory = "invalid";
  ObjectStore store(options);
  const std::string tools = R"(,"tools":[{"type":"function","name":"read",
    "parameters":{"type":"object","properties":{},
      "additionalProperties":false},"strict":false}])";
  for (const auto& selection :
       {std::string(), std::string(R"("auto")"), std::string(R"("none")"),
        std::string(R"("required")"),
        std::string(R"({"type":"function","name":"read"})"),
        std::string(R"({"type":"allowed_tools","mode":"required",
             "tools":[{"type":"function","name":"read"}]})")}) {
    ResponsesExecution run(store);
    auto config = Config();
    config.structured = true;
    const auto suffix =
        selection.empty() ? "" : ",\"tool_choice\":" + selection;
    Q4T_CHECK(Ok(run.Prepare(
        Parse(R"({"input":"question","store":false)" + tools + suffix + "}"),
        config, Access(), 42)));
    const auto* actual = run.Output().Snapshot().Find("tool_choice");
    Q4T_CHECK(actual);
    const auto public_choice = OutputJson(*actual);
    Q4T_CHECK(public_choice ==
              OutputJson(Parse(selection.empty() ? R"("auto")" : selection)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    Q4T_CHECK(wire.find("\"tool_choice\":" + public_choice) != wire.npos);
    Q4T_CHECK(Ok(run.CloseUnfinished()));
  }
  return true;
}

Q4T_TEST(responses_execution_commits_actual_ordered_events_and_usage) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  ResponsesState state(store);
  Q4T_CHECK(state.Retrieve(run.Id(), Access()).value.GetString("status") ==
            "in_progress");
  Q4T_CHECK(run.Append(Text("42"), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {7, 3, 2, 0}, 43, &wire)));
  auto read = state.Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(read) && OutputJson(read.value) == run.Output().Json());
  ObjectRequest request;
  request.kind = ObjectKind::kResponse;
  request.id = run.Id();
  request.scope = Access().scope;
  auto saved = store.Execute(request, Clock::now() + 5s);
  Q4T_CHECK(Ok(saved));
  const auto* events = saved.value.Find("data")->GetArray("events");
  Q4T_CHECK(events && events->array.size() >= 6);
  for (size_t i = 0; i < events->array.size(); ++i)
    Q4T_CHECK(events->array[i].GetInt("sequence_number") ==
              static_cast<int64_t>(i));
  Q4T_CHECK(events->array.back().GetString("type") == "response.completed");
  Q4T_CHECK(read.value.Find("usage")->GetInt("total_tokens") == 9);
  Q4T_CHECK(read.value.Find("usage")
                ->Find("input_tokens_details")
                ->GetInt("cached_tokens") == 3);
  return true;
}

Q4T_TEST(
    responses_execution_conflict_cannot_publish_completed_or_consume_sequence) {
  Fixture f;
  ObjectStore store(f.options);
  ConversationState conversations(store);
  auto conversation = conversations.Create(Parse("{}"), Parse("[]"), Access());
  Q4T_CHECK(Ok(conversation));
  const auto id = conversation.value.GetString("id");
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run, ",\"conversation\":\"" + id + '"'));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(run.Append(Text("answer"), "null", &wire));
  Q4T_CHECK(
      Ok(conversations.Update(id, Parse(R"({"new":"metadata"})"), Access())));
  const auto sequence = *run.Sequence();
  auto failed = run.Finish(false, 0, {7, 0, 1, 0}, 43, &wire);
  Q4T_CHECK(!failed.ok() && failed.http_status == 409 && wire.empty());
  Q4T_CHECK(!run.Output().Terminal() && *run.Sequence() == sequence);
  ResponsesExecution::ObserveError(&run, failed.http_status, failed.message,
                                   failed.code);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  Q4T_CHECK(ResponsesState(store)
                .Retrieve(run.Id(), Access())
                .value.GetString("status") == "failed");
  Q4T_CHECK(conversations.ListItems(id, {}, Access())
                .value.GetArray("data")
                ->array.empty());
  return true;
}

Q4T_TEST(
    responses_execution_truncated_tool_is_stored_incomplete_without_done_arguments) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call = {0, "call_actual", "lookup", "{\"query\":"};
  Q4T_CHECK(run.Append(delta, "null", &wire));
  Q4T_CHECK(Ok(run.Finish(true, 0, {7, 0, 4, 0}, 43, &wire)));
  Q4T_CHECK(wire.find("arguments.done") == wire.npos);
  Q4T_CHECK(wire.find("response.incomplete") != wire.npos);
  const auto response = ResponsesState(store).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(response) && response.value.GetString("status") == "incomplete");
  return true;
}

Q4T_TEST(responses_execution_http_errors_close_after_cancelled_caller_cleanup) {
  Fixture f;
  ObjectStore store(f.options);
  for (const bool stream : {false, true}) {
    ResponsesExecution run(store);
    Q4T_CHECK(Prepare(&run, stream ? R"(,"stream":true)" : ""));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    int sockets[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    {
      detail::HttpResponseContext context(sockets[0]);
      context.SetErrorObserver(&ResponsesExecution::ObserveError, &run);
      context.SetResponsesStream(&ResponsesExecution::RenderStreamError, &run,
                                 run.Sequence());
      if (stream) {
        Q4T_CHECK(detail::BeginStreamingResponse(sockets[0], context.State()));
        Q4T_CHECK(detail::WriteAll(sockets[0], wire));
      }
      detail::SendError(sockets[0], 503, "constraint worker unavailable", {},
                        "constraint_dependency_unavailable");
      run.BindCancellation(Clock::now() - 1s, [] { return true; });
      Q4T_CHECK(Ok(run.CloseUnfinished()));
      context.SetErrorObserver(nullptr, nullptr);
      context.SetResponsesStream(nullptr, nullptr, nullptr);
    }
    shutdown(sockets[0], SHUT_WR);
    std::string received;
    char buffer[4096];
    for (ssize_t count; (count = read(sockets[1], buffer, sizeof(buffer))) > 0;)
      received.append(buffer, count);
    close(sockets[0]);
    close(sockets[1]);
    Q4T_CHECK(received.find("[DONE]") == received.npos);
    if (stream) Q4T_CHECK(received.find("response.failed") != received.npos);
    const auto result = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(result) && result.value.GetString("status") == "failed");
    Q4T_CHECK(result.value.Find("error")->GetString("code") ==
              "constraint_dependency_unavailable");
  }
  return true;
}

Q4T_TEST(responses_execution_lost_commit_receipt_never_overwrites_or_replays) {
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-commit-pause");
  Fixture f;
  f.options.io_timeout_ms = 250;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(run.Append(Text("committed answer"), "null", &wire));
  auto result = run.Finish(false, 0, {7, 0, 2, 0}, 43, &wire);
  Q4T_CHECK(!result.ok() && result.commit_unknown && run.CommitUnknown());
  Q4T_CHECK(wire.empty() &&
            result.message.find(run.Id()) != result.message.npos);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  auto error = ResponsesExecution::RenderStreamError(&run, 503, result.message,
                                                     result.code, "");
  Q4T_CHECK(error.find("event: error") != error.npos &&
            error.find("response.failed") == error.npos);
  const auto end = Clock::now() + 500ms;
  while (store.Health().pending && Clock::now() < end)
    std::this_thread::sleep_for(1ms);
  auto actual = ResponsesState(store).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(actual) && actual.value.GetString("status") == "completed");
  return true;
}

Q4T_TEST(responses_checkpoint_failure_delivers_original_prefix_before_error) {
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  Fixture f;
  f.options.io_timeout_ms = 100;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  const auto next = *run.Sequence();
  const std::string generated(65536, 'x');
  Q4T_CHECK(!run.Append(Text(generated), "null", &wire));
  Q4T_CHECK(run.CommitUnknown());
  const auto error = ResponsesExecution::RenderStreamError(
      &run, 503, "missing checkpoint receipt", "object_transport_error", "");
  size_t offset = 0, events = 0;
  bool text = false, terminal = false;
  while ((offset = error.find("data: ", offset)) != std::string::npos) {
    const auto end = error.find("\r\n", offset);
    const auto event = Parse(error.substr(offset + 6, end - offset - 6));
    Q4T_CHECK(!terminal && event.GetInt("sequence_number") ==
                               next + static_cast<int64_t>(events++));
    if (event.GetString("type") == "response.output_text.delta")
      text = event.GetString("delta") == generated;
    if (event.GetString("type") == "error") {
      terminal = true;
      Q4T_CHECK(event.GetString("code") == "object_commit_unknown");
    }
    offset = end;
  }
  Q4T_CHECK(text && terminal && events == 4);
  Q4T_CHECK(error.find("response.completed") == error.npos &&
            error.find("response.failed") == error.npos);
  const auto repeated = ResponsesExecution::RenderStreamError(
      &run, 503, "missing checkpoint receipt", "object_transport_error", "");
  Q4T_CHECK(repeated.find("response.output_text.delta") == repeated.npos);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_execution_active_checkpoint_preserves_replay_prefix) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  ResponsesState state(store);
  auto initial = state.ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(initial));
  const auto* first = initial.value.GetArray("events");
  Q4T_CHECK(first && first->array.size() == 2);
  Q4T_CHECK(first->array[0].GetString("type") == "response.created");
  Q4T_CHECK(first->array[1].GetString("type") == "response.in_progress");
  Q4T_CHECK(initial.value.GetString("terminal_reserve").size() == 4096);
  Q4T_CHECK(run.Append(Text("partial answer"), "null", &wire));
  Q4T_CHECK(Ok(run.Checkpoint()));
  ResponsesReadPlan plan;
  plan.id = run.Id();
  plan.stream = true;
  plan.obfuscation = false;
  plan.starting_after = 1;
  ResponsesReplay partial;
  auto result = ReadResponsesState(store, plan, Access(), &partial);
  Q4T_CHECK(Ok(result) && !partial.terminal && partial.last_sequence > 1);
  Q4T_CHECK(partial.wire.find("partial answer") != partial.wire.npos);
  Q4T_CHECK(partial.wire.find("response.completed") == partial.wire.npos);
  auto checkpoint = state.ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(checkpoint));
  const auto prefix = *checkpoint.value.GetArray("events");
  Q4T_CHECK(run.Append(Text(" finished"), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {7, 0, 2, 0}, 43, &wire)));
  auto terminal = state.ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(terminal) && !terminal.value.Has("terminal_reserve"));
  const auto& events = terminal.value.GetArray("events")->array;
  for (size_t i = 0; i < prefix.array.size(); ++i)
    Q4T_CHECK(OutputJson(events[i]) == OutputJson(prefix.array[i]));
  plan.starting_after = partial.last_sequence;
  ResponsesReplay tail;
  Q4T_CHECK(Ok(ReadResponsesState(store, plan, Access(), &tail)));
  Q4T_CHECK(tail.terminal && tail.wire.find("response.completed") != wire.npos);
  Q4T_CHECK(tail.wire.find("partial answer\"") == tail.wire.npos);
  return true;
}

Q4T_TEST(responses_execution_size_checkpoint_is_visible_before_terminal) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(run.Append(Text(std::string(70000, 'x')), "null", &wire));
  const auto saved = ResponsesState(store).ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(saved));
  Q4T_CHECK(saved.value.Find("response")->GetString("status") == "in_progress");
  Q4T_CHECK(saved.value.GetArray("events")->array.size() > 2);
  const auto& item = saved.value.Find("response")->GetArray("output")->array[0];
  Q4T_CHECK(item.GetArray("content")->array[0].GetString("text").size() ==
            70000);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_execution_size_and_quota_rejection_retain_failure) {
  for (const bool quota : {false, true}) {
    Fixture f;
    f.options.max_object_bytes = quota ? 512 * 1024 : 16 * 1024;
    f.options.max_client_bytes = f.options.max_object_bytes;
    f.options.max_total_bytes = 2 * f.options.max_object_bytes;
    ObjectStore store(f.options);
    ResponsesExecution run(store);
    Q4T_CHECK(Prepare(&run));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    if (quota) {
      ObjectRequest filler;
      filler.operation = ObjectOperation::kPut;
      filler.kind = ObjectKind::kFile;
      filler.scope = Access().scope;
      filler.id = "file_retained";
      filler.data = Parse('"' + std::string(480 * 1024, 'x') + '"');
      Q4T_CHECK(Ok(store.Execute(filler, Clock::now() + 5s)));
    }
    Q4T_CHECK(run.Append(Text(std::string(24000, 'x')), "null", &wire));
    const auto rejected = run.Finish(false, 0, {7, 0, 2, 0}, 43, &wire);
    std::fprintf(stderr, "terminal quota=%d status=%d code=%s message=%s\n",
                 quota, rejected.http_status, rejected.code.c_str(),
                 rejected.message.c_str());
    Q4T_CHECK(rejected.code ==
              (quota ? "object_quota_exceeded" : "object_too_large"));
    Q4T_CHECK(wire.empty() && !run.Finished() && !run.CommitUnknown());
    ResponsesExecution::ObserveError(&run, rejected.http_status,
                                     rejected.message, rejected.code);
    Q4T_CHECK(Ok(run.CloseUnfinished()) && run.Finished());
    const auto record = ResponsesState(store).ReadRecord(run.Id(), Access());
    Q4T_CHECK(Ok(record));
    const auto* response = record.value.Find("response");
    Q4T_CHECK(response->GetString("status") == "failed");
    Q4T_CHECK(response->GetArray("output")->array.empty());
    Q4T_CHECK(response->Find("error")->GetString("code") == rejected.code);
    Q4T_CHECK(record.value.GetArray("events")->array.empty());
    Q4T_CHECK(!record.value.GetBool("events_retained", true));
    Q4T_CHECK(!record.value.Has("terminal_reserve"));
    Q4T_CHECK(record.value.GetArray("history")->array.size() == 1);
    ResponsesReadPlan plan;
    plan.id = run.Id();
    plan.stream = true;
    ResponsesReplay replay;
    Q4T_CHECK(ReadResponsesState(store, plan, Access(), &replay).code ==
              "response_events_unavailable");
    ObjectRequest items;
    items.operation = ObjectOperation::kList;
    items.kind = ObjectKind::kConversationItem;
    items.scope = Access().scope;
    const auto listed = store.Execute(items, Clock::now() + 5s);
    Q4T_CHECK(Ok(listed) && listed.value.GetArray("data")->array.empty());
    Q4T_CHECK(Ok(run.CloseUnfinished()));
  }
  return true;
}
