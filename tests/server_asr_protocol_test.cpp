#include "q4t/server/asr_request.h"
#include "q4t/server/asr_response.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cmath>
#include <limits>
#include <stdexcept>

#include "q4t/audio/asr_config.h"
#include "q4t/io/base64.h"
#include "q4t/server/chatkit.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Fields = std::vector<std::pair<std::string, std::string>>;
constexpr auto kType = "multipart/form-data; boundary=q4t-audio-test";
std::string Form(const Fields& fields, std::string_view file = "binary") {
  std::string wire;
  for (const auto& [key, value] : fields)
    wire += "--q4t-audio-test\r\nContent-Disposition: form-data; name=\"" +
            key + "\"\r\n\r\n" + value + "\r\n";
  if (!file.empty()) {
    wire +=
        "--q4t-audio-test\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"../voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
    wire.append(file);
    wire += "\r\n";
  }
  return wire + "--q4t-audio-test--\r\n";
}
q4t::Status Parse(const Fields& fields, AsrRequest* out, ChatPlanError* error,
                  std::string_view file = "binary") {
  return ParseAsrRequest(kType, Form(fields, file), out, error);
}
Json Read(const std::string& wire) {
  Json value;
  if (!q4t::io::ParseJson(wire, &value))
    throw std::runtime_error("invalid JSON");
  return value;
}
SampledToken Token(int id = 7) { return {id, -9999, {}, -0.25}; }
}  // namespace

Q4T_TEST(chatkit_composer_audio_uses_native_asr_and_preserves_failed_input) {
  const std::string wav("RIFF\0\0\0\0WAVE", 12);
  const std::string webm("\x1a\x45\xdf\xa3" "recording");
  std::string encoded;
  auto params = [&](const std::string& audio, const char* mime) {
    if (!q4t::io::EncodeBase64(audio, 128, &encoded))
      throw std::runtime_error("fixture encoding");
    Json value;
    if (!q4t::io::ParseJson("{\"mime_type\":\"" + std::string(mime) +
          "\",\"audio_base64\":\"" + encoded + "\"}", &value))
      throw std::runtime_error("fixture JSON");
    return value;
  };
  AsrRequest request;
  ChatPlanError error;
  Q4T_CHECK(ParseChatKitTranscription(params(wav, "audio/wav"), &request,
                                      &error));
  Q4T_CHECK(request.file == wav && request.model == "qwen3-asr-0.6b" &&
            request.format == AsrFormat::kText && !request.stream);
  Q4T_CHECK(ParseChatKitTranscription(params(webm, "audio/webm;codecs=opus"),
                                      &request, &error));
  Q4T_CHECK(request.file == webm);
  Q4T_CHECK(!ParseChatKitTranscription(params(wav, "audio/webm"), &request,
                                       &error));
  Q4T_CHECK(error.http_status == 415 && error.param == "params.mime_type");
  for (const auto* raw : {
           R"({"mime_type":"audio/wav","audio_base64":""})",
           R"({"mime_type":"audio/wav","audio_base64":"a==="})",
           R"({"mime_type":42,"audio_base64":"YQ=="})",
           R"({"mime_type":"audio/wav","audio_base64":"YQ==","model":"x"})"}) {
    Json input;
    Q4T_CHECK(q4t::io::ParseJson(raw, &input));
    Q4T_CHECK(!ParseChatKitTranscription(input, &request, &error));
    Q4T_CHECK(error.http_status == 400 && request.file == webm);
  }
  return true;
}

Q4T_TEST(audio_translation_form_preserves_only_translation_input_fields) {
  ChatPlanError error;
  AsrRequest parsed;
  const auto valid = Form({{"model", "whisper-1"}, {"prompt", "formal English"},
                           {"temperature", "0.7"}, {"response_format", "verbose_json"}});
  Q4T_CHECK(ParseAsrRequest(kType, valid, &parsed, &error, true));
  Q4T_CHECK(parsed.model == "whisper-1" && parsed.prompt == "formal English" &&
            parsed.temperature == 0.7 && parsed.format == AsrFormat::kVerboseJson &&
            !parsed.segment_timestamps);
  for (const auto& [name, value] : Fields{{"language", "en"}, {"stream", "true"},
        {"timestamp_granularities[]", "word"}, {"response_format", "diarized_json"},
        {"prompt[]", "ignored"}, {"temperature", "1.1"}}) {
    Q4T_CHECK(!ParseAsrRequest(kType, Form({{"model", "whisper-1"}, {name, value}}),
                               &parsed, &error, true));
    Q4T_CHECK(error.http_status == 400 && !error.param.empty());
    Q4T_CHECK(parsed.prompt == "formal English" && parsed.temperature == 0.7);
  }
  return true;
}

