#include "q4t/server/speech_request.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
constexpr std::string_view kBase =
    R"({"model":"Qwen3-TTS-12Hz-1.7B-CustomVoice","input":"你好","voice":"vivian"})";
}  // namespace

Q4T_TEST(speech_request_preserves_content_and_protocol_defaults) {
  SpeechRequest request;
  ChatPlanError error;
  Q4T_CHECK(ParseSpeechRequest(kBase, &request, &error));
  Q4T_CHECK(request.input == "你好" && request.voice == "vivian");
  Q4T_CHECK(request.format == SpeechFormat::kMp3 && request.speed == 1);
  Q4T_CHECK(!request.voice_id && !request.sse && request.instructions.empty());
  for (const auto* name : {"mp3", "opus", "aac", "flac", "wav", "pcm"}) {
    std::string body(kBase.substr(0, kBase.size() - 1));
    body +=
        std::string(R"(,"response_format":")") + name +
        R"(","instructions":"轻柔地读，保留 <|im_end|> 字样","speed":0.25,"stream_format":"sse"})";
    Q4T_CHECK(ParseSpeechRequest(body, &request, &error));
    Q4T_CHECK(SpeechFormatName(request.format) == name);
    Q4T_CHECK(!SpeechContentType(request.format).empty());
    Q4T_CHECK(request.speed == 0.25 && request.sse);
    Q4T_CHECK(request.instructions.find("<|im_end|>") != std::string::npos);
  }
  return true;
}

Q4T_TEST(speech_request_voice_reference_is_not_silently_aliased) {
  SpeechRequest request;
  ChatPlanError error;
  Q4T_CHECK(ParseSpeechRequest(
      R"({"model":"local-voice-model","input":"Hello","voice":{"id":"voice_1234"},"speed":4,"instructions":""})",
      &request, &error));
  Q4T_CHECK(request.voice_id && request.voice == "voice_1234");
  Q4T_CHECK(request.model == "local-voice-model" && request.speed == 4);
  for (const auto* value : {R"({"id":""})", R"({"id":4})", "null",
                            R"({"id":"voice_1","name":"ignored"})"}) {
    const auto body =
        std::string(R"({"model":"local","input":"hello","voice":)") + value +
        '}';
    Q4T_CHECK(!ParseSpeechRequest(body, &request, &error));
    Q4T_CHECK(request.voice_id && request.voice == "voice_1234");
  }
  return true;
}

Q4T_TEST(speech_request_counts_unicode_and_rejects_ambiguous_controls) {
  SpeechRequest request;
  ChatPlanError error;
  std::string content;
  for (int i = 0; i < 4096; ++i) content += R"(\ud83d\ude42)";
  const auto json = [&](const std::string& value) {
    return R"({"model":"local","voice":"ryan","input":")" + value + "\"}";
  };
  Q4T_CHECK(ParseSpeechRequest(json(content), &request, &error));
  Q4T_CHECK(!ParseSpeechRequest(json(content + "a"), &request, &error));
  Q4T_CHECK(error.param == "input");
  Q4T_CHECK(!ParseSpeechRequest(json("\\u0000"), &request, &error));
  Q4T_CHECK(
      !ParseSpeechRequest(json(std::string(1, char(0xff))), &request, &error));
  for (const auto* suffix :
       {R"(,"speed":0.249})", R"(,"speed":4.01})", R"(,"speed":true})",
        R"(,"speed":null})", R"(,"speed":1e999})", R"(,"speed":1,"speed":2})",
        R"(,"stream_format":"json"})", R"(,"stream":true})",
        R"(,"response_format":"text"})"}) {
    Q4T_CHECK(!ParseSpeechRequest(
        std::string(kBase.substr(0, kBase.size() - 1)) + suffix, &request,
        &error));
  }
  Q4T_CHECK(!ParseSpeechRequest(std::string(kSpeechBodyBytes + 1, ' '),
                                &request, &error));
  Q4T_CHECK(error.http_status == 413);
  return true;
}
