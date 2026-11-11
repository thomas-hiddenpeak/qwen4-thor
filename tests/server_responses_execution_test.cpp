#include "q4t/server/responses_execution.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "q4t/server/conversation_state.h"
#include "q4t/server/chat_prompt.h"
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
Json ModerationReport(bool flagged) {
  ModerationPlan plan;
  plan.model = "cpu";
  plan.text = true;
  Json result;
  const auto status = ParseModerationResult(
      flagged ? R"({"scores":[0,0,0,0,0,0,0,0,0,0,0,0.9,0]})"
              : R"({"scores":[0,0,0,0,0,0,0,0,0,0,0,0.1,0]})",
      plan, 0.5, &result);
  if (!status) throw std::runtime_error(status.message());
  return result;
}
}  // namespace

Q4T_TEST(responses_invalid_envelope_fails_before_prompt_state_lookup) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  for (const auto& [field, param] :
       std::vector<std::pair<std::string, std::string>>{
           {R"("input":null)", "input"},
           {R"("input":{})", "input"},
           {R"("user":null)", "user"},
           {R"("prompt_cache_options":null)", "prompt_cache_options"},
           {R"("reasoning":{"mode":null})", "reasoning.mode"},
           {R"("text":null)", "text"},
           {R"("unknown_option":true)", "unknown_option"}}) {
    ResponsesExecution run(store);
    const auto reply = run.Prepare(
        Parse(R"({"prompt":{"id":"prompt_missing"},"instructions":"Answer",)" +
              field + '}'),
        Config(), Access(), 42);
    Q4T_CHECK(reply.http_status == 400 &&
              reply.value.GetString("param") == param);
  }
  ResponsesExecution valid(store);
  Q4T_CHECK(valid.Prepare(Parse(R"({"prompt":{"id":"prompt_missing"}})"),
                           Config(), Access(), 42)
                .http_status == 404);
  return true;
}

Q4T_TEST(responses_service_tiers_report_actual_execution_mode) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  for (const auto* tier :
       {"auto", "default", "flex", "scale", "priority", "fast", "ultrafast"}) {
    ResponsesExecution run(store);
    Q4T_CHECK(Prepare(&run, std::string(R"(,"store":false,"service_tier":")") +
                               tier + '"'));
    Q4T_CHECK(run.Envelope().request.GetString("service_tier") == tier);
    Q4T_CHECK(run.Output().Snapshot().GetString("service_tier") == "default");
  }
  return true;
}

Q4T_TEST(responses_moderation_hold_hides_sse_checkpoint_and_background_replay) {
  for (const bool background : {false, true}) {
    Fixture fixture;
    ObjectStore store(fixture.options);
    ResponsesExecution run(store);
    const auto request = Parse(
        std::string(R"({"input":"question","background":)") +
        (background ? "true" : "false") +
        R"(,"stream":true,"stream_options":{"include_obfuscation":false}})");
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42, background)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    if (background) Q4T_CHECK(Ok(run.StartRunning(&wire)));
    Q4T_CHECK(Ok(run.HoldUnreviewedOutput()));
    Q4T_CHECK(run.Append(Text("PRIVATE_UNREVIEWED_OUTPUT"), "null", &wire));
    Q4T_CHECK(wire.empty() && Ok(run.Checkpoint()));
    auto stored = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(stored) && stored.value.GetArray("output")->array.empty());
    ResponsesReadPlan read;
    read.id = run.Id();
    read.stream = true;
    read.obfuscation = false;
    ResponsesReplay replay;
    Q4T_CHECK(Ok(ReadResponsesState(store, read, Access(), &replay)));
    Q4T_CHECK(!replay.terminal &&
              replay.wire.find("PRIVATE_UNREVIEWED_OUTPUT") ==
                  replay.wire.npos);
    Q4T_CHECK(run.Finish(false, 0, {20, 0, 4, 0}, 43, &wire).code ==
              "moderation_review_pending");
    Q4T_CHECK(run.PublishReviewedOutput(ModerationReport(true), &wire).code ==
              "moderation_not_approved");
    Q4T_CHECK(wire.empty() && run.OutputHeld());
    Q4T_CHECK(Ok(run.PublishReviewedOutput(ModerationReport(false), &wire)));
    Q4T_CHECK(wire.find("PRIVATE_UNREVIEWED_OUTPUT") != wire.npos &&
              !run.OutputHeld());
    Q4T_CHECK(Ok(run.Finish(false, 0, {20, 0, 4, 0}, 43, &wire)));
    stored = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(stored) && stored.value.GetString("status") == "completed" &&
              OutputJson(stored.value).find("PRIVATE_UNREVIEWED_OUTPUT") !=
                  std::string::npos);
  }
  return true;
}

