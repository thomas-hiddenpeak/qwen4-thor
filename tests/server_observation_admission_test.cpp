#include "q4t/server/client_request.h"
#include "q4t/server/http_connection.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;

Q4T_TEST(application_observation_is_bounded_during_active_generation) {
  ClientAdmission admission;
  GatewayClientPolicy policy;
  const std::string token(64, 'a');
  const auto json = R"({"version":1,"gateway_token":")" + token +
                    R"(","clients":[{"id":"alpha","max_inflight":1,)" +
                    R"("max_control_inflight":1,"max_prompt_tokens":64,)" +
                    R"("max_output_tokens":16,"max_body_bytes":1024}]})";
  Q4T_CHECK(ParseGatewayClientPolicy(json, &policy));
  Q4T_CHECK(admission.Configure(std::move(policy)));
  const auto owner = admission.Authenticate("alpha", token);
  ClientLease generation;
  Q4T_CHECK(generation.Acquire(admission, owner, ClientLane::kData));
  HttpRequestHeaders headers;
  headers.client_id = "alpha";
  headers.gateway_token = token;
  int status = 0;
  ClientIdentity identity;
  auto admit = [&](std::string_view method, std::string_view path,
                   ClientLease* lease) {
    return AdmitClientRequest(method, path, &headers, admission, lease,
                              &identity, &status);
  };
  struct Route {
    std::string_view method;
    std::string_view path;
  };
  constexpr Route observations[] = {
      {"GET", "/v1/threads/thread_a/runs/run_a?order=asc"},
      {"GET", "/v1/agents/agent_a"},
      {"GET", "/v1/evals/eval_a/runs/evalrun_a"},
      {"GET", "/v1/safety/cases/case_a"},
      {"GET", "/v1/realtime/traces/trace_a"},
      {"GET", "/v1/tunnels/tunnel_a"},
      {"GET", "/v1/audio/voice_consents"},
      {"GET", "/v1/responses/resp_a/input_items"},
      {"GET", "/v1/vector_stores/vs_a"},
      {"GET", "/v1/chatkit/threads/cthr_a/items"},
      {"DELETE", "/v1/webhook_endpoints/whe_a"},
      {"GET", "/v1/batches/batch_a"},
      {"GET", "/v1/files/file_a"},
      {"GET", "/v1/skills/skill_a"},
      {"GET", "/v1/containers/cntr_a"},
      {"GET", "/v1/prompts/prompt_a"},
      {"POST", "/v1/chat/completions/chat_a"},
  };
  for (const auto& [method, path] : observations) {
    if (ClassifyHttpConnection(method, path) !=
        HttpConnectionClass::kObservation) {
      std::fprintf(stderr, "Expected observation for %.*s %.*s\n",
                   static_cast<int>(method.size()), method.data(),
                   static_cast<int>(path.size()), path.data());
      return false;
    }
    ClientLease observation, overflow;
    Q4T_CHECK(admit(method, path, &observation));
    Q4T_CHECK(observation.Holds(owner, ClientLane::kControl));
    Q4T_CHECK(!admit(method, path, &overflow) && status == 429);
  }
  constexpr Route business[] = {
      {"POST", "/v1/threads/thread_a/runs"},
      {"GET", "/v1/files/file_a/content"},
      {"GET", "/v1/vector_stores/vs_a/files/file_a/content"},
      {"POST", "/v1/webhook_endpoints/whe_a/test"},
      {"GET", "/v1/webhook_endpoints/whe_a/rotate_secret"},
      {"DELETE", "/v1/webhook_event_types"},
  };
  for (const auto& [method, path] : business) {
    ClientLease unavailable;
    Q4T_CHECK(ClassifyHttpConnection(method, path) ==
              HttpConnectionClass::kBusiness);
    Q4T_CHECK(!admit(method, path, &unavailable) && status == 429);
  }
  ClientLease observation, cancel, second_cancel;
  Q4T_CHECK(admit("GET", "/v1/evals/eval_a/runs/evalrun_a", &observation));
  constexpr auto path = "/v1/evals/eval_a/runs/evalrun_a/cancel";
  Q4T_CHECK(ClassifyHttpConnection("POST", path) ==
            HttpConnectionClass::kControl);
  Q4T_CHECK(admit("POST", path, &cancel));
  Q4T_CHECK(cancel.Holds(owner, ClientLane::kCancel));
  Q4T_CHECK(!admit("POST", path, &second_cancel) && status == 429);
  return true;
}
}  // namespace
