#include "q4t/server/live_configuration.h"
#include "q4t/server/live_media_policy.h"
#include "q4t/server/live_responses_settings.h"
#include "q4t/server/mcp_policy.h"

#include <stdexcept>

#include "../src/server/responses_json.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using q4t::Status;
using namespace q4t::server;
using namespace q4t::server::responses_detail;
Json Parse(std::string text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("Invalid Live configuration test JSON");
  return value;
}
Json Message() {
  return Parse(R"({"type":"message","role":"user","content":[
    {"type":"input_text","text":"prior context"}]})");
}
struct Fixture {
  LiveConfigurationContext context;
  ChatPlanError error;
  Json output = String("untouched"), observed;
  std::string counted_text;
  size_t text_tokens = 7, history_tokens = 0, history_items = 0,
         validations = 0;
  bool accept = true;
  Fixture() {
    context.default_instructions = "Native conversation instructions";
    context.default_voice = String("vivian");
    context.count_text = [this](std::string_view text, size_t* count, auto*) {
      counted_text = text;
      *count = text_tokens;
      return Status{};
    };
    context.count_history = [this](const Json& items, size_t* count, auto*) {
      history_items = items.array.size();
      *count = history_tokens;
      return Status{};
    };
    context.validate = [this](const Json& value, ChatPlanError* failure) {
      ++validations;
      observed = value;
      if (!accept) {
        *failure = {"model_not_found", "session.model", 404};
        return Status::Fail("Native model unavailable");
      }
      return Status{};
    };
  }
  bool Resolve(const Json& input = Parse(R"({"model":"native"})")) {
    return ResolveLiveConfiguration(input, context, &output, &error).ok();
  }
  bool Update(const Json& patch) {
    return UpdateLiveConfiguration(output, patch, context, &output, &error)
        .ok();
  }
};
}  // namespace

Q4T_TEST(live_configuration_native_defaults_and_counter_binding) {
  Fixture f;
  auto input = Parse(R"({"model":"native","instructions":" \t\n",
                         "delegation":null})");
  const auto before = OutputJson(input);
  Q4T_CHECK(f.Resolve(input));
  Q4T_CHECK(OutputJson(input) == before);
  Q4T_CHECK(f.counted_text == f.context.default_instructions);
  Q4T_CHECK(f.output.GetString("instructions") == f.counted_text);
  Q4T_CHECK(!f.output.GetBool("store") && f.history_items == 0);
  const auto* audio = f.output.Find("audio");
  Q4T_CHECK(audio->Find("format")->GetInt("rate") == 24000);
  Q4T_CHECK(audio->Find("output")->GetString("voice") == "vivian");
  Q4T_CHECK(f.output.Find("delegation")->GetString("type") == "client");
  Q4T_CHECK(f.output.Find("delegation")->GetInt("acknowledgement_delay_ms") ==
            0);
  Q4T_CHECK(f.validations == 1 &&
            OutputJson(f.observed) == OutputJson(f.output));
  return true;
}

Q4T_TEST(live_configuration_token_limits_use_rendered_counts) {
  Fixture f;
  auto input = Parse(R"({"model":"native","instructions":"short text"})");
  Set(&input, "input", Array({Message()}));
  f.text_tokens = 16384;
  f.history_tokens = 8192;
  Q4T_CHECK(f.Resolve(input));
  Q4T_CHECK(f.counted_text == "short text" && f.history_items == 1);
  const auto accepted = OutputJson(f.output);
  f.text_tokens = 16385;
  Q4T_CHECK(!f.Resolve(input));
  Q4T_CHECK(f.error.param == "session.instructions");
  Q4T_CHECK(f.error.code == "context_length_exceeded");
  f.text_tokens = 16384;
  f.history_tokens = 8193;
  Q4T_CHECK(!f.Resolve(input));
  Q4T_CHECK(f.error.param == "session.input");
  Q4T_CHECK(OutputJson(f.output) == accepted && f.validations == 1);
  return true;
}