Q4T_TEST(asr_form_preserves_binary_and_real_multipart_fields) {
  std::string audio(9000, '\0');
  audio.replace(4090, 27, "\r\n--q4t-audio-test-not-a-part");
  AsrRequest request;
  ChatPlanError error;
  Q4T_CHECK(Parse({{"model", "qwen3-asr-0.6b"},
                   {"language", "zh"},
                   {"prompt", "芯片 Q4T"},
                   {"stream", "true"},
                   {"include[]", "logprobs"},
                   {"temperature", "0.4"}},
                  &request, &error, audio));
  Q4T_CHECK(request.file == audio && request.filename == "../voice.wav");
  Q4T_CHECK(request.language == "zh" && request.prompt == "芯片 Q4T");
  Q4T_CHECK(request.stream && request.logprobs && request.temperature == 0.4);
  Q4T_CHECK(request.format == AsrFormat::kJson);
  return true;
}

Q4T_TEST(asr_form_required_duplicate_unknown_and_atomic_failure) {
  AsrRequest request;
  request.model = "previous";
  ChatPlanError error;
  for (const Fields& fields :
       {Fields{}, Fields{{"model", "a"}, {"model", "b"}},
        Fields{{"model", "a"}, {"unexpected", "x"}}, Fields{{"model[]", "a"}},
        Fields{{"model", "a"}, {"file", "duplicate"}}}) {
    Q4T_CHECK(!Parse(fields, &request, &error));
    Q4T_CHECK(request.model == "previous" && error.http_status == 400);
  }
  Q4T_CHECK(!Parse({{"model", "a"}}, &request, &error, {}));
  Q4T_CHECK(error.param == "file");
  Q4T_CHECK(!ParseAsrRequest("application/json", "{}", &request, &error));
  Q4T_CHECK(!ParseAsrRequest(kType, Form({{"model", "a"}}).substr(0, 90),
                             &request, &error));
  return true;
}

Q4T_TEST(asr_form_validates_scalar_types_utf8_and_parameter_conflicts) {
  AsrRequest request;
  ChatPlanError error;
  for (const auto& [field, value] : Fields{{"temperature", "NaN"},
                                           {"temperature", "1.1"},
                                           {"temperature", "-0.1"},
                                           {"temperature", "0.5junk"},
                                           {"stream", "1"},
                                           {"language", "English"},
                                           {"language", "EN"},
                                           {"prompt", std::string("x\0y", 3)},
                                           {"prompt", "\xff"},
                                           {"response_format", "xml"}}) {
    Q4T_CHECK(!Parse({{"model", "a"}, {field, value}}, &request, &error));
    Q4T_CHECK(error.param == field);
  }
  Q4T_CHECK(!Parse(
      {{"model", "a"}, {"response_format", "text"}, {"include[]", "logprobs"}},
      &request, &error));
  Q4T_CHECK(!Parse({{"model", "a"}, {"timestamp_granularities[]", "word"}},
                   &request, &error));
  Q4T_CHECK(Parse({{"model", "a"}, {"stream", "null"}}, &request, &error));
  Q4T_CHECK(!request.stream);
  return true;
}

