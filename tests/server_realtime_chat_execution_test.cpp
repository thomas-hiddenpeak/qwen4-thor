#include "q4t/server/realtime_chat_execution.h"

#include "../src/server/object_protocol.h"
#include "../src/server/chat_model_step.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
struct Fixture {
  ClientAdmission clients;
  ClientIdentity client;
  ClientLease permit;
  HttpBodyBudget budget{1ULL << 20};
  std::shared_ptr<RequestControl> control;
  Json request =
      Object({{"messages", Array({Object({{"role", String("user")},
                                          {"content", String("hello")}})})}});
  Fixture() {
    client = clients.Authenticate("", "");
    if (!permit.Acquire(clients, client, ClientLane::kData))
      throw std::runtime_error("Realtime execution CPU owner");
    control = std::make_shared<RequestControl>(
        "resp_cpu", "", std::chrono::steady_clock::now() + 5s, client.Id());
  }
  RealtimeGenerationResult Run(
      const std::function<void(NativeChatGeneration&)>& execute,
      RealtimeDeltaSink sink = [](const ResponseDelta&) {
        return ObjectReply{};
      }) {
    return ExecuteRealtimeChat(request, client, permit, control, sink, execute,
                               budget);
  }
};
const char* body =
    R"({"choices":[{"message":{"role":"assistant","content":"answer"},"finish_reason":"stop"}]})";
void Reply(const char* value = body) {
  detail::SendSimple(-1, 200, "OK", value, "application/json");
}
}  // namespace

Q4T_TEST(
    realtime_chat_execution_borrows_online_identity_and_records_actual_usage) {
  Fixture f;
  bool owned = false;
  size_t deltas = 0;
  auto result = f.Run(
      [&](NativeChatGeneration& native) {
        auto* context = detail::HttpResponseContext::Current(-1);
        owned = context && context->Client().Allowed() &&
                context->RateDomain() == ClientRateDomain::kOnline &&
                context->TraceId() == f.control->id() &&
                f.permit.Holds(f.client, ClientLane::kData);
        context->Usage().Enable();
        context->Usage().BeginStep();
        context->Usage().Observe({22, 7});
        context->Usage().EndStep();
        ResponseDelta delta;
        delta.text = "answer";
        if (!native.Emit(delta)) throw std::runtime_error("CPU emit");
        Reply();
      },
      [&](const ResponseDelta& delta) {
        if (delta.text == "answer") ++deltas;
        return ObjectReply{};
      });
  Q4T_CHECK(owned && deltas == 1 && result.reply.ok());
  Q4T_CHECK(result.message.GetString("content") == "answer" && result.usage);
  Q4T_CHECK(result.usage->total.input == 22 &&
            result.usage->total.output == 7 && !result.usage->input_text);
  Q4T_CHECK(f.budget.Used() == 0);
  return true;
}

Q4T_TEST(realtime_chat_execution_is_once_only_even_after_ambiguous_output) {
  Fixture f;
  size_t calls = 0;
  auto execute = [&](NativeChatGeneration&) { ++calls; };
  const auto first = f.Run(execute);
  const auto second = f.Run(execute);
  Q4T_CHECK(first.reply.code == "realtime_response_incomplete" &&
            second.reply.code == "realtime_execution_already_claimed" &&
            calls == 1);
  Q4T_CHECK(!first.usage && !second.usage);
  return true;
}

Q4T_TEST(realtime_chat_execution_rejects_cross_client_and_cancelled_owners) {
  Fixture f;
  size_t calls = 0;
  auto execute = [&](NativeChatGeneration&) {
    ++calls;
    Reply();
  };
  f.control = std::make_shared<RequestControl>(
      "resp_other", "", std::chrono::steady_clock::now() + 5s,
      "different_client");
  Q4T_CHECK(f.Run(execute).reply.code == "realtime_execution_owner_invalid");
  f.control = std::make_shared<RequestControl>(
      "resp_cancelled", "", std::chrono::steady_clock::now() + 5s,
      f.client.Id());
  f.control->Cancel();
  Q4T_CHECK(f.Run(execute).reply.code == "explicit_cancel" && calls == 0);
  return true;
}

Q4T_TEST(realtime_chat_execution_preserves_quota_and_native_callback_failure) {
  Fixture f;
  const auto quota = f.Run([](NativeChatGeneration&) {
    detail::SendError(-1, 429, "CPU quota boundary", {},
                      "client_tpm_exhausted");
  });
  Q4T_CHECK(quota.reply.http_status == 429 &&
            quota.reply.code == "client_tpm_exhausted");
  Fixture second;
  const auto sink = second.Run(
      [](NativeChatGeneration& native) {
        ResponseDelta delta;
        delta.text = "answer";
        native.Emit(delta);
        Reply();
      },
      [](const ResponseDelta&) {
        auto reply =
            Error(503, "cpu_unknown_delivery", "CPU delivery uncertain");
        reply.commit_unknown = true;
        return reply;
      });
  Q4T_CHECK(sink.reply.code == "cpu_unknown_delivery" &&
            sink.reply.commit_unknown);
  return true;
}

