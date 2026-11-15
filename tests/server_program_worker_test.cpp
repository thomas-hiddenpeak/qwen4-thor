#include "q4t/server/program_worker.h"

#include <chrono>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/tool_result_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Input(std::string code) {
  return Object({{"code", String(std::move(code))},
                 {"tools", Array()},
                 {"calls", Array()},
                 {"results", Array()},
                 {"seed", Number(42)},
                 {"epoch_ms", Number(1000)}});
}
Json Definition(std::string name, std::string name_space = {},
                std::string type = "function") {
  return Object({{"name", String(name)},
                 {"namespace", String(name_space)},
                 {"key", String(name_space + "." + name)},
                 {"type", String(type)}});
}
ObjectReply Run(Json input, GrammarControl control = {}) {
  return ExecuteProgramWorker(
      input, {"client:program-component", Clock::now() + 3s, {}},
      std::move(control));
}
std::string Text(const ObjectReply& result, size_t index = 0) {
  const auto* output = result.value.GetArray("output");
  return output && output->array.size() > index
             ? output->array[index].GetString("value")
             : "";
}
bool Complete(const ObjectReply& reply) {
  return reply.ok() && reply.value.GetString("status") == "completed";
}
void Results(Json* input, Json calls, Json results) {
  Set(input, "calls", std::move(calls));
  Set(input, "results", std::move(results));
}
Json Result(int index, Json value) {
  return Object({{"index", Number(index)}, {"value", std::move(value)}});
}
}  // namespace

Q4T_TEST(program_worker_executes_javascript_and_freezes_time_random) {
  auto input = Input(
      "const values = [1, 2, 3].map(x => x * 2); "
      "text({sum: values.reduce((a,b) => a+b), now:Date.now(), "
      "date:+new Date(), random:Math.random()});");
  const auto first = Run(input);
  const auto second = Run(input);
  Q4T_CHECK(Complete(first) && Complete(second) && Text(first) == Text(second));
  Json value;
  Q4T_CHECK(q4t::io::ParseJson(Text(first), &value));
  Q4T_CHECK(value.GetInt("sum") == 12 && value.GetInt("now") == 1000 &&
            value.GetInt("date") == 1000 && value.GetNumber("random") > 0);
  return true;
}

Q4T_TEST(program_worker_parallel_calls_resume_with_actual_results) {
  auto input = Input(
      "const v = await Promise.all([tools.a({id:1}), "
      "tools.b({id:2})]); text(v[0].value + v[1].value);");
  Set(&input, "tools", Array({Definition("a"), Definition("b")}));
  const auto pending = Run(input);
  Q4T_CHECK(pending.ok() && pending.value.GetString("status") == "suspended");
  const auto calls = *pending.value.GetArray("calls");
  Q4T_CHECK(calls.array.size() == 2 &&
            calls.array[0].GetString("arguments") == "{\"id\":1}");
  Results(&input, calls,
          Array({Result(1, Object({{"value", Number(8)}})),
                 Result(0, Object({{"value", Number(7)}}))}));
  const auto done = Run(input);
  Q4T_CHECK(Complete(done) && Text(done) == "15" &&
            OutputJson(*done.value.Find("calls")) == OutputJson(calls));
  return true;
}

Q4T_TEST(program_worker_result_order_preserves_race_and_dependent_call) {
  auto input = Input(
      "const winner = await Promise.race([tools.a({}), "
      "tools.b({})]); text(await tools.c({winner}));");
  Set(&input, "tools",
      Array({Definition("a"), Definition("b"), Definition("c")}));
  const auto first = Run(input);
  Q4T_CHECK(first.ok() && first.value.GetString("status") == "suspended");
  auto events = Array({Result(1, String("second"))});
  Results(&input, *first.value.Find("calls"), events);
  const auto second = Run(input);
  Q4T_CHECK(second.ok() && second.value.GetString("status") == "suspended");
  const auto calls = *second.value.GetArray("calls");
  Q4T_CHECK(calls.array.size() == 3 &&
            calls.array[2].GetString("arguments") == "{\"winner\":\"second\"}");
  events.array.push_back(Result(0, String("first")));
  events.array.push_back(Result(2, String("real result")));
  Results(&input, calls, events);
  const auto done = Run(input);
  Q4T_CHECK(Complete(done) && Text(done) == "real result");
  return true;
}