Q4T_TEST(asr_form_preserves_advanced_contract_without_silent_drops) {
  AsrRequest r;
  ChatPlanError e;
  Q4T_CHECK(Parse({{"model", "a"},
                   {"response_format", "verbose_json"},
                   {"timestamp_granularities[]", "word"},
                   {"languages[]", "zh"},
                   {"languages[]", "en"},
                   {"keywords[]", "Thor"},
                   {"chunking_strategy[type]", "server_vad"},
                   {"chunking_strategy[threshold]", "0.7"},
                   {"chunking_strategy[prefix_padding_ms]", "120"},
                   {"chunking_strategy[silence_duration_ms]", "400"}},
                  &r, &e));
  Q4T_CHECK(r.word_timestamps && !r.segment_timestamps &&
            r.languages.size() == 2);
  Q4T_CHECK(r.keywords == std::vector<std::string>{"Thor"});
  Q4T_CHECK(r.chunking.mode == AsrChunking::Mode::kServerVad &&
            r.chunking.threshold == 0.7 &&
            r.chunking.prefix_padding_ms == 120 &&
            r.chunking.silence_duration_ms == 400);
  Q4T_CHECK(Parse({{"model", "a"},
                   {"response_format", "diarized_json"},
                   {"known_speaker_names[]", "Alice"},
                   {"known_speaker_references[]", "data:audio/wav;base64,AA=="},
                   {"chunking_strategy", "auto"}},
                  &r, &e));
  Q4T_CHECK(r.speaker_names.size() == 1 && r.speaker_references.size() == 1 &&
            r.chunking.mode == AsrChunking::Mode::kAuto);
  Q4T_CHECK(
      Parse({{"model", "a"},
             {"response_format", "verbose_json"},
             {"chunking_strategy", R"({"type":"server_vad","threshold":0})"}},
            &r, &e));
  Q4T_CHECK(r.segment_timestamps && r.chunking.threshold == 0);
  return true;
}

Q4T_TEST(asr_form_aligned_formats_keep_word_and_segment_requests_explicit) {
  AsrRequest request;
  ChatPlanError error;
  Q4T_CHECK(Parse({{"model", "a"},
                   {"response_format", "verbose_json"},
                   {"timestamp_granularities[]", "word"}},
                  &request, &error));
  Q4T_CHECK(request.word_timestamps && !request.segment_timestamps);
  Q4T_CHECK(Parse({{"model", "a"}, {"response_format", "verbose_json"}},
                  &request, &error));
  Q4T_CHECK(!request.word_timestamps && request.segment_timestamps);
  Q4T_CHECK(Parse({{"model", "a"},
                   {"response_format", "verbose_json"},
                   {"timestamp_granularities[]", "word"},
                   {"timestamp_granularities[]", "segment"}},
                  &request, &error));
  Q4T_CHECK(request.word_timestamps && request.segment_timestamps);
  return true;
}

Q4T_TEST(asr_form_subtitle_stream_is_rejected_without_overriding_format) {
  AsrRequest request;
  ChatPlanError error;
  request.model = "previous";
  for (const char* format : {"srt", "vtt", "verbose_json"}) {
    Q4T_CHECK(!Parse(
        {{"model", "a"}, {"response_format", format}, {"stream", "true"}},
        &request, &error));
    Q4T_CHECK(error.param == "stream" && request.model == "previous");
  }
  Q4T_CHECK(
      Parse({{"model", "a"}, {"response_format", "text"}, {"stream", "true"}},
            &request, &error));
  Q4T_CHECK(request.format == AsrFormat::kText && request.stream);
  return true;
}

Q4T_TEST(asr_form_rejects_nested_drift_duplicate_arrays_and_mixed_encodings) {
  AsrRequest r;
  ChatPlanError e;
  for (const auto& fields : std::vector<Fields>{
           {{"languages[]", "zh"}, {"languages[]", "zh"}},
           {{"include[]", "logprobs"}, {"include[]", "logprobs"}},
           {{"known_speaker_names[]", "Alice"}},
           {{"chunking_strategy[type]", "server_vad"},
            {"chunking_strategy", "auto"}},
           {{"chunking_strategy", "auto"},
            {"chunking_strategy[type]", "server_vad"}},
           {{"chunking_strategy",
             R"({"type":"server_vad","type":"server_vad"})"}},
           {{"chunking_strategy", R"({"type":"server_vad","threshold":2})"}},
           {{"chunking_strategy",
             R"({"type":"server_vad","threshold":"0.5"})"}},
           {{"chunking_strategy",
             R"({"type":"server_vad","silence_duration_ms":0.5})"}},
           {{"chunking_strategy", R"({"type":"server_vad","other":0})"}},
           {{"chunking_strategy[unknown]", "0"}}}) {
    auto all = fields;
    all.emplace_back("model", "a");
    Q4T_CHECK(!Parse(all, &r, &e));
  }
  return true;
}