Q4T_TEST(responses_moderation_cancel_and_errors_discard_all_private_prefixes) {
  for (const bool cancel : {false, true}) {
    Fixture fixture;
    ObjectStore store(fixture.options);
    ResponsesExecution run(store);
    if (cancel)
      Q4T_CHECK(Ok(run.Prepare(
          Parse(R"({"input":"question","background":true})"), Config(),
          Access(), 42, true)));
    else
      Q4T_CHECK(Prepare(&run));
    ResponsesExecution::ObserveUsage(&run, ActualUsage{7, 2, 1, 1, 0});
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    if (cancel) Q4T_CHECK(Ok(run.StartRunning(&wire)));
    Q4T_CHECK(Ok(run.HoldUnreviewedOutput()));
    const std::string private_text = "PRIVATE_PREFIX" + std::string(65536, 'x');
    Q4T_CHECK(run.Append(Text(private_text), "null", &wire) && wire.empty());
    if (cancel) {
      Q4T_CHECK(Ok(run.Cancel()));
    } else {
      wire = ResponsesExecution::RenderStreamError(
          &run, 503, "Classifier unavailable", "moderation_failed",
          "moderation");
      Q4T_CHECK(wire.find("PRIVATE_PREFIX") == wire.npos &&
                wire.find("response.output_text.delta") == wire.npos);
      Q4T_CHECK(Ok(run.CloseUnfinished()));
    }
    const auto record = ResponsesState(store).ReadRecord(run.Id(), Access());
    Q4T_CHECK(Ok(record) &&
              OutputJson(record.value).find("PRIVATE_PREFIX") ==
                  std::string::npos &&
              record.value.Find("response")->GetString("status") ==
                  (cancel ? "cancelled" : "failed"));
    Q4T_CHECK(
        record.value.Find("response")->Find("usage")->GetInt("total_tokens") ==
        9);
    Q4T_CHECK(!run.OutputHeld());
  }
  return true;
}

Q4T_TEST(responses_moderation_rejects_fake_approval_and_late_hold) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution held(store), late(store);
  Q4T_CHECK(Prepare(&held) && Prepare(&late));
  std::string wire;
  Q4T_CHECK(Ok(held.Begin(&wire)) && Ok(held.HoldUnreviewedOutput()));
  Q4T_CHECK(held.Append(Text("private text"), "null", &wire));
  Q4T_CHECK(
      held.PublishReviewedOutput(
              Parse(R"({"type":"moderation_result","flagged":false})"), &wire)
          .code == "moderation_not_approved");
  Q4T_CHECK(held.OutputHeld() && wire.empty() && Ok(held.CloseUnfinished()));
  Q4T_CHECK(Ok(late.Begin(&wire)) &&
            late.Append(Text("public"), "null", &wire));
  Q4T_CHECK(late.HoldUnreviewedOutput().code == "moderation_hold_invalid");
  Q4T_CHECK(Ok(late.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_moderation_lost_approval_receipt_cannot_leak_or_replay) {
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  Fixture fixture;
  fixture.options.io_timeout_ms = 100;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.HoldUnreviewedOutput()));
  Q4T_CHECK(run.Append(Text("PRIVATE_UNKNOWN_RECEIPT"), "null", &wire));
  Q4T_CHECK(!run.PublishReviewedOutput(ModerationReport(false), &wire).ok());
  Q4T_CHECK(run.CommitUnknown() && run.OutputHeld() && wire.empty());
  wire = ResponsesExecution::RenderStreamError(
      &run, 503, "Review checkpoint receipt unavailable",
      "object_transport_error", "");
  Q4T_CHECK(wire.find("PRIVATE_UNKNOWN_RECEIPT") == wire.npos &&
            wire.find("object_commit_unknown") != wire.npos &&
            wire.find("response.completed") == wire.npos &&
            wire.find("response.failed") == wire.npos);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_moderation_filtered_terminal_retains_usage_without_content) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(
      &run,
      R"(,"stream":true,"stream_options":{"include_obfuscation":false})"));
  std::string wire, all;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  all += wire;
  Q4T_CHECK(Ok(run.HoldUnreviewedOutput()));
  Q4T_CHECK(run.Append(Text("PRIVATE_BLOCKED"), "null", &wire) && wire.empty());
  auto results = Parse(R"({"input":null,"output":null})");
  results.object[0].second = ModerationReport(false);
  results.object[1].second = ModerationReport(true);
  Q4T_CHECK(run.SetModerationResults(results, {100, 40, 2}));
  run.DiscardUnreviewedOutput();
  Q4T_CHECK(Ok(run.Finish(true, 0, {80, 0, 13, 0}, 43, &wire, true)));
  all += wire;
  const auto& output = run.Output().Snapshot();
  Q4T_CHECK(output.GetArray("output")->array.empty() &&
            output.Find("incomplete_details")->GetString("reason") ==
                "content_filter" &&
            output.Find("moderation")->Find("output")->GetBool("flagged"));
  Q4T_CHECK(output.Find("usage")->GetInt("total_tokens") == 93 &&
            output.Find("q4t_moderation_usage")->GetInt("total_tokens") ==
                140 &&
            all.find("PRIVATE_BLOCKED") == all.npos);
  if (const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR")) {
    std::ofstream(std::filesystem::path(directory) /
                  "moderation-block.json.wire")
        << run.Output().Json();
    std::ofstream(std::filesystem::path(directory) /
                  "moderation-block.sse.wire")
        << all;
  }
  return true;
}

