#include "q4t/server/chat_audio_history.h"
#include "q4t/server/chat_audio_output.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/http_response.h"
#include "q4t/server/object_retention.h"

#include <chrono>
#include <cstdlib>
#include <ctime>
#include <filesystem>

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value))
    throw std::runtime_error("invalid fixture");
  return value;
}
Json Options(std::string format = "mp3", bool text = true) {
  return Object(
      {{"modalities", text ? Array({String("text"), String("audio")})
                           : Array({String("audio")})},
       {"audio",
        Object({{"format", String(format)}, {"voice", String("ryan")}})},
       {"messages", Array({Object({{"role", String("user")},
                                   {"content", String("Hello")}})})}});
}
Json History(std::string id) {
  return Object({{"messages",
                  Array({Object({{"role", String("assistant")},
                                 {"audio", Object({{"id", String(id)}})}})})}});
}
BinaryAccess Access(std::string scope = "client:audio") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
SpeechEncodingReceipt Receipt() { return {1920, 1920, 4, Digest("real"), {}}; }
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root =
        std::filesystem::absolute(env ? env : ".q4t-work/chat-audio-host");
    std::filesystem::create_directories(root);
    auto name = (root / "audio-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("fixture directory");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
};
}  // namespace

Q4T_TEST(chat_audio_output_options_keep_all_formats_and_actual_voice_identity) {
  for (const auto* format : {"wav", "aac", "mp3", "flac", "opus", "pcm16"}) {
    auto request = Options(format);
    ChatAudioOptions options;
    ChatPlanError error;
    Q4T_CHECK(ParseChatAudioOptions(request, &options, &error));
    Q4T_CHECK(options.enabled && options.text &&
              options.speech.voice == "ryan");
    Q4T_CHECK(ChatAudioFormatName(options.speech.format) == format);
    Q4T_CHECK(options.speech.input.empty() && options.speech.model.empty());
    ChatRequestPlan plan;
    ChatPlanConfig config;
    Q4T_CHECK(!PlanChatRequest(request, config, &plan, &error));
    Q4T_CHECK(error.http_status == 503);
    config.native_tts_enabled = true;
    config.native_tts_output_tokens = 2561;
    Q4T_CHECK(PlanChatRequest(request, config, &plan, &error));
    Q4T_CHECK(plan.audio_output.enabled);
    Q4T_CHECK(ChatOutputLimit(request, config) == 2817);
    Set(&request, "max_completion_tokens", Number(2048));
    Q4T_CHECK(ChatOutputLimit(request, config) == 2048);
  }
  auto custom = Options("pcm16", false);
  Set(&Field(custom, "audio"), "voice",
      Object({{"id", String("voice_private")}}));
  ChatAudioOptions options;
  ChatPlanError error;
  Q4T_CHECK(ParseChatAudioOptions(custom, &options, &error));
  Q4T_CHECK(options.enabled && !options.text && options.speech.voice_id &&
            options.speech.voice == "voice_private");
  return true;
}

Q4T_TEST(chat_audio_output_invalid_contract_is_atomic_and_has_original_paths) {
  ChatAudioOptions previous;
  ChatPlanError error;
  Q4T_CHECK(ParseChatAudioOptions(Options("wav"), &previous, &error));
  for (const auto* field : {"voice", "format", "unknown"}) {
    auto request = Options();
    Set(&Field(request, "audio"), field, Number(3));
    Q4T_CHECK(!ParseChatAudioOptions(request, &previous, &error));
    Q4T_CHECK(error.param == "audio." + std::string(field));
    Q4T_CHECK(previous.enabled && previous.speech.format == SpeechFormat::kWav);
  }
  for (const auto& value : {Array(), Array({String("audio"), String("audio")}),
                            Array({String("video")}), String("audio")}) {
    auto request = Options();
    Set(&request, "modalities", value);
    Q4T_CHECK(!ParseChatAudioOptions(request, &previous, &error));
    Q4T_CHECK(error.param == "modalities");
  }
  auto invalid = Options("pcm");
  Q4T_CHECK(!ParseChatAudioOptions(invalid, &previous, &error));
  Q4T_CHECK(error.param == "audio.format");
  Set(&invalid, "modalities", Array({String("text")}));
  Q4T_CHECK(!ParseChatAudioOptions(invalid, &previous, &error));
  Q4T_CHECK(error.param == "audio");
  return true;
}