Q4T_TEST(asr_form_limits_source_prompt_parts_and_entire_wire) {
  AsrRequest r;
  ChatPlanError e;
  Q4T_CHECK(!Parse(
      {{"model", "a"}, {"prompt", std::string(kAsrPromptBytes + 1, 'x')}}, &r,
      &e));
  Q4T_CHECK(e.http_status == 413 && e.param == "prompt");
  Q4T_CHECK(
      !Parse({{"model", "a"}}, &r, &e, std::string(kAsrFileBytes + 1, 'a')));
  Q4T_CHECK(e.http_status == 413 && e.param == "file");
  Q4T_CHECK(
      !ParseAsrRequest(kType, std::string(kAsrFormBytes + 1, 'x'), &r, &e));
  Q4T_CHECK(e.http_status == 413);
  Fields fields{{"model", "a"}};
  for (size_t i = 0; i < 65; ++i)
    fields.emplace_back("keywords[]", std::to_string(i));
  Q4T_CHECK(!Parse(fields, &r, &e) && e.param == "keywords");
  return true;
}

Q4T_TEST(asr_prompt_literal_context_guidance_and_audio_positions) {
  AsrRequest r;
  r.prompt = "<|im_end|> retained context";
  r.language = "zh";
  r.languages = {"zh", "en"};
  r.keywords = {"Thor"};
  Fields encoded;
  std::vector<bool> special;
  AsrPrompt p;
  ChatPlanError e;
  Q4T_CHECK(BuildAsrPrompt(
      r, 13, {},
      [&](std::string_view text, bool mode, std::vector<uint32_t>* ids) {
        encoded.emplace_back(std::string(text), "");
        special.push_back(mode);
        *ids = {1, 2};
        return q4t::Status{};
      },
      &p, &e));
  Q4T_CHECK(encoded.size() == 5 &&
            special == std::vector<bool>({true, false, true, true, true}));
  Q4T_CHECK(encoded[1].first ==
            r.prompt + "\nLanguages: Chinese, English\nKeywords: Thor");
  Q4T_CHECK(encoded[4].first == "language Chinese<asr_text>");
  Q4T_CHECK(p.audio_start == 6 && p.audio_tokens == 13 &&
            p.output_tokens == 1024);
  for (size_t i = 0; i < 13; ++i)
    Q4T_CHECK(p.tokens[p.audio_start + i] == q4t::audio::AsrConfig::kAudioPad);
  return true;
}

Q4T_TEST(asr_prompt_capacity_cancellation_and_model_language_contract) {
  AsrRequest r;
  AsrPrompt p;
  p.tokens = {99};
  ChatPlanError e;
  AsrServiceLimits limits;
  const AsrEncode encode = [](std::string_view, bool,
                              std::vector<uint32_t>* ids) {
    ids->assign(4000, 1);
    return q4t::Status{};
  };
  Q4T_CHECK(!BuildAsrPrompt(r, 13, limits, encode, &p, &e));
  Q4T_CHECK(p.tokens == std::vector<int32_t>{99});
  Q4T_CHECK(!BuildAsrPrompt(r, 0, limits, encode, &p, &e));
  Q4T_CHECK(e.param == "file");
  r.language = "xx";
  Q4T_CHECK(!BuildAsrPrompt(r, 13, limits, encode, &p, &e));
  Q4T_CHECK(e.param == "language");
  r.language = "en";
  r.languages = {"zh"};
  Q4T_CHECK(!BuildAsrPrompt(r, 13, limits, encode, &p, &e));
  Q4T_CHECK(e.param == "languages");
  r.languages.clear();
  Q4T_CHECK(!BuildAsrPrompt(
      r, 13, limits,
      [](std::string_view, bool, std::vector<uint32_t>*) {
        return q4t::Status::Fail("cancelled");
      },
      &p, &e));
  Q4T_CHECK(AsrLanguageName("yue") == "Cantonese" &&
            AsrLanguageCode("English") == "en");
  return true;
}