Q4T_TEST(responses_pro_persists_reasoning_mode_usage_and_original_input) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  const auto request = Parse(R"({"input":"17+25?","store":true,"stream":true,
    "stream_options":{"include_obfuscation":false},
    "reasoning":{"mode":"pro","effort":"none"}})");
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  std::string wire, all;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  all += wire;
  auto note = Text("Checked addition: 17 + 25 = 42.");
  note.kind = ResponseDelta::Kind::kReasoning;
  Q4T_CHECK(run.Append(note, "null", &wire));
  all += wire;
  Q4T_CHECK(run.Append(Text("42"), "null", &wire));
  all += wire;
  Q4T_CHECK(Ok(run.Finish(false, 0, {140, 0, 12, 9}, 43, &wire)));
  all += wire;
  const auto record = ResponsesState(store).ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(record));
  Q4T_CHECK(OutputJson(*record.value.Find("response")) == run.Output().Json());
  const auto& output = run.Output().Snapshot();
  Q4T_CHECK(output.Find("reasoning")->GetString("mode") == "pro" &&
            output.Find("reasoning")->GetString("effort") == "none");
  Q4T_CHECK(output.Find("usage")->GetInt("total_tokens") == 152 &&
            output.Find("usage")->Find("output_tokens_details")
                    ->GetInt("reasoning_tokens") == 9);
  Q4T_CHECK(OutputJson(*record.value.Find("own_input")).find("Checked addition")
                == std::string::npos);
  Q4T_CHECK(output.GetArray("output")->array.size() == 2 &&
            all.find("response.reasoning_text.delta") != std::string::npos);
  if (const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR")) {
    std::ofstream(std::filesystem::path(directory) / "pro.json.wire")
        << run.Output().Json();
    std::ofstream(std::filesystem::path(directory) / "pro.sse.wire") << all;
  }
  return true;
}