Q4T_TEST(live_configuration_history_and_trigger_semantics) {
  Fixture f;
  auto input = Parse(R"({"model":"native"})");
  auto history = Array();
  history.array.assign(128, Message());
  Set(&input, "input", history);
  Q4T_CHECK(f.Resolve(input));
  history.array.push_back(Message());
  Set(&input, "input", history);
  Q4T_CHECK(!f.Resolve(input));
  history.array.resize(1);
  Field(history.array[0], "content")
      .array.push_back(Field(history.array[0], "content").array[0]);
  Set(&input, "input", history);
  Q4T_CHECK(!f.Resolve(input));
  Set(&input, "input", Array({Message()}));
  auto trigger = Parse(R"({"type":"decisions",
    "event_type":"session.input_transcript.delta",
    "decisions":{"questions":[]}})");
  Set(&input, "triggers", Array({trigger, trigger}));
  Q4T_CHECK(!f.Resolve(input));
  Q4T_CHECK(f.error.param == "session.triggers");
  Set(&trigger, "event_type", String("session.output_transcript.delta"));
  Field(input, "triggers").array[1] = trigger;
  Q4T_CHECK(f.Resolve(input));
  Q4T_CHECK(f.Update(Parse(R"({"triggers":[]})")));
  Q4T_CHECK(f.output.GetArray("triggers")->array.empty());
  return true;
}

Q4T_TEST(live_configuration_websocket_and_negotiated_audio_formats) {
  Fixture f;
  for (const auto* type : {"audio/pcm", "audio/pcmu", "audio/pcma"}) {
    for (const int rate : {8000, 16000, 24000, 48000}) {
      auto input = Parse(R"({"model":"native"})");
      Set(&input, "audio",
          Object({{"format",
                   Object({{"type", String(type)}, {"rate", Number(rate)}})}}));
      const bool valid = std::string(type) == "audio/pcm"
                             ? rate == 16000 || rate == 24000
                             : rate == 8000;
      Q4T_CHECK(f.Resolve(input) == valid);
    }
  }
  for (const auto transport : {LiveTransport::kWebRtc, LiveTransport::kSip}) {
    f.context.transport = transport;
    Q4T_CHECK(f.Resolve());
    Q4T_CHECK(!f.output.Find("audio")->Has("format"));
    Q4T_CHECK(!f.Resolve(Parse(R"({"model":"native","audio":{
      "format":{"type":"audio/pcm","rate":24000}}})")));
    Q4T_CHECK(f.error.param == "session.audio.format");
  }
  return true;
}

Q4T_TEST(live_configuration_sparse_backend_update_and_null) {
  Fixture f;
  Q4T_CHECK(f.Resolve(Parse(R"({"model":"native","delegation":{
    "type":"responses","acknowledgement_delay_ms":800,"responses":{
      "model":"backend","instructions":"business rules",
      "reasoning":{"effort":"low","summary":"auto"},
      "text":{"verbosity":"low"},
      "tool_choice":"auto","tools":[]}}})")));
  Q4T_CHECK(f.Update(Parse(R"({"delegation":{"type":"responses","responses":{
    "instructions":null,"reasoning":{"effort":"high"},
    "text":{},"tools":[]}}})")));
  const auto* delegation = f.output.Find("delegation");
  Q4T_CHECK(delegation->GetInt("acknowledgement_delay_ms") == 800);
  const auto* backend = delegation->Find("responses");
  Q4T_CHECK(backend->GetString("model") == "backend");
  Q4T_CHECK(backend->Find("instructions")->IsNull());
  Q4T_CHECK(backend->Find("reasoning")->GetString("effort") == "high");
  Q4T_CHECK(backend->Find("reasoning")->GetString("summary") == "auto");
  Q4T_CHECK(backend->Find("text")->GetString("verbosity") == "low");
  Q4T_CHECK(f.Update(Parse(R"({"delegation":{"type":"responses",
    "responses":{"reasoning":null,"text":null}}})")));
  backend = f.output.Find("delegation")->Find("responses");
  Q4T_CHECK(backend->Find("reasoning")->IsNull());
  Q4T_CHECK(backend->Find("text")->IsNull());
  return true;
}

