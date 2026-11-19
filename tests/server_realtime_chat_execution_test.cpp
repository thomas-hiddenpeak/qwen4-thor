#include "q4t/server/realtime_chat_execution.h"

#include "../src/server/object_protocol.h"
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