Q4T_TEST(responses_pro_queued_restore_cancel_and_no_started_replay) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution queued(store);
  const auto request = Parse(R"({"input":"17+25?","background":true,
    "reasoning":{"mode":"pro","effort":"none"}})");
  Q4T_CHECK(Ok(queued.Prepare(request, Config(), Access(), 42, true)));
  std::string wire;
  Q4T_CHECK(Ok(queued.Begin(&wire)));
  ResponsesExecution restored(store);
  Q4T_CHECK(Ok(restored.RestoreQueued(queued.Id(), Config(), Access())));
  Q4T_CHECK(restored.Envelope().reasoning_mode == "pro");
  Q4T_CHECK(Ok(restored.StartRunning(&wire)));
  auto note = Text("Partial actual working note");
  note.kind = ResponseDelta::Kind::kReasoning;
  Q4T_CHECK(restored.Append(note, "null", &wire));
  Q4T_CHECK(Ok(restored.Checkpoint()));
  Q4T_CHECK(Ok(restored.Cancel()));
  const auto record = ResponsesState(store).Retrieve(queued.Id(), Access());
  Q4T_CHECK(Ok(record) && record.value.GetString("status") == "cancelled");
  Q4T_CHECK(record.value.Find("reasoning")->GetString("mode") == "pro" &&
            OutputJson(*record.value.GetArray("output"))
                    .find("Partial actual working note") != std::string::npos);
  ResponsesExecution again(store);
  Q4T_CHECK(again.RestoreQueued(queued.Id(), Config(), Access()).code ==
              "response_not_queued");
  return true;
}

Q4T_TEST(responses_cache_prewarm_empty_output_usage_and_diagnostics_persist) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  const auto request = Parse(R"({"input":"prefix","store":true,
    "prompt_cache_key":"customer","reasoning":{"effort":"none"},
    "prompt_cache_options":{"prewarm":true,"ttl":"12h"}})");
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  Q4T_CHECK(run.Envelope().cache.prewarm);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  run.CacheDiagnostics(Parse(R"({"type":"unavailable"})"));
  Q4T_CHECK(Ok(run.Finish(false, 0, {1400, 0, 0, 0, 1300}, 43, &wire)));
  const auto& snapshot = run.Output().Snapshot();
  Q4T_CHECK(snapshot.GetString("status") == "completed" &&
            snapshot.GetArray("output")->array.empty());
  Q4T_CHECK(snapshot.Find("usage")->GetInt("output_tokens") == 0 &&
            snapshot.Find("usage")->Find("input_tokens_details")
                ->GetInt("cache_write_tokens") == 1300);
  Q4T_CHECK(snapshot.Find("prompt_cache_options")->GetString("ttl") == "12h" &&
            !snapshot.Find("prompt_cache_options")->Has("prewarm"));
  const auto record = ResponsesState(store).ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(record));
  Q4T_CHECK(OutputJson(*record.value.Find("response")) == run.Output().Json());
  const auto& terminal = record.value.GetArray("events")->array.back();
  Q4T_CHECK(terminal.GetString("type") == "response.completed" &&
            OutputJson(*terminal.Find("response")) == run.Output().Json());
  Q4T_CHECK(wire.find("output_item.added") == wire.npos);
  // Inspect the actual C++ terminal against the separately generated SDK
  // schema; the Python check does not manufacture missing output properties.
  if (const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR")) {
    std::ofstream(std::filesystem::path(output) / "cache-prewarm.json.wire")
        << run.Output().Json();
    std::ofstream(std::filesystem::path(output) / "cache-prewarm.sse.wire")
        << wire;
  }
  return true;
}

Q4T_TEST(responses_cache_usage_cannot_double_count_reads_and_writes) {
  ObjectStoreOptions options;
  options.directory = "invalid";
  ObjectStore store(options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run, R"(,"store":false,"prompt_cache_options":{"mode":"explicit"})"));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(!run.Finish(false, 0, {1400, 1000, 0, 0, 500}, 43, &wire).ok());
  Q4T_CHECK(!run.Finished() && wire.empty());
  Q4T_CHECK(Ok(run.Finish(false, 0, {1400, 1000, 0, 0, 400}, 43, &wire)));
  Q4T_CHECK(store.Health().workers_started == 0);
  return true;
}

namespace {
Json ContextRequest(bool background = false) {
  auto request =
      Parse(R"({"input":[{"role":"user","content":")" + std::string(2000, 'x') +
            R"("},{"role":"assistant","content":")" + std::string(2000, 'y') +
            R"("},{"role":"user","content":"current question"}],
    "truncation":"auto","reasoning":{"effort":"none"},
    "stream":true,"stream_options":{"include_obfuscation":false}})");
  request.object.emplace_back("background",
                              Parse(background ? "true" : "false"));
  return request;
}
q4t::Status ContextMeasure(const ResponsesGenerationPlan& candidate,
                           ChatPlanError* failure) {
  const auto& request = candidate.chat.Request(candidate.generation_request);
  std::string prompt;
  std::vector<VisionItem> media;
  auto status = PrepareChatPrompt(request, candidate.chat.constraints, &prompt,
                                  &media, &failure->param, &failure->code);
  if (!status) return status;
  auto config = Config();
  config.max_len = 1024;
  std::vector<uint32_t> tokens;
  ChatPromptBudget budget;
  return EncodeChatPrompt(
      request, config, prompt,
      [](std::string_view value, std::vector<uint32_t>* out) {
        out->assign(value.begin(), value.end());
        return q4t::Status();
      },
      &tokens, &budget, failure);
}
}  // namespace