Q4T_TEST(live_configuration_delegation_mode_and_timer_boundaries) {
  Fixture f;
  Q4T_CHECK(f.Resolve(Parse(R"({"model":"native","delegation":{
    "type":"decisions","decisions":{"model":"backend","questions":[]}}})")));
  Q4T_CHECK(f.output.Find("delegation")->GetInt("acknowledgement_delay_ms") ==
            400);
  auto before = OutputJson(f.output);
  Q4T_CHECK(!f.Update(Parse(R"({"delegation":null})")));
  Q4T_CHECK(!f.Update(Parse(R"({"delegation":{"type":"responses"}})")));
  Q4T_CHECK(OutputJson(f.output) == before);
  Q4T_CHECK(f.Update(Parse(R"({"delegation":{"type":"decisions",
    "acknowledgement_delay_ms":0,"decisions":{"questions":[]}}})")));
  Q4T_CHECK(
      f.output.Find("delegation")->Find("decisions")->GetString("model") ==
      "backend");
  for (const auto* duration : {"-1", "0.5", "2147483648"}) {
    auto patch = Parse(std::string(R"({"delegation":{"type":"decisions",
      "acknowledgement_delay_ms":)") +
                       duration + "}}");
    before = OutputJson(f.output);
    Q4T_CHECK(!f.Update(patch));
    Q4T_CHECK(OutputJson(f.output) == before);
  }
  Q4T_CHECK(f.Resolve());
  Q4T_CHECK(f.Update(Parse(R"({"delegation":{"type":"client",
    "acknowledgement_delay_ms":900}})")));
  Q4T_CHECK(f.Update(Parse(R"({"delegation":null})")));
  Q4T_CHECK(f.output.Find("delegation")->GetInt("acknowledgement_delay_ms") ==
            900);
  return true;
}

Q4T_TEST(live_configuration_startup_only_fields_reject_atomically) {
  Fixture f;
  Q4T_CHECK(f.Resolve());
  const auto accepted = OutputJson(f.output);
  for (const auto* patch :
       {R"({"model":"other"})", R"({"instructions":"x"})", R"({"input":[]})",
        R"({"store":true})", R"({"audio":{}})", R"({"client":{}})"}) {
    Q4T_CHECK(!f.Update(Parse(patch)));
    Q4T_CHECK(OutputJson(f.output) == accepted);
  }
  auto duplicate = Parse(R"({"triggers":[]})");
  duplicate.object.push_back(duplicate.object[0]);
  Q4T_CHECK(!f.Update(duplicate));
  Q4T_CHECK(OutputJson(f.output) == accepted && f.validations == 1);
  return true;
}

Q4T_TEST(live_configuration_backend_rejection_and_missing_owners) {
  Fixture f;
  f.accept = false;
  Q4T_CHECK(!f.Resolve());
  Q4T_CHECK(f.output.str == "untouched" && f.error.http_status == 404);
  f.context.validate = [](const Json&, ChatPlanError*) -> Status {
    throw std::runtime_error("Native backend exception");
  };
  Q4T_CHECK(!f.Resolve() && f.error.http_status == 503);
  f.context.count_text = {};
  Q4T_CHECK(!f.Resolve() && f.output.str == "untouched");
  Q4T_CHECK(!ResolveLiveConfiguration(Parse(R"({"model":"native"})"), f.context,
                                      nullptr, &f.error));
  Q4T_CHECK(!ResolveLiveConfiguration(Parse(R"({"model":"native"})"), f.context,
                                      &f.output, nullptr));
  Fixture g;
  g.context.count_history = [](const Json&, size_t*, ChatPlanError* error) {
    *error = {"tokenizer_unavailable", "session.input", 503};
    return Status::Fail("Tokenizer unavailable");
  };
  Q4T_CHECK(!g.Resolve() && g.validations == 0);
  Q4T_CHECK(g.error.code == "tokenizer_unavailable");
  return true;
}