Q4T_TEST(realtime_chat_execution_keeps_incomplete_and_unknown_usage_honest) {
  Fixture f;
  const auto result = f.Run([](NativeChatGeneration&) {
    Reply(
        R"({"choices":[{"message":{"role":"assistant","content":"answer"},"finish_reason":"length"}]})");
  });
  Q4T_CHECK(result.reply.ok() && !result.usage &&
            result.state == RealtimeResponseEnd::State::kIncomplete &&
            result.reason == "max_output_tokens");
  Fixture invalid;
  Q4T_CHECK(
      !invalid
           .Run([](NativeChatGeneration&) {
             Reply(
                 R"({"choices":[{"message":{},"finish_reason":"invented"}]})");
           })
           .reply.ok());
  return true;
}

Q4T_TEST(realtime_chat_execution_body_capacity_and_exception_do_not_replay) {
  Fixture f;
  HttpBodyLease occupied(f.budget);
  Q4T_CHECK(occupied.Acquire(1ULL << 20));
  size_t calls = 0;
  Q4T_CHECK(f.Run([&](NativeChatGeneration&) { ++calls; }).reply.code ==
                "body_capacity_exhausted" &&
            calls == 0);
  Fixture throwing;
  const auto result = throwing.Run([](NativeChatGeneration&) {
    throw std::runtime_error("CPU callback fault");
  });
  Q4T_CHECK(!result.reply.ok() &&
            result.state == RealtimeResponseEnd::State::kFailed);
  Q4T_CHECK(throwing.budget.Used() == 0);
  return true;
}

Q4T_TEST(realtime_prepared_invocation_is_bound_before_first_model_work) {
  Fixture f;
  RealtimeInvocation first(f.control), duplicate(f.control);
  Q4T_CHECK(first.Ready(f.control) && !duplicate.Ready(f.control));
  auto different = std::make_shared<RequestControl>(
      f.control->id(), "", std::chrono::steady_clock::now() + 5s,
      f.client.Id());
  Q4T_CHECK(!first.Ready(different) && !first.Consume(different));
  Q4T_CHECK(first.Consume(f.control) && !first.Consume(f.control));
  Fixture cancelled;
  RealtimeInvocation stopped(cancelled.control);
  cancelled.control->Cancel();
  Q4T_CHECK(!stopped.Ready(cancelled.control) &&
            !stopped.Consume(cancelled.control));
  return true;
}

Q4T_TEST(realtime_prepared_execution_preserves_native_media_and_tool_proofs) {
  Fixture f;
  ChatRequestPlan plan;
  plan.prepared_source_data = true;
  plan.responses_tool_media = true;
  plan.async_tool_call_ids = {"call_native"};
  plan.header_tools = Array();
  RealtimeInvocation invocation(f.control);
  size_t calls = 0;
  const auto result = ExecuteRealtimeChat(
      f.request, f.client, f.permit, f.control,
      [](const ResponseDelta&) { return ObjectReply{}; },
      [&](NativeChatGeneration& native) {
        if (native.PreparedPlan() != &plan || native.Preparation() ||
            !native.PreparedPlan()->prepared_source_data ||
            !native.PreparedPlan()->responses_tool_media ||
            native.PreparedPlan()->async_tool_call_ids !=
                std::vector<std::string>{"call_native"})
          throw std::runtime_error("lost prepared proof");
        ++calls;
        detail::HttpResponseContext::Current(-1)->Usage().Enable();
        Reply();
      },
      f.budget, {&plan, nullptr}, &invocation);
  Q4T_CHECK(result.reply.ok() && calls == 1 && result.usage &&
            result.usage->total == ActualUsage{} &&
            !invocation.Ready(f.control) && f.budget.Used() == 0);
  return true;
}

Q4T_TEST(realtime_prepared_execution_rejects_missing_and_foreign_invocation) {
  Fixture f, other;
  ChatRequestPlan plan;
  size_t calls = 0;
  auto execute = [&](NativeChatGeneration&) { ++calls; };
  auto sink = [](const ResponseDelta&) { return ObjectReply{}; };
  Q4T_CHECK(ExecuteRealtimeChat(f.request, f.client, f.permit, f.control, sink,
                                execute, f.budget, {&plan, nullptr})
                .reply.code == "realtime_preparation_owner_invalid");
  RealtimeInvocation foreign(other.control);
  Q4T_CHECK(ExecuteRealtimeChat(f.request, f.client, f.permit, f.control, sink,
                                execute, f.budget, {&plan, nullptr}, &foreign)
                .reply.code == "realtime_execution_already_claimed");
  Q4T_CHECK(calls == 0 && foreign.Ready(other.control));
  return true;
}