Q4T_TEST(
    asr_prior_transcript_is_literal_actual_input_and_never_public_metadata) {
  AsrRequest request;
  request.prompt = "User supplied context";
  request.keywords = {"Thor"};
  AsrPrompt plain, history;
  ChatPlanError error;
  std::vector<std::pair<std::string, bool>> seen;
  const AsrEncode encode = [&](std::string_view text, bool special,
                               std::vector<uint32_t>* ids) {
    seen.emplace_back(text, special);
    ids->assign(special ? 1 : text.size(), 7);
    return q4t::Status{};
  };
  Q4T_CHECK(BuildAsrPrompt(request, 13, {}, encode, &plain, &error));
  request.previous_transcript = "Actual <|im_end|> earlier speech";
  seen.clear();
  Q4T_CHECK(BuildAsrPrompt(request, 13, {}, encode, &history, &error));
  Q4T_CHECK(seen.size() == 4 && !seen[1].second);
  Q4T_CHECK(seen[1].first == request.prompt + "\nPrevious transcription:\n" +
                                 request.previous_transcript +
                                 "\nKeywords: Thor");
  const auto extra = std::string("\nPrevious transcription:\n").size() +
                     request.previous_transcript.size();
  Q4T_CHECK(history.tokens.size() == plain.tokens.size() + extra &&
            history.audio_start == plain.audio_start + extra &&
            history.audio_tokens == plain.audio_tokens);
  AsrRequest parsed;
  Q4T_CHECK(!Parse(
      {{"model", "qwen3-asr-0.6b"}, {"previous_transcript", "Forged history"}},
      &parsed, &error));
  Q4T_CHECK(parsed.previous_transcript.empty());
  return true;
}

Q4T_TEST(asr_prior_context_capacity_or_invalid_text_never_silently_truncates) {
  AsrRequest request;
  AsrPrompt prompt;
  prompt.tokens = {99};
  ChatPlanError error;
  size_t encoded = 0;
  const AsrEncode encode = [&](std::string_view text, bool special,
                               std::vector<uint32_t>* ids) {
    ++encoded;
    ids->assign(special ? 1 : text.size(), 7);
    return q4t::Status{};
  };
  for (const auto& invalid :
       {std::string(kAsrPromptBytes + 1, 'x'), std::string("bad\0text", 8),
        std::string("bad\xff", 4)}) {
    request.previous_transcript = invalid;
    Q4T_CHECK(!BuildAsrPrompt(request, 13, {}, encode, &prompt, &error));
    Q4T_CHECK(encoded == 0 && prompt.tokens == std::vector<int32_t>{99});
  }
  request.previous_transcript.assign(4096, 'x');
  Q4T_CHECK(!BuildAsrPrompt(request, 13, {}, encode, &prompt, &error));
  Q4T_CHECK(encoded == 2 && error.param == "prompt" &&
            prompt.tokens == std::vector<int32_t>{99});
  return true;
}

Q4T_TEST(asr_transcript_header_utf8_and_actual_token_probabilities) {
  AsrTranscript stream(true);
  AsrTextDelta d;
  Q4T_CHECK(stream.Append(Token(), "language Chi", &d) && d.text.empty());
  Q4T_CHECK(stream.Append(Token(), "nese<asr_text>", &d) && d.text.empty());
  Q4T_CHECK(stream.Append(Token(8), "\xe4", &d) && d.text.empty());
  Q4T_CHECK(stream.Append(Token(9), "\xb8\xad", &d) && d.text == "中");
  const auto rows = Read(d.logprobs);
  Q4T_CHECK(rows.IsArray() && rows.array.size() == 2);
  Q4T_CHECK(rows.array[0].GetArray("bytes")->array[0].number == 228);
  Q4T_CHECK(rows.array[0].GetNumber("logprob") == -0.25 &&
            !rows.array[0].Has("top_logprobs"));
  Q4T_CHECK(stream.Append(Token(10), "!  ", &d) && d.text == "!");
  Q4T_CHECK(stream.Finish(&d) && d.text.empty());
  std::string json;
  Q4T_CHECK(stream.ResultJson({25, 6}, 10, true, &json));
  const auto result = Read(json);
  Q4T_CHECK(result.GetString("type") == "transcript.text.done" &&
            result.GetString("text") == "中!");
  Q4T_CHECK(result.GetArray("logprobs")->array.size() == 3);
  Q4T_CHECK(result.Find("usage")->GetNumber("total_tokens") == 31);
  Q4T_CHECK(result.Find("usage")
                ->Find("input_token_details")
                ->GetInt("text_tokens") == 15);
  Q4T_CHECK(result.GetArray("languages")->array[0].GetString("code") == "zh");
  return true;
}