Q4T_TEST(live_configuration_storage_and_session_identity) {
  Fixture f;
  auto oversized = Parse(R"({"model":"native"})");
  Set(&oversized, "instructions", String(std::string(16ULL << 20, 'x')));
  Q4T_CHECK(!f.Resolve(oversized));
  Q4T_CHECK(f.error.code == "item_validation_limit_exceeded");
  Q4T_CHECK(f.validations == 0 && f.output.str == "untouched");
  Q4T_CHECK(f.Resolve());
  Json resource = String("unchanged");
  for (const auto* id : {"", "live/path", "live?x"})
    Q4T_CHECK(
        !LiveSessionResource(f.output, id, 1795195000, &resource, &f.error));
  Q4T_CHECK(!LiveSessionResource(f.output, "live_1", 0, &resource, &f.error));
  Q4T_CHECK(!LiveSessionResource(f.output, "live_1", 9007199254740992ULL,
                                 &resource, &f.error));
  Q4T_CHECK(resource.str == "unchanged");
  Q4T_CHECK(
      LiveSessionResource(f.output, "live_1", 1795195000, &resource, &f.error));
  Q4T_CHECK(resource.GetString("id") == "live_1");
  Q4T_CHECK(resource.GetString("status") == "active");
  Q4T_CHECK(resource.GetInt("expires_at") == 1795195000);
  Q4T_CHECK(!f.output.Has("id") && !f.output.Has("status"));
  return true;
}

Q4T_TEST(live_responses_private_mcp_survives_sparse_update_and_replacement) {
  Fixture f;
  const auto raw = Parse(R"({"model":"native","delegation":{"type":"responses",
    "responses":{"model":"worker","tools":[{"type":"mcp","server_label":"docs",
    "server_url":"https://docs.example/mcp","authorization":"test-bearer",
    "headers":{"X-Fixture":"test-header"}},{"type":"function","name":"note",
    "parameters":{"type":"object","properties":{"headers":{"type":"string"}}},
    "strict":false}]}}})");
  Q4T_CHECK(!ValidateLiveShape(raw, LiveShape::kSessionStart, &f.error));
  Q4T_CHECK(f.Resolve(raw));
  Q4T_CHECK(f.Update(Parse(
      R"({"delegation":{"type":"responses","responses":{"instructions":"read documentation"}}})")));
  Json public_view, request;
  Q4T_CHECK(LiveSessionResource(f.output, "live_private", 1795200000,
                                &public_view, &f.error));
  const auto wire = OutputJson(public_view);
  Q4T_CHECK(wire.find("test-bearer") == wire.npos &&
            wire.find("X-Fixture") == wire.npos &&
            wire.find("test-header") == wire.npos &&
            wire.find("headers") != wire.npos);
  const auto settings = *f.output.Find("delegation")->Find("responses");
  Q4T_CHECK(MakeLiveResponsesRequest(settings, Array(), &request, &f.error));
  Q4T_CHECK(request.GetArray("tools")->array[0].GetString("authorization") ==
            "test-bearer");
  Q4T_CHECK(request.GetString("instructions") == "read documentation" &&
            !request.GetBool("store", true) && request.GetBool("stream") &&
            request.GetString("truncation") == "auto" &&
            request.GetString("model") == "worker");
  Q4T_CHECK(
      f.Update(Parse(R"({"delegation":{"type":"responses","responses":{"tools":[
    {"type":"mcp","server_label":"docs","server_url":"https://docs.example/mcp"}]}}})")));
  Q4T_CHECK(OutputJson(f.output).find("test-bearer") == wire.npos);
  Q4T_CHECK(!f.output.Find("delegation")
                 ->Find("responses")
                 ->GetArray("tools")
                 ->array[0]
                 .Has("headers"));
  return true;
}

Q4T_TEST(
    live_responses_projection_preserves_outer_shape_and_checks_duplicates) {
  Fixture f;
  auto start = Parse(R"({"model":"native","delegation":{"type":"responses",
    "responses":{"model":"worker","tools":[{"type":"mcp"}],
    "text":{"format":{"type":"json_object"},"verbosity":"low"}}}})");
  Q4T_CHECK(f.Resolve(start));
  auto& settings = Field(Field(start, "delegation"), "responses");
  Field(settings, "tools").array[0].object.emplace_back("type", String("mcp"));
  Q4T_CHECK(!f.Resolve(start));
  Field(settings, "tools").array = {Parse(R"({"type":"mcp"})"),
                                    Parse(R"({"type":"function","name":5})")};
  Q4T_CHECK(!f.Resolve(start));
  Field(settings, "tools").array.pop_back();
  Set(&Field(settings, "text"), "unknown", Number(1));
  Q4T_CHECK(!f.Resolve(start));
  return true;
}