Q4T_TEST(responses_context_execution_keeps_stored_history_and_input_items) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(ContextRequest(), Config(), Access(), 42)));
  const auto history = OutputJson(run.Generation().input.items);
  const auto identity = run.Id();
  Q4T_CHECK(Ok(run.FitContext(Config(), ContextMeasure)));
  Q4T_CHECK(run.Generation().truncated_items == 2 && run.Id() == identity);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(run.Append(Text("42"), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {7, 0, 2, 0}, 43, &wire)));
  const auto saved = ResponsesState(store).ReadRecord(identity, Access());
  Q4T_CHECK(Ok(saved));
  Q4T_CHECK(OutputJson(*saved.value.Find("history")) == history);
  Q4T_CHECK(OutputJson(*saved.value.Find("own_input")) == history);
  ResponsesReadPlan read;
  Q4T_CHECK(Ok(PlanResponsesRead(
      "GET", "/v1/responses/" + identity + "/input_items?order=asc", "",
      &read)));
  ResponsesReplay replay;
  const auto input = ReadResponsesState(store, read, Access(), &replay);
  Q4T_CHECK(Ok(input) && OutputJson(*input.value.Find("data")) == history);
  // A subsequent turn resolves full durable history, including dropped items.
  ResponsesExecution next(store);
  Q4T_CHECK(
      Ok(next.Prepare(Parse(R"({"input":"followup","previous_response_id":")" +
                            identity + R"(","truncation":"auto"})"),
                      Config(), Access(), 44)));
  Q4T_CHECK(next.Generation().input.items.array.size() == 5);
  Q4T_CHECK(OutputJson(next.Generation().input.items.array[0]) ==
            OutputJson(Parse(history).array[0]));
  Q4T_CHECK(Ok(next.FitContext(Config(), ContextMeasure)));
  return true;
}

Q4T_TEST(responses_context_background_restore_keeps_events_and_history) {
  Fixture f;
  std::string id, history, events;
  {
    ObjectStore store(f.options);
    ResponsesExecution run(store);
    Q4T_CHECK(
        Ok(run.Prepare(ContextRequest(true), Config(), Access(), 42, true)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    id = run.Id();
    const auto saved = ResponsesState(store).ReadRecord(id, Access());
    Q4T_CHECK(Ok(saved));
    history = OutputJson(*saved.value.Find("history"));
    events = OutputJson(*saved.value.Find("events"));
  }
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.RestoreQueued(id, Config(), Access())));
  std::string wire;
  Q4T_CHECK(Ok(run.StartRunning(&wire)));
  Q4T_CHECK(Ok(run.FitContext(Config(), ContextMeasure)));
  Q4T_CHECK(run.Generation().truncated_items == 2 && run.Id() == id);
  Q4T_CHECK(run.Output().Snapshot().GetInt("created_at") == 42);
  const auto running = ResponsesState(store).ReadRecord(id, Access());
  Q4T_CHECK(Ok(running) &&
            OutputJson(*running.value.Find("history")) == history);
  const auto prefix = Parse(events);
  for (size_t i = 0; i < prefix.array.size(); ++i)
    Q4T_CHECK(OutputJson(running.value.GetArray("events")->array[i]) ==
              OutputJson(prefix.array[i]));
  Q4T_CHECK(run.Append(Text("answer"), "null", &wire));
  Q4T_CHECK(!run.FitContext(Config(), ContextMeasure).ok());
  Q4T_CHECK(Ok(run.Finish(false, 0, {9, 0, 1, 0}, 43, &wire)));
  const auto done = ResponsesState(store).ReadRecord(id, Access());
  Q4T_CHECK(Ok(done) && OutputJson(*done.value.Find("history")) == history);
  const auto* ordered = done.value.GetArray("events");
  Q4T_CHECK(ordered &&
            ordered->array.back().GetString("type") == "response.completed");
  for (size_t i = 0; i < ordered->array.size(); ++i)
    Q4T_CHECK(ordered->array[i].GetInt("sequence_number") ==
              static_cast<int64_t>(i));
  return true;
}

