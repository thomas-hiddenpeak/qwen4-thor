#include "q4t/server/chat_audio_inputs.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_plan.h"

#include <chrono>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:audio", Clock::now() + 30s, {}}; }
std::string Base64(std::string_view bytes) {
  constexpr auto alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string result;
  uint32_t value = 0;
  int bits = 0;
  for (unsigned char c : bytes) {
    value = (value << 8) | c;
    bits += 8;
    while (bits >= 6) {
      bits -= 6;
      result += alphabet[(value >> bits) & 63];
    }
  }
  if (bits) result += alphabet[(value << (6 - bits)) & 63];
  while (result.size() % 4) result += '=';
  return result;
}
std::string Wave() {
  std::string bytes = "RIFF";
  const auto number = [&](uint32_t value, int size) {
    for (int i = 0; i < size; ++i) bytes += char(value >> (8 * i));
  };
  number(38, 4);
  bytes += "WAVEfmt ";
  number(16, 4);
  number(1, 2);  // PCM16, one sample. Container-only unit fixture.
  number(1, 2);
  number(16000, 4);
  number(32000, 4);
  number(2, 2);
  number(16, 2);
  bytes += "data";
  number(2, 4);
  number(1234, 2);
  return bytes;
}
Json Audio(std::string bytes = Wave(), std::string format = "wav") {
  return Object({{"type", String("input_audio")},
                 {"input_audio", Object({{"data", String(Base64(bytes))},
                                         {"format", String(format)}})}});
}
Json Text(std::string text) {
  return Object({{"type", String("text")}, {"text", String(text)}});
}
Json Request(Json parts, std::string role = "user") {
  return Object({{"messages", Array({Object({{"role", String(role)},
                                             {"name", String("reader")},
                                             {"content", std::move(parts)}})})},
                 {"temperature", Number(0)}});
}
Json& Parts(Json& request) {
  return Field(Field(request, "messages").array[0], "content");
}
}  // namespace

Q4T_TEST(chat_audio_sources_keep_exact_binary_and_original_coordinates) {
  const std::string mpeg("ID3\4\0\0\0\0\0\0unit", 14);
  auto request = Request(
      Array({Text("before"), Audio(), Text("middle"), Audio(mpeg, "mp3")}));
  const auto original = OutputJson(request);
  ChatAudioInputs owner;
  Q4T_CHECK(HasChatAudioInputs(request));
  Q4T_CHECK(owner.Prepare(request, Access()).ok());
  Q4T_CHECK(owner.Inputs().size() == 2 && owner.Active());
  Q4T_CHECK(owner.Inputs()[0].message_index == 0);
  Q4T_CHECK(owner.Inputs()[0].part_index == 1);
  Q4T_CHECK(owner.Inputs()[1].part_index == 3);
  Q4T_CHECK(owner.Inputs()[0].source.bytes == Wave());
  Q4T_CHECK(owner.Inputs()[1].source.bytes == mpeg);
  Q4T_CHECK(owner.Inputs()[0].source.sha256 == Digest(Wave()));
  Q4T_CHECK(OutputJson(request) == original);
  return true;
}

Q4T_TEST(chat_audio_shape_and_role_errors_preserve_original_paths) {
  for (const auto role : {"assistant", "system", "developer", "tool"}) {
    ChatAudioInputs owner;
    const auto reply = owner.Prepare(Request(Array({Audio()}), role), Access());
    Q4T_CHECK(reply.http_status == 400);
    Q4T_CHECK(reply.value.GetString("param") == "messages[0].content[0]");
  }
  for (const auto& [field, path] : {std::pair{"type", ".type"},
                                    {"input_audio", ".input_audio"},
                                    {"data", ".input_audio.data"},
                                    {"format", ".input_audio.format"},
                                    {"extra", ".input_audio.extra"}}) {
    auto request = Request(Array({Audio()}));
    auto& part = Parts(request).array[0];
    if (std::string_view(field) == "type" ||
        std::string_view(field) == "input_audio")
      Set(&part, field, Number(1));
    else
      Set(&Field(part, "input_audio"), field, Number(1));
    ChatAudioInputs owner;
    const auto reply = owner.Prepare(request, Access());
    Q4T_CHECK(reply.http_status == 400 && !owner.Active());
    Q4T_CHECK(reply.value.GetString("param") ==
              std::string("messages[0].content[0]") + path);
  }
  return true;
}