Q4T_TEST(live_responses_request_rejects_override_limits_and_protected_fields) {
  ChatPlanError error;
  auto settings =
      Parse(R"({"model":"worker","instructions":null,"max_output_tokens":16})");
  Json request = String("unchanged");
  for (const auto* key : {"store", "stream", "input", "previous_response_id",
                          "background", "request_id"}) {
    auto bad = settings;
    Set(&bad, key, Json());
    Q4T_CHECK(!MakeLiveResponsesRequest(bad, Array(), &request, &error));
    Q4T_CHECK(request.str == "unchanged");
  }
  for (auto cap : {"15", "16.25"}) {
    auto bad = settings;
    Set(&bad, "max_output_tokens", Parse(cap));
    Q4T_CHECK(!MakeLiveResponsesRequest(bad, Array(), &request, &error));
  }
  Q4T_CHECK(MakeLiveResponsesRequest(settings, Array({String("sentinel")}),
                                     &request, &error));
  Q4T_CHECK(!request.Has("instructions") &&
            request.GetArray("input")->array[0].str == "sentinel");
  auto bad = Parse(R"({"model":"worker","tools":[{"type":"shell","environment":{
    "network_policy":{"domain_secrets":[{"domain":"test","value":"private-marker"}]}}}]})");
  Q4T_CHECK(!MakeLiveResponsesRequest(bad, Array(), &request, &error));
  Q4T_CHECK(error.param.ends_with(".network_policy.domain_secrets"));
  return true;
}

Q4T_TEST(live_responses_validation_scope_resources_and_credential_errors) {
  ChatPlanConfig config;
  config.model = "worker";
  ChatPlanError error;
  auto settings = Parse(R"({"model":"worker","tools":[{"type":"mcp",
    "server_label":"private","connector_id":"connector_googledrive"}]})");
  Q4T_CHECK(!CheckLiveResponsesSettings(settings, config, "client:a", &error));
  Q4T_CHECK(error.code == "mcp_target_mapping_required");
  auto policy = std::make_shared<McpDeploymentPolicy>();
  McpDeploymentTarget target;
  target.kind = "connector_id";
  target.id = "connector_googledrive";
  target.scopes = {"client:a"};
  target.generation = "r1";
  target.transport = "provider";
  policy->targets.push_back(target);
  config.mcp_policy = policy;
  Q4T_CHECK(CheckLiveResponsesSettings(settings, config, "client:a", &error));
  Q4T_CHECK(!CheckLiveResponsesSettings(settings, config, "client:b", &error) &&
            error.http_status == 403);
  auto& tool = Field(settings, "tools").array[0];
  Set(&tool, "headers", Parse(R"({"X-Private-Fixture":234})"));
  auto bad = CheckLiveResponsesSettings(settings, config, "client:a", &error);
  Q4T_CHECK(!bad && error.param == "tools[0].headers" &&
            bad.message().find("Fixture") == std::string::npos);
  settings = Parse(
      R"({"model":"worker","tools":[{"type":"file_search","vector_store_ids":["vs_a","vs_b"]}]})");
  std::vector<std::string> checked;
  bad = CheckLiveResponsesSettings(settings, config, "client:a", &error,
                                   [&](const auto& id) {
                                     checked.push_back(id);
                                     ObjectReply reply;
                                     if (id == "vs_b") {
                                       reply.http_status = 404;
                                       reply.code = "not_found";
                                       reply.message = "private-marker";
                                     }
                                     return reply;
                                   });
  Q4T_CHECK(!bad && checked == std::vector<std::string>({"vs_a", "vs_b"}) &&
            error.http_status == 404);
  Q4T_CHECK(error.param == "tools[0].vector_store_ids[1]" &&
            bad.message().find("private-marker") == std::string::npos);
  return true;
}

