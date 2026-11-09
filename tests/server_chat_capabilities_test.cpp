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
  Q4T_CHECK(observation && observation->GetInt("version") == 5);
  Q4T_CHECK(observation->GetBool("business_connections_exclude_observation"));
  Q4T_CHECK(observation->GetString("connection_rejections") ==
            "q4t_connection_rejections_total");
  const auto* http = caps.Find("http");
  Q4T_CHECK(http && http->GetInt("control_reserved_connections") == 4 &&
            http->GetInt("observation_reserved_connections") == 4);
  Q4T_CHECK(http->GetString("connection_profile") == "q4t-http-partitions-v2");
  Q4T_CHECK(http->GetBool("connection_reuse"));
  Q4T_CHECK(http->GetBool("chunked_requests"));
  Q4T_CHECK(http->GetInt("keep_alive_max_requests") == 100);
  Q4T_CHECK(http->GetInt("keep_alive_idle_ms") == 5000);
  Q4T_CHECK(http->GetString("expect_continue") ==
            "after_authentication_and_admission");
  Q4T_CHECK(http->GetString("sse_http11_framing") == "chunked");
  Q4T_CHECK(http->GetString("request_identity") == "reauthenticate_each_request");
  Q4T_CHECK(http->GetString("truncated_stream") == "close_without_replay");
  Q4T_CHECK(http->GetBool("observation_during_drain") &&
            http->GetBool("reservation_excludes_unclassified_header_flood"));
  const auto* routes = http->GetArray("observation_routes");
  Q4T_CHECK(routes && routes->array.size() == 13);
  Q4T_CHECK(observation->GetString("http_receive_failures") ==
            "q4t_http_receive_failures_total");
  Q4T_CHECK(observation->GetString("http_write_failures") ==
            "q4t_http_write_failures_total");
  Q4T_CHECK(observation->GetString("transport_counts") ==
            "first_receive_and_write_failure_per_request");
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
  Q4T_CHECK(stop &&
            stop->GetString("profile") == q4t::server::kTextStopProfile);
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
  Q4T_CHECK(templ->GetString("id") == "qwen4_exp-openai-v3");
  Q4T_CHECK(templ->GetString("message_profile") == "qwen3.8-openai-v3");
  Q4T_CHECK(templ->GetString("developer_mapping") == "system");
  Q4T_CHECK(templ->GetBool("mixed_instruction_roles"));
  Q4T_CHECK(templ->GetBool("refusal_history"));
  Q4T_CHECK(!templ->GetBool("user_message_required", true));
  const auto* reasoning = caps.Find("reasoning");
  Q4T_CHECK(reasoning && reasoning->GetString("profile") == "q4t-reasoning-v1");
  Q4T_CHECK(reasoning->GetString("default_field") == "reasoning_content");
  Q4T_CHECK(reasoning->GetBool("include_default"));
  Q4T_CHECK(reasoning->GetBool("options_require_parsed_messages"));
  Q4T_CHECK(reasoning->GetBool("thinking_token_budget"));
  Q4T_CHECK(reasoning->GetString("budget_profile") == "q4t-thinking-v1");
  Q4T_CHECK(reasoning->GetInt("max_forced_closure_tokens") == 14);
  Q4T_CHECK(templ->GetString("message_name") == "json_quoted_participant");
  Q4T_CHECK(templ->GetInt("name_max_bytes") == 4096);
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
  const auto* chat = caps.Find("chat");
  Q4T_CHECK(chat && chat->GetString("sampling_profile") == "q4t-sampling-v1");
  Q4T_CHECK(chat->GetInt("temperature_default") == 1 &&
            chat->GetInt("choices") == 128 && chat->GetBool("logprobs") &&
            chat->GetInt("top_logprobs_max") == 20);
  Q4T_CHECK(caps.Find("chat")->GetString("stream_tool_arguments") ==
            "incremental");
  Q4T_CHECK(caps.Find("chat")->GetString("tool_execution_barrier") ==
            "successful_terminal_and_eof");
  const auto* choices = tools->GetArray("tool_choice");
  const bool backend = q4t::server::kStructuredDecodingAvailable;
  Q4T_CHECK(choices && choices->array.size() == (backend ? 5 : 1));
  Q4T_CHECK(choices->array[0].IsString() && choices->array[0].str == "auto");
  Q4T_CHECK(tools->GetBool("strict") == backend);
  Q4T_CHECK(tools->GetBool("constrained_decoding") == backend);
  const auto* structured = caps.Find("structured_output");
  Q4T_CHECK(structured && structured->GetBool("available") == backend);
  Q4T_CHECK(structured->GetString("schema_profile") == "q4t-json-schema-v2");
  Q4T_CHECK(structured->GetString("stream_payload") ==
            "incremental_provisional_until_success_terminal_and_eof");
  Q4T_CHECK(structured->GetString("json_with_tools") ==
            "json_content_or_tool_calls");
  Q4T_CHECK(structured->GetInt("max_schema_depth") == 10);
  Q4T_CHECK(structured->GetInt("max_schema_properties") == 5000);
  Q4T_CHECK(structured->GetInt("max_tools") == 128);
  Q4T_CHECK(structured->GetBool("exact_decimal_data"));
  Q4T_CHECK(structured->GetBool("session_cache") &&
            structured->GetBool("prefix_cache"));
  Q4T_CHECK(structured->GetString("cache_layout") == "target_only");
  const auto* resources = structured->Find("resources");
  Q4T_CHECK(resources && resources->GetInt("active_limit") == 1);
  Q4T_CHECK(resources->GetInt("cleanup_timeout_ms") ==
            q4t::server::kGrammarCleanupMilliseconds);
  Q4T_CHECK(resources->GetBool("unreaped_counts_toward_capacity"));
  Q4T_CHECK(resources->GetString("shutdown") == "bounded_kill_then_owned_reap");
  Q4T_CHECK(resources->GetString("deferred_owner") == "process");
  Q4T_CHECK(!resources->GetBool("deferred_request_callbacks", true));
  Q4T_CHECK(!structured->GetBool("cleanup_pending", true));
  Q4T_CHECK(resources->GetString("terminal_handoff") ==
            "wait_for_cleanup_with_request_cancellation");
  Q4T_CHECK(resources->GetString("handoff_waiter_bound") ==
            "max_inflight_requests");
  Q4T_CHECK(structured->GetString("backend_revision") ==
            q4t::server::kXGrammarRevision);
  Q4T_CHECK(!structured->GetBool("mtp", true));
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