Q4T_TEST(
    asr_transcript_forced_language_and_whitespace_preserve_selected_bytes) {
  AsrTranscript stream(true, "en");
  AsrTextDelta d;
  Q4T_CHECK(stream.Append(Token(), "  Hello ", &d) && d.text == "Hello");
  Q4T_CHECK(Read(d.logprobs).array[0].GetString("token") == "  Hello ");
  Q4T_CHECK(stream.Append(Token(), " world\n", &d) && d.text == "  world");
  Q4T_CHECK(stream.Finish(&d));
  Q4T_CHECK(stream.Text() == "Hello  world" && stream.Language() == "en");
  std::string json;
  Q4T_CHECK(stream.ResultJson({20, 3}, 5, false, &json));
  Q4T_CHECK(!Read(json).Has("type") &&
            Read(json).GetArray("logprobs")->array.size() == 2);
  return true;
}

Q4T_TEST(asr_transcript_text_only_empty_and_malformed_header_boundaries) {
  for (const char* text : {"Hello world", "language", "", "<asr_text>"}) {
    AsrTranscript stream(false);
    AsrTextDelta d;
    Q4T_CHECK(stream.Append(Token(), text, &d) && stream.Finish(&d));
    Q4T_CHECK(stream.Text() ==
              (std::string_view(text) == "<asr_text>" ? "" : text));
  }
  for (const char* header : {"language English", "language Wrong<asr_text>"}) {
    AsrTranscript stream(false);
    AsrTextDelta d;
    const auto append = stream.Append(Token(), header, &d);
    Q4T_CHECK(!append || !stream.Finish(&d));
    Q4T_CHECK(!stream.Append(Token(), " text", &d));
    std::string json = "previous";
    Q4T_CHECK(!stream.ResultJson({1, 1}, 1, true, &json) && json == "previous");
  }
  return true;
}

Q4T_TEST(asr_transcript_utf8_repair_partial_scalar_and_single_terminal) {
  AsrTranscript stream(true, "en");
  AsrTextDelta d;
  Q4T_CHECK(stream.Append(Token(), "\xf0\x9f", &d) && d.text.empty());
  Q4T_CHECK(stream.Finish(&d) && d.text == "\xef\xbf\xbd");
  const auto rows = Read(d.logprobs);
  Q4T_CHECK(rows.array.size() == 1 &&
            rows.array[0].GetArray("bytes")->array.size() == 2);
  Q4T_CHECK(Read(stream.DeltaJson(d)).GetString("type") ==
            "transcript.text.delta");
  std::string json;
  Q4T_CHECK(!stream.ResultJson({1, 2}, 2, true, &json));
  Q4T_CHECK(stream.ResultJson({2, 2}, 2, true, &json));
  Q4T_CHECK(!stream.Finish(&d));
  Q4T_CHECK(!stream.Append(Token(), "late", &d));
  Q4T_CHECK(!stream.ResultJson({2, 2}, 2, true, &json));
  return true;
}

Q4T_TEST(asr_transcript_requires_eos_owner_and_finite_probabilities) {
  AsrTranscript stream(true, "en");
  AsrTextDelta d;
  std::string json = "previous";
  Q4T_CHECK(stream.Append(Token(), "partial", &d));
  Q4T_CHECK(!stream.ResultJson({1, 1}, 0, false, &json) && json == "previous");
  auto token = Token();
  token.raw_logprob = std::numeric_limits<double>::quiet_NaN();
  Q4T_CHECK(!stream.Append(token, "bad", &d));
  Q4T_CHECK(!stream.Finish(&d));
  return true;
}