Q4T_TEST(live_media_policy_separates_frontend_controls_from_rtp_and_sideband) {
  LiveMediaPolicy policy;
  ChatPlanError error;
  const auto delta = Parse(R"({"type":"response.event","event":{
    "type":"response.output_text.delta"}})");
  const auto usage = Parse(R"({"type":"session.usage.updated"})");
  const auto rtp =
      Parse(R"({"type":"session.output_audio.delta","delta":"AA=="})");
  const auto restricted = Parse(R"({"data_channel":{
    "allowed_client_events":["session.close"],
    "allowed_server_events":[{"type":"response.event",
    "response_event":"response.output_text.delta"}]}})");
  Q4T_CHECK(policy.Configure(nullptr, &error) && policy.AllowsServer(usage));
  Q4T_CHECK(!policy.AllowsClient("session.start") &&
            !policy.AllowsClient("session.input_audio.append") &&
            !policy.AllowsServer(rtp));
  Q4T_CHECK(policy.Configure(&restricted, &error));
  Q4T_CHECK(policy.AllowsClient("session.close") &&
            !policy.AllowsClient("session.update"));
  Q4T_CHECK(policy.AllowsServer(delta) && !policy.AllowsServer(usage));
  Q4T_CHECK(!policy.AllowsServer(Parse(R"({"type":"response.event","event":{
    "type":"response.function_call_arguments.delta"}})")));
  for (
      const auto* input :
      {R"({"data_channel":{"allowed_server_events":[{"type":"response.event"}]}})",
       R"({"data_channel":{"allowed_server_events":[{"type":"error","response_event":"x"}]}})",
       R"({"data_channel":{"allowed_client_events":"none"}})"}) {
    const auto invalid = Parse(input);
    Q4T_CHECK(!policy.Configure(&invalid, &error) && error.http_status == 400);
    Q4T_CHECK(policy.AllowsServer(delta) && !policy.AllowsServer(usage));
  }
  std::string unicode;
  for (int i = 0; i < 256; ++i) unicode += "界";
  auto selector = [&](const std::string& type) {
    return Object({{"data_channel",
                    Object({{"allowed_server_events",
                             Array({Object({{"type", String(type)}})})}})}});
  };
  auto boundary = selector(unicode);
  Q4T_CHECK(policy.Configure(&boundary, &error));
  boundary = selector(unicode + "界");
  Q4T_CHECK(!policy.Configure(&boundary, &error));
  const auto empty = Parse(R"({"data_channel":{"allowed_client_events":[],
    "allowed_server_events":[]}})");
  Q4T_CHECK(policy.Configure(&empty, &error) &&
            !policy.AllowsClient("session.close") &&
            !policy.AllowsServer(delta));
  const auto all = Parse(R"({"data_channel":{"allowed_client_events":"all",
    "allowed_server_events":"all"}})");
  Q4T_CHECK(policy.Configure(&all, &error) && policy.AllowsServer(delta) &&
            policy.AllowsClient("session.close") && !policy.AllowsServer(rtp));
  return true;
}

Q4T_TEST(live_media_permissions_reject_257_before_startup) {
  Fixture f;
  f.context.transport = LiveTransport::kWebRtc;
  for (const auto* name : {"allowed_server_events", "allowed_client_events"}) {
    auto rows = Array();
    auto row = std::string_view(name) == "allowed_client_events"
                   ? String("session.close")
                   : Object({{"type", String("error")}});
    rows.array.resize(256, row);
    auto request = [&](const Json& permissions) {
      return Object({{"model", String("native")},
                     {"client", Object({{"data_channel",
                                         Object({{name, permissions}})}})}});
    };
    Q4T_CHECK(f.Resolve(request(rows)));
    rows.array.push_back(row);
    Q4T_CHECK(!f.Resolve(request(rows)) &&
              f.error.param ==
                  std::string("session.client.data_channel.") + name);
  }
  return true;
}