Q4T_TEST(chat_audio_output_history_shape_never_bypasses_resolution) {
  auto request = History("audio_real");
  std::string id;
  ChatPlanError error;
  auto& message = Field(request, "messages").array[0];
  Q4T_CHECK(ChatAudioHistoryId(message, &id, &error) && id == "audio_real");
  std::string param, prompt;
  Q4T_CHECK(ValidateChatMessageProfile(*request.Find("messages"), &param));
  std::vector<q4t::server::VisionItem> media;
  Q4T_CHECK(!PrepareChatPrompt(request, {}, &prompt, &media, &param));
  for (const auto* role : {"user", "system", "developer", "tool"}) {
    Set(&message, "role", String(role));
    Q4T_CHECK(!ChatAudioHistoryId(message, &id, &error));
  }
  Set(&message, "role", String("assistant"));
  Set(&Field(message, "audio"), "data", String("attacker replacement"));
  Q4T_CHECK(!ChatAudioHistoryId(message, &id, &error));
  return true;
}

Q4T_TEST(chat_audio_output_attachment_preserves_tool_refusal_and_schema_text) {
  const auto message = Parse(
      R"({"role":"assistant","content":"{\"answer\":42}","tool_calls":[{"id":"call_1","type":"function","function":{"name":"read","arguments":"{}"}}],"reasoning_content":"private"})");
  ChatAudioOptions options;
  ChatPlanError error;
  Q4T_CHECK(ParseChatAudioOptions(Options("wav", false), &options, &error));
  Json result;
  Q4T_CHECK(AttachChatAudio(message, options, "audio_real", 123, "real",
                            message.GetString("content"), 4096, &result));
  Q4T_CHECK(result.Find("content")->IsNull());
  Q4T_CHECK(OutputJson(*result.Find("tool_calls")) ==
            OutputJson(*message.Find("tool_calls")));
  Q4T_CHECK(result.Find("audio")->GetString("transcript") == "{\"answer\":42}");
  Q4T_CHECK(result.Find("audio")->GetString("data") == "cmVhbA==");
  const auto prior = OutputJson(result);
  Q4T_CHECK(!AttachChatAudio(message, options, "audio_real", 123, "real",
                             "private", 4096, &result));
  Q4T_CHECK(!AttachChatAudio(message, options, "audio_real", 123, "real",
                             message.GetString("content"), 16, &result));
  Q4T_CHECK(OutputJson(result) == prior);
  const auto refusal =
      Parse(R"({"role":"assistant","content":null,"refusal":"No."})");
  Q4T_CHECK(AttachChatAudio(refusal, options, "audio_real", 123, "real", "No.",
                            4096, &result));
  Q4T_CHECK(result.GetString("refusal") == "No.");
  return true;
}

Q4T_TEST(
    chat_audio_output_stream_is_concatenable_at_arbitrary_byte_boundaries) {
  std::string bytes(65539, '\0');
  for (size_t i = 0; i < bytes.size(); ++i) bytes[i] = char(i);
  for (const size_t split : {1, 2, 3, 8672, 65535, 65536}) {
    ChatAudioDataStream stream;
    std::vector<std::string> events;
    std::string encoded;
    for (size_t offset = 0; offset < bytes.size(); offset += split) {
      Q4T_CHECK(stream.Append(std::string_view(bytes).substr(offset, split),
                              &events));
      for (const auto& event : events) {
        const auto part = Parse(event).Find("audio")->GetString("data");
        Q4T_CHECK(part.find('=') == part.npos);
        encoded += part;
      }
    }
    Q4T_CHECK(stream.Finish(&events));
    for (const auto& event : events)
      encoded += Parse(event).Find("audio")->GetString("data");
    std::string expected;
    Q4T_CHECK(q4t::io::EncodeBase64(bytes, 100000, &expected));
    Q4T_CHECK(encoded == expected);
    Q4T_CHECK(!stream.Finish(&events));
    Q4T_CHECK(!stream.Append("x", &events));
  }
  return true;
}