Q4T_TEST(responses_context_deadline_cancel_and_missing_measurement_are_atomic) {
  ObjectStoreOptions options;
  options.directory = "invalid";
  ObjectStore store(options);
  for (const bool expire : {false, true}) {
    ResponsesExecution run(store);
    auto request = ContextRequest();
    request.object.emplace_back("store", Parse("false"));
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
    const auto original = OutputJson(run.Generation().input.items);
    Q4T_CHECK(run.FitContext(Config(), {}).code ==
              "response_executor_unavailable");
    bool stop = false;
    run.BindCancellation(expire ? Clock::now() - 1s : Clock::now() + 5s,
                         [&] { return stop; });
    int checks = 0;
    const auto failure =
        run.FitContext(Config(), [&](const auto& candidate, auto* error) {
          if (++checks == 2) {
            stop = true;
            return q4t::Status();
          }
          return ContextMeasure(candidate, error);
        });
    Q4T_CHECK(failure.http_status == (expire ? 408 : 499));
    Q4T_CHECK(checks == (expire ? 0 : 2));
    Q4T_CHECK(OutputJson(run.Generation().input.items) == original);
    Q4T_CHECK(!run.Begun() && run.Generation().truncated_items == 0);
  }
  Q4T_CHECK(store.Health().workers_started == 0);
  return true;
}

Q4T_TEST(responses_summary_checkpoint_replay_and_followup_keep_derived_item) {
  for (const bool background : {false, true}) {
    Fixture fixture;
    ObjectStore store(fixture.options);
    ResponsesExecution run(store);
    auto request = Parse(R"({"input":"17 plus 25", "stream":true,
      "reasoning":{"effort":"low","summary":"concise"}})");
    request.object.emplace_back("background",
                                Parse(background ? "true" : "false"));
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42, background)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    if (background) Q4T_CHECK(Ok(run.StartRunning(&wire)));
    auto reasoning = Text("17 + 25 = 42. Check addition.");
    reasoning.kind = ResponseDelta::Kind::kReasoning;
    Q4T_CHECK(run.Append(reasoning, "null", &wire));
    Q4T_CHECK(run.Append(Text("42"), "null", &wire));
    Q4T_CHECK(run.AppendSummary("Added the values", &wire));
    Q4T_CHECK(Ok(run.Checkpoint()));
    ResponsesReadPlan read;
    read.id = run.Id();
    read.stream = true;
    read.obfuscation = false;
    ResponsesReplay prefix;
    Q4T_CHECK(Ok(ReadResponsesState(store, read, Access(), &prefix)));
    Q4T_CHECK(!prefix.terminal &&
              prefix.wire.find("reasoning_summary_text.delta") !=
                  prefix.wire.npos);
    Q4T_CHECK(prefix.wire.find("response.completed") == prefix.wire.npos);
    Q4T_CHECK(run.AppendSummary(" and checked the result.", &wire));
    Q4T_CHECK(Ok(run.Finish(false, 0, {43, 0, 16, 10}, 43, &wire)));
    read.starting_after = prefix.last_sequence;
    ResponsesReplay tail;
    Q4T_CHECK(Ok(ReadResponsesState(store, read, Access(), &tail)));
    Q4T_CHECK(tail.terminal &&
              tail.wire.find("reasoning_summary_part.done") != tail.wire.npos);
    const auto stored = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(stored));
    const auto& item = stored.value.GetArray("output")->array.back();
    Q4T_CHECK(!item.Has("content") &&
              item.GetArray("summary")->array[0].GetString("text") ==
                  "Added the values and checked the result.");
    ResponsesExecution next(store);
    Q4T_CHECK(Ok(
        next.Prepare(Parse(R"({"input":"continue","previous_response_id":")" +
                           run.Id() + R"("})"),
                     Config(), Access(), 44)));
    Q4T_CHECK(
        OutputJson(next.Generation().input.items).find(item.GetString("id")) !=
        std::string::npos);
  }
  return true;
}