Q4T_TEST(chat_capabilities_media_and_host_failure_boundaries) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("m", true, false), &caps));
  const auto* media = caps.Find("media_failures");
  const auto* health = caps.Find("model_health");
  Q4T_CHECK(health && health->GetString("profile") == "q4t-model-health-v1");
  Q4T_CHECK(health->GetString("error_code") == "model_unhealthy" &&
            health->GetBool("readiness_closed") &&
            health->GetBool("liveness_independent") &&
            !health->GetBool("automatic_recovery", true));
  Q4T_CHECK(media &&
            media->GetString("requested_load_failure") == "startup_rejected");
  Q4T_CHECK(media->GetInt("invalid_input_http_status") == 400 &&
            media->GetInt("device_failure_http_status") == 503 &&
            !media->GetBool("source_decode_hard_budget"));
  const auto* errors = caps.Find("errors");
  const auto* host = errors ? errors->Find("host_memory") : nullptr;
  Q4T_CHECK(host && host->GetInt("http_status") == 503 &&
            host->GetString("code") == "host_memory_exhausted" &&
            host->GetBool("fixed_buffer_fallback") &&
            !host->GetBool("recovery_guaranteed"));
  return true;
}

Q4T_TEST(chat_capabilities_native_completions_and_models) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("m", false, true), &caps));
  const auto* completion = caps.Find("completions");
  Q4T_CHECK(completion && completion->GetString("profile") ==
            "q4t-completions-v1");
  Q4T_CHECK(completion->GetBool("usage_includes_unreturned_candidates"));
  Q4T_CHECK(completion->GetBool("prompt_logprobs"));
  Q4T_CHECK(completion->GetString("token_input") == "exact_ids");
  Q4T_CHECK(!completion->GetBool("best_of_selection_streaming", true));
  const auto* models = caps.Find("models");
  Q4T_CHECK(models && models->GetInt("loaded_base_delete") == 403);
  Q4T_CHECK(models->GetInt("max_aliases") == 32);
  return true;
}