Q4T_TEST(
    realtime_prepared_admission_failure_is_zero_primary_and_cannot_replay) {
  Fixture f;
  ChatRequestPlan plan;
  RealtimeInvocation invocation(f.control);
  HttpBodyLease occupied(f.budget);
  Q4T_CHECK(occupied.Acquire(1ULL << 20));
  size_t calls = 0;
  const auto run = [&] {
    return ExecuteRealtimeChat(
        f.request, f.client, f.permit, f.control,
        [](const ResponseDelta&) { return ObjectReply{}; },
        [&](NativeChatGeneration&) { ++calls; }, f.budget, {&plan, nullptr},
        &invocation);
  };
  const auto first = run();
  Q4T_CHECK(first.reply.code == "body_capacity_exhausted" && first.usage &&
            first.usage->total == ActualUsage{});
  Q4T_CHECK(run().reply.code == "realtime_execution_already_claimed" &&
            calls == 0 && !invocation.Ready(f.control));
  return true;
}

Q4T_TEST(realtime_prepared_primary_missing_usage_never_becomes_known_zero) {
  Fixture f;
  ChatRequestPlan plan;
  RealtimeInvocation invocation(f.control);
  const auto result = ExecuteRealtimeChat(
      f.request, f.client, f.permit, f.control,
      [](const ResponseDelta&) { return ObjectReply{}; },
      [](NativeChatGeneration&) { Reply(); }, f.budget, {&plan, nullptr},
      &invocation);
  Q4T_CHECK(result.reply.ok() && !result.usage);
  return true;
}

Q4T_TEST(realtime_prepared_sheet_and_primary_share_one_rpm_and_actual_usage) {
  Fixture f;
  GatewayClientPolicy policy;
  const std::string secret(64, 'a');
  Q4T_CHECK(ParseGatewayClientPolicy(
      "{\"version\":2,\"fair_queue\":false,\"gateway_token\":\"" + secret +
          R"(","clients":[{"id":"alpha","max_inflight":2,)"
          R"("max_control_inflight":1,"max_prompt_tokens":128,)"
          R"("max_output_tokens":16,"max_body_bytes":1024,)"
          R"("requests_per_minute":1,"tokens_per_minute":200}]})",
      &policy));
  ClientAdmission clients;
  Q4T_CHECK(clients.Configure(policy));
  const auto client = clients.Authenticate("alpha", secret);
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(clients, client, ClientLane::kData));
  auto control = std::make_shared<RequestControl>(
      "resp_sheet", "", std::chrono::steady_clock::now() + 5s, client.Id());
  RealtimeInvocation invocation(control);
  ClientRateLease rate;
  ModelPreparation accounting(clients, client, rate, 16, 0, 64);
  detail::HttpResponseContext parent(detail::HttpResponseContext::Detached{});
  parent.SetClient(client, &permit);
  parent.Usage().Enable();
  Set(&f.request, "max_completion_tokens", Number(16));
  ChatRequestPlan plan;
  InternalModelStep step{f.request,
                         {},
                         accounting,
                         [&] {
                           return invocation.Ready(control)
                                      ? ObjectReply{}
                                      : Error(409, "cancelled",
                                              "CPU preparation owner");
                         },
                         {},
                         false,
                         {},
                         {},
                         0,
                         0};
  auto prepared = ExecuteInternalModelStep(
      -1, &step, control,
      [&](detail::ResponseState*, std::shared_ptr<RequestControl>) {
        if (!step.before_model().ok() || !accounting.ReservePrompt(20).ok() ||
            !accounting.StartStep())
          throw std::runtime_error("CPU sheet ownership");
        accounting.ObserveOutput(7);
        auto& usage = detail::HttpResponseContext::Current(-1)->Usage();
        usage.Enable();
        usage.BeginStep();
        usage.Observe({20, 7, 2, 1});
        usage.EndStep();
        step.completed = true;  // CPU executor receipt, never model quality.
      });
  Q4T_CHECK(prepared.ok() && accounting.ReadyForPrimary(client, 16));
  const auto primary = ExecuteRealtimeChat(
      f.request, client, permit, control,
      [](const ResponseDelta&) { return ObjectReply{}; },
      [&](NativeChatGeneration& native) {
        if (native.Preparation() != &accounting ||
            rate.ExtendPrompt(clients, client, 30) !=
                ClientRateResult::kAllowed)
          throw std::runtime_error("CPU primary rate handoff");
        rate.Start();
        rate.Completion(accounting.OutputTokens() + 4);
        auto& usage = detail::HttpResponseContext::Current(-1)->Usage();
        usage.Enable();
        usage.BeginStep();
        usage.Observe({30, 4, 3, 1});
        usage.EndStep();
        Reply();
      },
      f.budget, {&plan, &accounting}, &invocation);
  Q4T_CHECK(primary.reply.ok() && primary.usage);
  parent.Usage().Merge(primary.usage->total);
  Q4T_CHECK(parent.Usage().Total() == (ActualUsage{50, 11, 5, 2}));
  rate.Settle();
  const auto usage = clients.RateUsage(client.Index());
  Q4T_CHECK(usage.requests == 1 && usage.tokens == 61 && usage.reserved == 0);
  Q4T_CHECK(!accounting.ReadyForPrimary(client, 16) && f.budget.Used() == 0);
  return true;
}