Q4T_TEST(chat_audio_output_expiry_chunk_omits_finish_then_normal_terminal) {
  std::string delta;
  Q4T_CHECK(ChatAudioExpiryDelta(123456, &delta));
  const auto event = detail::SseDeltaChunk("chatcmpl_1", "q4t", delta, "", 1,
                                           100, true, "null", false, true);
  const auto value = Parse(event.substr(6));
  const auto& choice = value.GetArray("choices")->array[0];
  Q4T_CHECK(!choice.Has("finish_reason"));
  Q4T_CHECK(choice.Find("delta")->Find("audio")->object.size() == 1);
  const auto normal =
      Parse(detail::SseDeltaChunk("chatcmpl_1", "q4t", "{}", "stop", 1, 100)
                .substr(6));
  Q4T_CHECK(normal.GetArray("choices")->array[0].GetString("finish_reason") ==
            "stop");
  return true;
}

Q4T_TEST(chat_audio_output_usage_adds_actual_codes_without_recounting_text) {
  ChatChoices choices(2, false);
  Q4T_CHECK(choices.SetAudioPreparation({31, 7}, 20));
  for (int i = 0; i < 2; ++i)
    Q4T_CHECK(choices.Add(i, "{}", "stop", "null", 50, 9, 10, true, 10, 2, 0));
  auto usage = Parse(choices.UsageJson());
  Q4T_CHECK(AddChatAudioUsage({12, 65}, 64, &usage));
  Q4T_CHECK(AddChatAudioUsage({13, 81}, 80, &usage));
  Q4T_CHECK(usage.GetInt("prompt_tokens") == 106);
  Q4T_CHECK(usage.GetInt("completion_tokens") == 171);
  Q4T_CHECK(usage.GetInt("total_tokens") == 277);
  Q4T_CHECK(usage.Find("prompt_tokens_details")->GetInt("audio_tokens") == 20);
  Q4T_CHECK(usage.Find("completion_tokens_details")->GetInt("audio_tokens") ==
            144);
  Q4T_CHECK(
      usage.Find("completion_tokens_details")->GetInt("reasoning_tokens") == 4);
  Q4T_CHECK(usage.Find("q4t_audio_synthesis")->GetInt("input_tokens") == 25);
  const auto prior = OutputJson(usage);
  Q4T_CHECK(!AddChatAudioUsage({1, 1}, 2, &usage));
  Q4T_CHECK(!AddChatAudioUsage({kActualUsageLimit, 1}, 1, &usage));
  Q4T_CHECK(OutputJson(usage) == prior);
  return true;
}

Q4T_TEST(chat_audio_output_history_survives_restart_and_is_client_private) {
  Fixture fixture;
  const auto id = NewObjectId(ObjectKind::kAudio);
  const auto now = std::time(nullptr);
  {
    ObjectStore objects(fixture.options);
    Q4T_CHECK(PublishChatAudioHistory(objects, Access(), id, now,
                                      "Meeting at three.", SpeechFormat::kMp3,
                                      Receipt())
                  .ok());
  }
  ObjectStore objects(fixture.options);
  const auto original = History(id);
  Json resolved;
  Q4T_CHECK(
      ResolveChatAudioHistory(&objects, Access(), original, &resolved).ok());
  const auto& message = resolved.GetArray("messages")->array[0];
  Q4T_CHECK(message.GetString("content") == "Meeting at three.");
  Q4T_CHECK(!message.Has("audio") && HasChatAudioHistory(original));
  std::string prompt, param;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(resolved, {}, &prompt, &media, &param));
  Q4T_CHECK(prompt.find("Meeting at three.") != prompt.npos);
  const auto before = OutputJson(resolved);
  const auto denied = ResolveChatAudioHistory(&objects, Access("client:other"),
                                              original, &resolved);
  Q4T_CHECK(denied.http_status == 404 && denied.code == "audio_not_found");
  Q4T_CHECK(OutputJson(resolved) == before);
  return true;
}