Q4T_TEST(program_worker_namespace_custom_and_image_output) {
  auto input = Input(
      "text(await tools.docs.read('path')); "
      "image('data:image/png;base64,iVBORw0KGgo=');");
  Set(&input, "tools", Array({Definition("read", "docs", "custom")}));
  const auto pending = Run(input);
  Q4T_CHECK(pending.ok() && pending.value.GetString("status") == "suspended");
  Q4T_CHECK(pending.value.GetArray("calls")->array[0].GetString("arguments") ==
            "path");
  Results(&input, *pending.value.Find("calls"),
          Array({Result(0, String("actual document"))}));
  const auto done = Run(input);
  Q4T_CHECK(Complete(done) && Text(done) == "actual document" &&
            done.value.GetArray("output")->array[1].GetString("type") ==
                "image");
  return true;
}

Q4T_TEST(program_worker_replay_mismatch_and_duplicate_results_fail_closed) {
  auto input = Input("text(await tools.a({n:1}));");
  Set(&input, "tools", Array({Definition("a")}));
  const auto first = Run(input);
  Q4T_CHECK(first.ok() && first.value.GetString("status") == "suspended");
  auto calls = *first.value.Find("calls");
  Set(&calls.array[0], "arguments", String("{\"n\":2}"));
  Results(&input, calls, Array({Result(0, Number(1))}));
  const auto wrong = Run(input);
  Q4T_CHECK(wrong.ok() &&
            wrong.value.GetString("error") == "program_replay_mismatch");
  Results(&input, *first.value.Find("calls"),
          Array({Result(0, Number(1)), Result(0, Number(2))}));
  const auto duplicate = Run(input);
  Q4T_CHECK(duplicate.ok() &&
            duplicate.value.GetString("status") == "incomplete" &&
            duplicate.value.GetArray("calls")->array.empty());
  return true;
}

Q4T_TEST(program_worker_rejects_ambient_io_and_module_loading) {
  const auto globals =
      Run(Input("text([typeof process,typeof require,"
                "typeof fetch,typeof console,typeof std,typeof os]);"));
  Q4T_CHECK(Complete(globals) &&
            Text(globals) ==
                "[\"undefined\",\"undefined\",\"undefined\",\"undefined\","
                "\"undefined\",\"undefined\"]");
  const auto module = Run(Input("await import('std'); text('unsafe');"));
  Q4T_CHECK(module.ok() && module.value.GetString("status") == "incomplete" &&
            module.value.GetArray("output")->array.empty());
  return true;
}

Q4T_TEST(program_worker_time_memory_and_output_limits_leave_next_run_healthy) {
  for (const auto* code : {"while(true) {}", "new Array(20000000).fill(1);",
                           "for(let i=0;i<300;i++) text('item');"}) {
    const auto before = Clock::now();
    const auto failed = Run(Input(code));
    Q4T_CHECK(Clock::now() - before < 3s);
    Q4T_CHECK(!Complete(failed));
    const auto healthy = Run(Input("text('healthy');"));
    Q4T_CHECK(Complete(healthy) && Text(healthy) == "healthy");
  }
  return true;
}

Q4T_TEST(program_worker_microtasks_unhandled_rejection_and_call_bounds) {
  for (const auto* code :
       {"while(true) await Promise.resolve();",
        "Promise.reject('unhandled'); text('not a success');",
        "for(let i=0;i<65;i++) tools.a({});"}) {
    auto input = Input(code);
    Set(&input, "tools", Array({Definition("a")}));
    const auto result = Run(input);
    Q4T_CHECK(result.ok() && result.value.GetString("status") == "incomplete" &&
              result.value.GetArray("calls")->array.empty());
  }
  const auto handled =
      Run(Input("await Promise.reject('handled').catch("
                "() => text('caught'));"));
  Q4T_CHECK(Complete(handled) && Text(handled) == "caught");
  return true;
}

Q4T_TEST(program_worker_cancelled_and_unreaped_slot_is_bounded) {
  const auto input = Input("text('done');");
  auto counters = std::make_shared<GrammarWorkerCounters>();
  GrammarControl control;
  control.counters = counters;
  const auto cancelled = ExecuteProgramWorker(
      input, {"client:program", Clock::now() + 3s, [] { return true; }},
      control);
  Q4T_CHECK(cancelled.http_status == 499 && counters->started == 0);
  detail::DeferGrammarReapingForTesting(true);
  const auto first = Run(input, control);
  const auto second = Run(input, control);
  detail::DeferGrammarReapingForTesting(false);
  detail::PollIsolatedWorkers(IsolatedWorkerDomain::kPrograms);
  Q4T_CHECK(!first.ok() && !second.ok() && counters->started == 1 &&
            counters->reaped == 1 && counters->active == 0);
  const auto valid = Run(input);
  Q4T_CHECK(Complete(valid));
  const auto schema = Object({{"type", String("integer")}});
  Q4T_CHECK(ValidateToolResultSchemas(
                Array({Object({{"schema", schema}, {"output", String("7")}})}),
                {"client:ordinary", Clock::now() + 3s, {}})
                .ok());
  return true;
}
