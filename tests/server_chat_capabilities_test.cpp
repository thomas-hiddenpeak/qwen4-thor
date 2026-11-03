#include "q4t/server/chat_capabilities.h"

#include <algorithm>
#include <string>

#include "q4t/io/json.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/server/text_stop.h"
#include "q4t/test.h"

namespace {
bool Hash(const std::string& text) {
  return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
}  // namespace

Q4T_TEST(chat_capabilities_transport_observation_contract) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("m", false, true), &caps));
  const auto* observation = caps.Find("observability");
  Q4T_CHECK(observation && observation->GetInt("version") == 4);
  Q4T_CHECK(observation->GetBool("business_connections_exclude_observation"));
  Q4T_CHECK(observation->GetString("connection_rejections") ==
            "q4t_connection_rejections_total");
  const auto* http = caps.Find("http");
  Q4T_CHECK(http && http->GetInt("control_reserved_connections") == 4 &&
            http->GetInt("observation_reserved_connections") == 4);
  Q4T_CHECK(http->GetString("connection_profile") ==
            "q4t-http-partitions-v1");
  Q4T_CHECK(http->GetBool("observation_during_drain") &&
            http->GetBool("reservation_excludes_unclassified_header_flood"));
  const auto* routes = http->GetArray("observation_routes");
  Q4T_CHECK(routes && routes->array.size() == 4);
  Q4T_CHECK(observation->GetString("http_receive_failures") ==
            "q4t_http_receive_failures_total");
  Q4T_CHECK(observation->GetString("http_write_failures") ==
            "q4t_http_write_failures_total");
  Q4T_CHECK(observation->GetString("transport_counts") ==
            "first_receive_and_write_failure_per_connection");
  Q4T_CHECK(observation->GetString("transport_publication") ==
            "after_heartbeat_join");
  Q4T_CHECK(observation->GetBool("receive_excludes_business_validation"));
  Q4T_CHECK(observation->GetString("body_capacity_error") ==
            "body_capacity_exhausted");
  return true;
}

Q4T_TEST(chat_capabilities_text_stop_profile) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("m", false, true), &caps));
  const auto* stop = caps.Find("text_stop");
  Q4T_CHECK(stop && stop->GetString("profile") == q4t::server::kTextStopProfile);
  Q4T_CHECK(stop->GetInt("max_strings", 0) == 4);
  Q4T_CHECK(stop->GetInt("max_utf8_bytes", 0) == 256);
  Q4T_CHECK(!stop->GetBool("constrained_output", true));
  Q4T_CHECK(!stop->GetBool("state_cache", true));
  return true;
}

Q4T_TEST(chat_capabilities_preflight_contract) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("m", false, true), &caps));
  const auto* preflight = caps.Find("preflight");
  Q4T_CHECK(preflight && preflight->GetString("profile") == "q4t-preflight-v1");
  Q4T_CHECK(preflight->GetString("endpoint") == "/v1/chat/preflight");
  Q4T_CHECK(preflight->GetString("config_endpoint") == "/v1/config");
  Q4T_CHECK(preflight->GetBool("shares_chat_planning"));
  Q4T_CHECK(!preflight->GetBool("executes_model", true));
  Q4T_CHECK(!preflight->GetBool("compiles_grammar", true));
  Q4T_CHECK(!preflight->GetBool("reserves_slot", true));
  return true;
}