Q4T_TEST(chat_capabilities_native_responses_state_and_recovery) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("m", false, true), &caps));
  const auto* responses = caps.Find("responses");
  Q4T_CHECK(responses && responses->GetBool("shared_native_generation") &&
            responses->GetBool("store_default") &&
            !responses->GetBool("chat_done_sentinel", true));
  Q4T_CHECK(responses->GetBool("previous_response_id") &&
            responses->GetBool("conversation"));
  const auto* summary = responses->Find("summary");
  const auto* moderation = responses->Find("moderation");
  Q4T_CHECK(moderation && moderation->GetBool("enabled") &&
            moderation->GetString("executor") == "native_served_model" &&
            moderation->GetBool("block_output_before_publication") &&
            moderation->GetBool("same_client_tpm") &&
            moderation->GetInt("max_steps") == 2 &&
            moderation->GetInt("auxiliary_output_tokens_per_step") == 512 &&
            !moderation->GetBool("model_restart_on_error", true));
  const auto* execution = responses->Find("reasoning_execution");
  Q4T_CHECK(execution && execution->GetString("pro") ==
                            "native_deliberation_then_answer" &&
            execution->GetBool("effort_independent") &&
            execution->GetBool("shared_output_budget") &&
            !execution->GetBool("speculative_tool_execution", true) &&
            !execution->GetBool("cloud_model_equivalence", true));
  const auto* cache = responses->Find("prompt_cache");
  Q4T_CHECK(cache && cache->GetBool("native_state") &&
            cache->GetInt("max_writes") == 4 &&
            cache->GetInt("minimum_tokens") == 1024 &&
            !cache->GetBool("core_readiness_dependency", true));
  Q4T_CHECK(cache->GetString("prewarm") == "input_only_no_sampling");
  const auto* encrypted = responses->Find("encrypted_reasoning");
  Q4T_CHECK(encrypted && encrypted->GetBool("enabled") &&
            encrypted->GetString("algorithm") == "AES-256-GCM" &&
            encrypted->GetBool("key_persistence") &&
            encrypted->GetBool("rotation") &&
            encrypted->GetBool("store_false") &&
            encrypted->GetString("key_management") ==
                "native_cli_same_uid_unix_socket" &&
            !encrypted->GetBool("key_rotation_model_restart", true) &&
            !encrypted->GetBool("cross_provider_keys", true));
  Q4T_CHECK(responses->GetBool("reasoning_summary") && summary &&
            summary->GetBool("shared_output_budget") &&
            summary->GetBool("usage_includes_extra_input") &&
            summary->GetBool("single_request_admission") &&
            summary->GetBool("shared_cancellation") &&
            summary->GetString("source") == "actual_reasoning");
  const auto* compaction = responses->Find("compaction");
  Q4T_CHECK(compaction && compaction->GetBool("enabled") &&
            compaction->GetBool("automatic") &&
            compaction->GetBool("trigger_item") &&
            compaction->GetBool("shared_output_budget") &&
            compaction->GetBool("requires_measured_reduction") &&
            compaction->GetString("endpoint") == "/v1/responses/compact");
  const auto* truncation = responses->Find("truncation");
  Q4T_CHECK(truncation && truncation->GetBool("auto") &&
            truncation->GetBool("disabled") &&
            truncation->GetBool("preserves_active_turn") &&
            truncation->GetBool("persistent_history_unchanged"));
  const auto* jobs = responses->Find("background");
  Q4T_CHECK(jobs && jobs->GetInt("cancel_status") == 200 &&
            jobs->GetBool("cancel_idempotent") &&
            !jobs->GetBool("replays_started_generation", true) &&
            !jobs->GetBool("recovery_resets_deadline", true));
  Q4T_CHECK(jobs->GetString("recovery") ==
            "verified_dead_owner_queued_only");
  const auto* conversations = caps.Find("conversations");
  Q4T_CHECK(conversations && conversations->GetBool("items_append") &&
            conversations->GetString("retention") == "until_explicit_delete");
  const auto* objects = caps.Find("application_state");
  Q4T_CHECK(objects && objects->GetBool("isolated_io") &&
            !objects->GetBool("core_readiness_dependency", true));
  Q4T_CHECK(objects->GetString("controller_recovery") ==
                "single_owned_thread_bounded_backoff" &&
            objects->GetString("foreground_reconciliation") ==
                "reserved_metadata_after_executor_cleanup");
  const auto* http = caps.Find("http");
  Q4T_CHECK(http &&
            http->GetString("query_parameters") ==
                "route_specific_closed_contract" &&
            http->GetInt("client_cancel_inflight") == 1);
  return true;
}