Q4T_TEST(chat_audio_requires_canonical_inline_base64_without_io) {
  for (const auto data :
       {"", "a", "YR==", "YQ=", "YQ==\n", "====", "YQ--",
        "YQ==YQ==", "file:///etc/passwd", "https://example.invalid/voice.wav",
        "data:audio/wav;base64,YQ=="}) {
    auto request = Request(Array({Audio()}));
    Set(&Field(Parts(request).array[0], "input_audio"), "data", String(data));
    ChatAudioInputs owner;
    const auto reply = owner.Prepare(request, Access());
    Q4T_CHECK(reply.http_status == 400 && !owner.Active());
    Q4T_CHECK(reply.value.GetString("param") ==
              "messages[0].content[0].input_audio.data");
    Q4T_CHECK(owner.Prepare(Request(Array({Audio()})), Access()).ok());
  }
  return true;
}

Q4T_TEST(chat_audio_format_mismatch_cannot_select_another_codec) {
  for (const auto& part :
       {Audio(Wave(), "mp3"), Audio("ID3abcdefghijk", "wav"),
        Audio("fLaCabcdefghijk", "mp3"), Audio("not a wave file", "wav"),
        Audio(Wave(), "flac")}) {
    ChatAudioInputs owner;
    const auto reply = owner.Prepare(Request(Array({part})), Access());
    Q4T_CHECK(reply.http_status == 400 && !owner.Active());
    Q4T_CHECK(reply.value.GetString("param") ==
              "messages[0].content[0].input_audio.format");
  }
  return true;
}

Q4T_TEST(chat_audio_shared_source_and_part_limits_are_not_per_clip) {
  const auto one = Request(Array({Audio()}));
  const auto two = Request(Array({Audio(), Audio()}));
  ChatAudioInputs exact, bytes, parts;
  Q4T_CHECK(exact.Prepare(two, Access(), {2, 2 * Wave().size(), 16}).ok());
  Q4T_CHECK(bytes.Prepare(two, Access(), {2, 2 * Wave().size() - 1, 16})
                .http_status == 413);
  Q4T_CHECK(
      parts.Prepare(two, Access(), {1, 2 * Wave().size(), 16}).http_status ==
      413);
  Q4T_CHECK(!bytes.Active() && !parts.Active());
  Q4T_CHECK(bytes.Prepare(one, Access(), {1, Wave().size(), 16}).ok());
  ChatAudioInputs invalid;
  Q4T_CHECK(invalid.Prepare(one, Access(), {0, 1, 1}).http_status == 500);
  return true;
}

Q4T_TEST(chat_audio_cancel_and_deadline_keep_preparation_and_render_atomic) {
  const auto request = Request(Array({Audio()}));
  auto access = Access();
  size_t calls = 0;
  access.cancelled = [&] { return ++calls >= 3; };
  ChatAudioInputs owner;
  Q4T_CHECK(owner.Prepare(request, access).http_status == 499);
  Q4T_CHECK(!owner.Active());
  access = Access();
  access.deadline = Clock::now() - 1s;
  Q4T_CHECK(owner.Prepare(request, access).http_status == 408);
  Q4T_CHECK(owner.Prepare(request, Access()).ok());
  Q4T_CHECK(owner.Complete(0, "real result").ok());
  auto output = String("keep");
  Q4T_CHECK(owner.Render(request, access, &output).http_status == 408);
  Q4T_CHECK(output.IsString() && output.str == "keep");
  access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(owner.Render(request, access, &output).http_status == 499);
  Q4T_CHECK(output.str == "keep");
  return true;
}

Q4T_TEST(chat_audio_complete_projection_preserves_neighbors_and_silence) {
  auto request = Request(
      Array({Text("left"), Audio(), Text("middle"), Audio(), Text("right")}));
  Set(&Parts(request).array[1], "prompt_cache_breakpoint",
      Object({{"type", String("ephemeral")}}));
  const auto original = OutputJson(request);
  ChatAudioInputs owner;
  Q4T_CHECK(owner.Prepare(request, Access()).ok());
  Q4T_CHECK(owner.Complete(0, "真实内容").ok());
  auto output = String("keep");
  Q4T_CHECK(!owner.Render(request, Access(), &output).ok());
  Q4T_CHECK(output.IsString() && output.str == "keep");
  Q4T_CHECK(owner.Complete(1, "").ok());
  Q4T_CHECK(owner.Render(request, Access(), &output).ok());
  auto expected = request;
  auto& parts = Parts(expected).array;
  parts[1] = Text("真实内容");
  Set(&parts[1], "prompt_cache_breakpoint",
      Object({{"type", String("ephemeral")}}));
  parts[3] = Text("");
  Q4T_CHECK(OutputJson(output) == OutputJson(expected));
  Q4T_CHECK(!HasChatAudioInputs(output) && OutputJson(request) == original);
  return true;
}

