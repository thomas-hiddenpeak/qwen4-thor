#include "q4t/server/live_configuration.h"

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