Q4T_TEST(asr_adaptive_temperature_and_masked_full_head_probability) {
  AsrTemperature temperature(0);
  for (int i = 0; i < 8; ++i) temperature.Observe(-1.0);
  Q4T_CHECK(temperature.Value() == 0);
  for (int i = 0; i < 7; ++i) temperature.Observe(-2);
  Q4T_CHECK(temperature.Value() == 0);
  temperature.Observe(-2);
  Q4T_CHECK(temperature.Value() == 0.2);
  for (int i = 0; i < 100; ++i) temperature.Observe(-2);
  Q4T_CHECK(temperature.Value() == 1);
  AsrTemperature fixed(0.3);
  for (int i = 0; i < 100; ++i) fixed.Observe(-10);
  Q4T_CHECK(fixed.Value() == 0.3);
  SamplingOptions options;
  options.logprobs = true;
  TokenSampler sampler;
  Q4T_CHECK(!sampler.SetTemperature(0));
  Q4T_CHECK(sampler.Init(options, 3, 1));
  Q4T_CHECK(!sampler.SetTemperature(-1));
  Q4T_CHECK(sampler.SetTemperature(0));
  SampledToken token;
  const std::vector<uint16_t> logits(3, 0);
  const std::vector<uint8_t> mask{1, 0, 0};
  Q4T_CHECK(sampler.Select(logits, &token, mask));
  Q4T_CHECK(token.token == 0 &&
            std::abs(token.raw_logprob + std::log(3.0)) < 1e-12);
  return true;
}

Q4T_TEST(asr_stream_error_and_heap_failure_never_emit_chat_done_or_success) {
  for (bool heap : {false, true}) {
    int sockets[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    {
      detail::HttpResponseContext context(sockets[0]);
      context.SetTranscriptionStream();
      *context.State() = detail::ResponseState::kStreaming;
      if (heap)
        detail::SendExceptionResponse(sockets[0], *context.State(),
                                      detail::ExceptionKind::kHostMemory);
      else
        detail::SendError(sockets[0], 502, "incomplete", {},
                          "asr_output_limit");
      detail::SendExceptionResponse(sockets[0], *context.State());
    }
    close(sockets[0]);
    std::string wire;
    char buffer[4096];
    ssize_t count;
    while ((count = read(sockets[1], buffer, sizeof(buffer))) > 0)
      wire.append(buffer, count);
    close(sockets[1]);
    Q4T_CHECK(wire.starts_with("event: error\r\ndata: {\"type\":\"error\""));
    Q4T_CHECK(wire.find("[DONE]") == wire.npos &&
              wire.find("transcript.text.done") == wire.npos);
    Q4T_CHECK(wire.find("event: error", 1) == wire.npos);
    Q4T_CHECK(wire.find(heap ? "host_memory_exhausted" : "asr_output_limit") !=
              wire.npos);
  }
  return true;
}

Q4T_TEST(
    asr_capabilities_separate_configured_backend_and_remaining_native_work) {
  const auto absent = Read(ChatCapabilitiesJson("primary", false, false));
  const auto present = Read(
      ChatCapabilitiesJson("primary", false, false, 0, nullptr, false, true));
  Q4T_CHECK(!absent.Find("transcriptions")->GetBool("configured"));
  Q4T_CHECK(present.Find("transcriptions")->GetBool("configured"));
  for (const auto* capabilities : {&absent, &present}) {
    const auto* transcription = capabilities->Find("transcriptions");
    const auto* remaining = transcription->GetArray("remaining_native_work");
    Q4T_CHECK(remaining && remaining->array.size() == 1 &&
              remaining->array.front().type == Json::Type::kString &&
              remaining->array.front().str == "overlap_diarization");
    Q4T_CHECK(transcription->Find("alignment") &&
              transcription->Find("segments") && transcription->Find("vad"));
    Q4T_CHECK(!transcription->Find("alignment")->GetBool("configured") &&
              !transcription->Find("vad")->GetBool("configured"));
  }
  Q4T_CHECK(present.GetBool("implementation_only") &&
            !present.GetBool("release_attestation"));
  return true;
}