Q4T_TEST(chat_audio_binding_and_result_failures_cannot_publish_partial_text) {
  auto request = Request(Array({Audio(), Audio()}));
  ChatAudioInputs owner;
  Q4T_CHECK(owner.Prepare(request, Access(), {2, 4096, 5}).ok());
  Q4T_CHECK(!owner.Prepare(request, Access()).ok());
  Q4T_CHECK(owner.Complete(0, std::string("bad\0", 4)).http_status == 502);
  Q4T_CHECK(owner.Complete(0, "\xff").http_status == 502);
  Q4T_CHECK(owner.Complete(0, "four").ok());
  Q4T_CHECK(!owner.Complete(0, "again").ok());
  Q4T_CHECK(!owner.Complete(2, "bad").ok());
  Q4T_CHECK(owner.Complete(1, "xx").http_status == 413);
  Q4T_CHECK(owner.Complete(1, "x").ok());
  for (int mutation = 0; mutation < 4; ++mutation) {
    auto changed = request;
    if (mutation == 0) Parts(changed).array.pop_back();
    if (mutation == 1) Parts(changed).array.push_back(Audio());
    if (mutation == 2)
      Set(&Field(Parts(changed).array[0], "input_audio"), "data",
          String(Base64("ID3changeddata")));
    if (mutation == 3)
      Set(&Field(changed, "messages").array[0], "role", String("system"));
    auto output = String("keep");
    Q4T_CHECK(!owner.Render(changed, Access(), &output).ok());
    Q4T_CHECK(output.IsString() && output.str == "keep");
  }
  Json output;
  Q4T_CHECK(owner.Render(request, Access(), &output).ok());
  return true;
}

Q4T_TEST(chat_audio_plan_requires_real_backend_and_explicit_preparation) {
  auto request = Request(Array({Audio()}));
  ChatPlanConfig config;
  config.preparing_audio_inputs = true;
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(error.code == "audio_backend_unavailable" &&
            error.http_status == 503);
  config.native_asr_enabled = true;
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  Set(&request, "modalities", Array({String("text")}));
  Set(&request, "audio", Json());
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  Set(&request, "modalities", Json());
  Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
  config.preparing_audio_inputs = false;
  Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(error.param == "messages[0].content[0]");
  return true;
}

Q4T_TEST(chat_audio_modalities_preserve_output_request_and_reject_bad_shapes) {
  auto request = Request(Array({Text("plain")}));
  ChatPlanConfig config;
  ChatRequestPlan plan;
  ChatPlanError error;
  for (const auto& value :
       {Array(), String("text"), Array({Number(1)}),
        Array({String("text"), String("text")}), Array({String("video")})}) {
    Set(&request, "modalities", value);
    Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
    Q4T_CHECK(error.param == "modalities" && error.http_status == 400);
  }
  Set(&request, "modalities", Array({String("text"), String("audio")}));
  Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(error.code == "audio_output_backend_unavailable" &&
            error.http_status == 503);
  Set(&request, "modalities", Array({String("text")}));
  Set(&request, "audio", Object());
  Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
  Q4T_CHECK(error.param == "audio" && error.http_status == 400);
  return true;
}

Q4T_TEST(chat_audio_usage_is_shared_once_across_choices_and_transports) {
  ChatChoices json(2, false), stream(2, true);
  for (auto* choices : {&json, &stream}) {
    Q4T_CHECK(choices->SetAudioPreparation({31, 7}, 20));
    for (int i = 0; i < 2; ++i)
      Q4T_CHECK(
          choices->Add(i, "{}", "stop", "null", 50, 9, 10, true, -1, 0, 0));
    Q4T_CHECK(choices->CompletionTokens() == 18);
  }
  Q4T_CHECK(json.UsageJson() == stream.UsageJson());
  Json usage;
  Q4T_CHECK(q4t::io::ParseJson(json.UsageJson(), &usage));
  Q4T_CHECK(usage.GetInt("prompt_tokens") == 81);
  Q4T_CHECK(usage.GetInt("completion_tokens") == 25);
  Q4T_CHECK(usage.GetInt("total_tokens") == 106);
  const auto* details = usage.Find("prompt_tokens_details");
  Q4T_CHECK(details && details->GetInt("audio_tokens") == 20 &&
            details->GetInt("cached_tokens") == 10);
  const auto* prepared = usage.Find("q4t_audio_preparation");
  Q4T_CHECK(prepared && prepared->GetInt("input_tokens") == 31 &&
            prepared->GetInt("output_tokens") == 7);
  return true;
}