Q4T_TEST(chat_capabilities_identity) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
                q4t::server::ChatCapabilitiesJson("m", false, false), &caps)
                .ok());
  Q4T_CHECK(caps.GetString("object") == "q4t.capabilities");
  Q4T_CHECK(caps.GetInt("schema_version") == 1);
  Q4T_CHECK(caps.GetBool("implementation_only"));
  Q4T_CHECK(!caps.GetBool("release_attestation", true));
  Q4T_CHECK(caps.GetString("protocol_source_sha256") ==
            q4t::server::identity::kProtocol);
  Q4T_CHECK(Hash(caps.GetString("protocol_source_sha256")));
  const auto* templ = caps.Find("template");
  Q4T_CHECK(templ && templ->IsObject());
  Q4T_CHECK(templ->GetString("id") == "qwen4_exp-native-v2");
  Q4T_CHECK(templ->GetString("message_profile") == "qwen3.8-native-v2");
  Q4T_CHECK(templ->GetString("developer_mapping") == "system");
  Q4T_CHECK(!templ->GetBool("mixed_instruction_roles", true));
  const auto* reasoning = caps.Find("reasoning");
  Q4T_CHECK(reasoning && reasoning->GetString("profile") == "q4t-reasoning-v1");
  Q4T_CHECK(reasoning->GetString("default_field") == "reasoning_content");
  Q4T_CHECK(reasoning->GetBool("include_default"));
  Q4T_CHECK(reasoning->GetBool("options_require_parsed_messages"));
  Q4T_CHECK(reasoning->GetBool("thinking_token_budget"));
  Q4T_CHECK(reasoning->GetString("budget_profile") == "q4t-thinking-v1");
  Q4T_CHECK(reasoning->GetInt("max_forced_closure_tokens") == 14);
  Q4T_CHECK(templ->GetString("message_name") == "absent_or_null_only");
  for (const auto* key : {"reference_sha256", "renderer_source_sha256",
                          "tool_history_source_sha256", "fixture_sha256"})
    Q4T_CHECK(Hash(templ->GetString(key)));
  return true;
}

Q4T_TEST(chat_capabilities_option_contract) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
                q4t::server::ChatCapabilitiesJson("m", false, true), &caps)
                .ok());
  const auto* tools = caps.Find("tools");
  Q4T_CHECK(tools && tools->IsObject());
  const auto* choices = tools->GetArray("tool_choice");
  const bool backend = q4t::server::kStructuredDecodingAvailable;
  Q4T_CHECK(choices && choices->array.size() == (backend ? 4 : 1));
  Q4T_CHECK(choices->array[0].IsString() && choices->array[0].str == "auto");
  Q4T_CHECK(tools->GetBool("strict") == backend);
  Q4T_CHECK(tools->GetBool("constrained_decoding") == backend);
  const auto* structured = caps.Find("structured_output");
  Q4T_CHECK(structured && structured->GetBool("available") == backend);
  Q4T_CHECK(structured->GetString("backend_revision") ==
            q4t::server::kXGrammarRevision);
  Q4T_CHECK(!structured->GetBool("mtp", true));
  const auto* resources = structured->Find("resources");
  Q4T_CHECK(resources && resources->GetBool("compile_interruptible"));
  Q4T_CHECK(resources->GetInt("worker_address_space_bytes") ==
            q4t::server::kGrammarAddressSpaceLimit);
  Q4T_CHECK(!resources->GetBool("whole_server_ram_limit", true));
  for (const auto* choice : {"auto", "none", "required"}) {
    q4t::io::Json request;
    Q4T_CHECK(q4t::io::ParseJson(
                  std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                              R"("tool_choice":")") +
                      choice + "\"}",
                  &request)
                  .ok());
    Q4T_CHECK(q4t::server::ValidateChatContract(request, "m", false).ok() ==
              (std::string(choice) != "required"));
  }
  return true;
}

Q4T_TEST(chat_capabilities_runtime_flags) {
  for (const bool media : {false, true}) {
    for (const bool mtp : {false, true}) {
      q4t::io::Json caps;
      Q4T_CHECK(q4t::io::ParseJson(
                    q4t::server::ChatCapabilitiesJson("m", media, mtp), &caps)
                    .ok());
      const auto* runtime = caps.Find("runtime");
      Q4T_CHECK(runtime && runtime->GetBool("media_enabled") == media);
      Q4T_CHECK(runtime->GetBool("mtp_enabled") == mtp);
      const auto* cancellation = caps.Find("cancellation");
      Q4T_CHECK(cancellation &&
                !cancellation->GetBool("acceptance_is_drained", true) &&
                !cancellation->GetBool("request_replay_idempotent", true));
    }
  }
  return true;
}

Q4T_TEST(chat_capabilities_model_escaping) {
  const std::string model = "test\"\\\nmodel";
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
                q4t::server::ChatCapabilitiesJson(model, false, false), &caps)
                .ok());
  Q4T_CHECK(caps.GetString("model") == model);
  return true;
}