Q4T_TEST(
    responses_summary_checkpoint_failure_preserves_prefix_without_success) {
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  Fixture fixture;
  fixture.options.io_timeout_ms = 100;
  ObjectStore store(fixture.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Prepare(&run));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  const auto next = *run.Sequence();
  Q4T_CHECK(!run.AppendSummary(std::string(65536, 's'), &wire));
  Q4T_CHECK(run.CommitUnknown() && wire.empty());
  const auto failed = ResponsesExecution::RenderStreamError(
      &run, 503, "missing receipt", "object_transport_error", "");
  Q4T_CHECK(failed.find("reasoning_summary_text.delta") != failed.npos &&
            failed.find("object_commit_unknown") != failed.npos &&
            failed.find("response.completed") == failed.npos &&
            failed.find("summary_text.done") == failed.npos);
  size_t begin = 0;
  int64_t sequence = next;
  while ((begin = failed.find("data: ", begin)) != failed.npos) {
    const auto end = failed.find("\r\n", begin);
    Q4T_CHECK(Parse(failed.substr(begin + 6, end - begin - 6))
                  .GetInt("sequence_number") == sequence++);
    begin = end;
  }
  Q4T_CHECK(sequence == next + 4);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

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
      context.SetUsageObserver(&ResponsesExecution::ObserveUsage, &run);
      context.Usage().Enable();
      context.Usage().BeginStep();
      ActualUsage work{9, 4, 2, 1, 3};
      struct Refresh {
        detail::HttpResponseContext* http;
        ActualUsage* work;
      };
      Refresh refresh{&context, &work};
      context.SetUsageRefresh(
          [](void* value) noexcept {
            auto& current = *static_cast<Refresh*>(value);
            current.http->Usage().Observe(*current.work);
          },
          &refresh);
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
      context.SetUsageRefresh(nullptr, nullptr);
      context.SetUsageObserver(nullptr, nullptr);
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
    if (stream) {
      Q4T_CHECK(received.find("response.failed") != received.npos);
      Q4T_CHECK(received.find("\"total_tokens\":13") != received.npos);
    } else
      Q4T_CHECK(received.find("\"usage\"") == received.npos);
    const auto result = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(result) && result.value.GetString("status") == "failed");
    Q4T_CHECK(result.value.Find("usage")->GetInt("total_tokens") == 13 &&
              result.value.Find("usage")
                      ->Find("input_tokens_details")
                      ->GetInt("cache_write_tokens") == 3);
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
  Q4T_CHECK(result.ClientCode() == "object_commit_unknown" &&
            result.ClientMessage().find(run.Id()) != std::string::npos);
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
    ResponsesExecution::ObserveUsage(&run, ActualUsage{7, 2, 1, 1, 0});
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
    Q4T_CHECK(response->Find("usage")->GetInt("total_tokens") == 9);
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

Q4T_TEST(responses_cancel_usage_distinguishes_queued_drained_and_unknown) {
  Fixture fixture;
  ObjectStore store(fixture.options);
  for (const int mode : {0, 1, 2}) {
    ResponsesExecution run(store);
    Q4T_CHECK(Ok(run.Prepare(Parse(R"({"input":"question","background":true})"),
                             Config(), Access(), 42, true)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    if (mode != 0) Q4T_CHECK(Ok(run.StartRunning(&wire)));
    if (mode == 1) {
      ResponsesExecution::ObserveUsage(&run, ActualUsage{8, 3, 2, 1, 0});
      Q4T_CHECK(Ok(run.Checkpoint()));
    }
    auto before = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(before) && before.value.Find("usage")->IsNull());
    Q4T_CHECK(Ok(run.Cancel()));
    const auto after = ResponsesState(store).Retrieve(run.Id(), Access());
    Q4T_CHECK(Ok(after) && after.value.GetString("status") == "cancelled");
    const auto* usage = after.value.Find("usage");
    Q4T_CHECK(usage);
    if (mode == 2)
      Q4T_CHECK(usage->IsNull());
    else
      Q4T_CHECK(usage->GetInt("total_tokens") == (mode == 1 ? 11 : 0));
  }
  return true;
}