Q4T_TEST(chat_audio_usage_rejects_unknown_double_or_overflow_accounting) {
  ChatChoices choices(1, false);
  Q4T_CHECK(!choices.SetAudioPreparation({}, 0));
  Q4T_CHECK(!choices.SetAudioPreparation({1, 1}, 2));
  Q4T_CHECK(!choices.SetAudioPreparation({1, 1, 1}, 1));
  Q4T_CHECK(choices.SetAudioPreparation({2, 3}, 1));
  const auto before = choices.UsageJson();
  Q4T_CHECK(!choices.SetAudioPreparation({2, 3}, 1));
  Q4T_CHECK(!choices.Add(0, "{}", "stop", "null", UINT64_MAX - 2, 1, 0, false,
                         -1, 0, 0));
  Q4T_CHECK(choices.UsageJson() == before);
  Q4T_CHECK(choices.Add(0, "{}", "stop", "null", 4, 5, 0, false, -1, 0, 0));
  Q4T_CHECK(!choices.SetAudioPreparation({2, 3}, 1));
  ChatChoices plain(1, true);
  Q4T_CHECK(plain.Add(0, "{}", "stop", "null", 4, 5, 0, false, -1, 0, 0));
  Q4T_CHECK(plain.UsageJson() ==
            "{\"prompt_tokens\":4,\"completion_tokens\":5,\"total_tokens\":9}");
  return true;
}

Q4T_TEST(chat_audio_budget_is_shared_upper_bound_without_model_execution) {
  auto request = Request(Array({Audio(), Audio()}));
  Set(&request, "max_tokens", Number(32));
  Set(&request, "n", Number(2));
  ChatAudioInputs inputs;
  Q4T_CHECK(inputs.Prepare(request, Access()).ok());
  ChatPlanConfig config;
  config.native_asr_enabled = true;
  config.model = "cpu-audio";
  config.max_len = 4096;
  ChatAudioBudget budget;
  Q4T_CHECK(PlanChatAudioBudget(request, config, inputs, &budget).ok());
  Q4T_CHECK(budget.prompt_capacity ==
            4096U + 2U * config.asr_limits.max_context_tokens);
  Q4T_CHECK(budget.output == 64 && budget.auxiliary_output == 0 &&
            budget.preparation_output ==
                uint64_t(config.asr_limits.max_output_tokens));
  ClientLimits limits;
  limits.max_prompt_tokens = 5000;
  limits.max_output_tokens = 128;
  config.client_limits = &limits;
  Q4T_CHECK(PlanChatAudioBudget(request, config, inputs, &budget).ok());
  Q4T_CHECK(budget.prompt_capacity == 5000 && budget.output == 64);
  Set(&request, "moderation", Object({{"model", String(config.model)}}));
  Q4T_CHECK(PlanChatAudioBudget(request, config, inputs, &budget).ok());
  Q4T_CHECK(budget.prompt_capacity == 5000 && budget.auxiliary_output == 1024);
  config.client_limits = nullptr;
  Q4T_CHECK(PlanChatAudioBudget(request, config, inputs, &budget).ok());
  Q4T_CHECK(budget.prompt_capacity ==
            3U * 4096U + 2U * config.asr_limits.max_context_tokens);
  const auto saved = budget;
  config.native_asr_enabled = false;
  Q4T_CHECK(PlanChatAudioBudget(request, config, inputs, &budget).http_status ==
            503);
  Q4T_CHECK(budget.prompt_capacity == saved.prompt_capacity &&
            budget.preparation_output == saved.preparation_output);
  return true;
}

Q4T_TEST(chat_audio_preflight_never_claims_transcription_or_exact_context_fit) {
  const auto request = Request(Array({Audio()}));
  ChatAudioInputs inputs;
  Q4T_CHECK(inputs.Prepare(request, Access()).ok());
  ChatPlanConfig config;
  config.native_asr_enabled = true;
  ChatAudioBudget budget;
  Q4T_CHECK(PlanChatAudioBudget(request, config, inputs, &budget).ok());
  const auto value = ChatAudioPreflight(inputs, budget);
  Q4T_CHECK(value.GetString("status") == "requires_audio_preparation");
  Q4T_CHECK(value.Find("input")->Find("tokens")->IsNull());
  Q4T_CHECK(value.Find("input")->GetString("mode") == "context_upper_bound");
  Q4T_CHECK(value.Find("audio")->GetInt("parts") == 1 &&
            !value.Find("audio")->GetBool("transcribed"));
  for (const auto name :
       {"audio_decoded", "media", "web_search", "template", "tokenizer",
        "budget", "request_registered", "gpu_executed"})
    Q4T_CHECK(value.Find("checks")->Find(name)->IsBool() &&
              !value.Find("checks")->GetBool(name));
  Q4T_CHECK(!value.GetBool("execution_guaranteed"));
  Json output;
  Q4T_CHECK(!inputs.Render(request, Access(), &output).ok());
  return true;
}