Q4T_TEST(chat_audio_output_history_preserves_additional_text_and_atomic_batch) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto id = NewObjectId(ObjectKind::kAudio);
  Q4T_CHECK(PublishChatAudioHistory(objects, Access(), id, std::time(nullptr),
                                    "spoken", SpeechFormat::kWav, Receipt())
                .ok());
  auto request = History(id);
  auto& message = Field(request, "messages").array[0];
  Set(&message, "content", String("spoken"));
  Json result;
  Q4T_CHECK(ResolveChatAudioHistory(&objects, Access(), request, &result).ok());
  Q4T_CHECK(result.GetArray("messages")->array[0].GetString("content") ==
            "spoken");
  Set(&message, "content", String("additional"));
  Q4T_CHECK(ResolveChatAudioHistory(&objects, Access(), request, &result).ok());
  Q4T_CHECK(result.GetArray("messages")->array[0].GetString("content") ==
            "additional\nspoken");
  const auto before = OutputJson(result);
  Field(request, "messages")
      .array.push_back(History("audio_missing").GetArray("messages")->array[0]);
  Q4T_CHECK(
      !ResolveChatAudioHistory(&objects, Access(), request, &result).ok());
  Q4T_CHECK(OutputJson(result) == before);
  return true;
}

Q4T_TEST(
    chat_audio_output_history_rejects_incomplete_receipt_and_expired_publish) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto id = NewObjectId(ObjectKind::kAudio);
  auto receipt = Receipt();
  receipt.sha256.clear();
  Q4T_CHECK(!PublishChatAudioHistory(objects, Access(), id, std::time(nullptr),
                                     "spoken", SpeechFormat::kPcm, receipt)
                 .ok());
  Q4T_CHECK(!PublishChatAudioHistory(
                 objects, Access(), id,
                 std::time(nullptr) - kChatAudioLifetimeSeconds - 1, "spoken",
                 SpeechFormat::kPcm, Receipt())
                 .ok());
  Json result;
  Q4T_CHECK(ResolveChatAudioHistory(&objects, Access(), History(id), &result)
                .http_status == 404);
  const auto info = InspectObjectRetention(
      "audio", Object({{"format", String("q4t.chat_audio.v1")},
                       {"expires_at", Number(123)},
                       {"transcript", String("spoken")}}));
  Q4T_CHECK(!info.uncertain && !info.active);
  return true;
}

Q4T_TEST(chat_audio_actual_utterance_keeps_fractional_tempo_and_voice) {
  for (double speed : {0.25, 0.5, 1.0, 1.5}) {
    SpeechRequest request;
    request.model = "native-fixture";
    request.input = "Actual generated text.";
    request.voice = "voice_cpu";
    request.voice_id = true;
    request.format = SpeechFormat::kPcm;
    request.speed = speed;
    request.instructions = "Read clearly.";
    SpeechRequest parsed;
    ChatPlanError error;
    Q4T_CHECK(ValidateChatSpeechRequest(request, &parsed, &error));
    Q4T_CHECK(parsed.input == request.input && parsed.voice_id &&
              parsed.voice == request.voice && parsed.model == request.model &&
              parsed.speed == speed && parsed.format == SpeechFormat::kPcm &&
              parsed.instructions == request.instructions);
    request.speed = 0;
    Q4T_CHECK(!ValidateChatSpeechRequest(request, &parsed, &error));
    Q4T_CHECK(parsed.speed == speed && parsed.input == request.input);
  }
  return true;
}
