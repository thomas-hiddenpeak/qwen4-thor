#include "q4t/server/voice_request.h"

#include <utility>
#include <vector>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
using Fields = std::vector<std::pair<std::string, std::string>>;
constexpr auto kType = "multipart/form-data; boundary=voice-test";
std::string Form(const Fields& fields, std::string_view key = {},
                 std::string_view bytes = "audio",
                 std::string_view mime = "audio/wav",
                 std::string_view filename = "../../private.wav") {
  std::string body;
  for (const auto& [name, value] : fields)
    body += "--voice-test\r\nContent-Disposition: form-data; name=\"" + name +
            "\"\r\n\r\n" + value + "\r\n";
  if (!key.empty()) {
    body += "--voice-test\r\nContent-Disposition: form-data; name=\"" +
            std::string(key) + "\"; filename=\"" + std::string(filename) +
            "\"\r\nContent-Type: " + std::string(mime) + "\r\n\r\n";
    body.append(bytes);
    body += "\r\n";
  }
  return body + "--voice-test--\r\n";
}
}  // namespace

Q4T_TEST(voice_sample_form_owns_exact_binary_and_defaults_type) {
  VoiceCreateRequest request;
  ChatPlanError error;
  const auto audio = std::string("a\0b", 3) + "\r\n--voice-test-not-boundary";
  Q4T_CHECK(ParseVoiceCreateRequest(
      kType,
      Form({{"name", "我的音色"}, {"consent", "cons_test"}}, "audio_sample",
           audio),
      &request, &error));
  Q4T_CHECK(request.type == VoiceType::kAudioSample &&
            request.audio_sample.bytes == audio &&
            request.audio_sample.filename == "../../private.wav" &&
            request.consent == "cons_test" && request.name == "我的音色");
  return true;
}

Q4T_TEST(voice_prompt_json_and_form_preserve_all_fields) {
  VoiceCreateRequest json, form;
  ChatPlanError error;
  Q4T_CHECK(ParseVoiceCreateRequest(
      "application/json; charset=utf-8",
      R"({"type":"prompt","name":"n","prompt":"A calm adult voice",
          "model":"2026-10-08","script_hint":"你好，这是我的声音。"})",
      &json, &error));
  Q4T_CHECK(ParseVoiceCreateRequest(kType,
                                    Form({{"type", "prompt"},
                                          {"name", "n"},
                                          {"prompt", json.prompt},
                                          {"model", json.model},
                                          {"script_hint", json.script_hint}}),
                                    &form, &error));
  Q4T_CHECK(json.type == VoiceType::kPrompt && form.prompt == json.prompt &&
            form.model == json.model && form.script_hint == json.script_hint);
  Q4T_CHECK(ParseVoiceCreateRequest(
      "application/json",
      R"({"type":"prompt","name":"n","prompt":"A calm voice"})", &json,
      &error));
  Q4T_CHECK(json.model == "auto");
  return true;
}

Q4T_TEST(voice_creation_rejects_conflicting_types_and_untrusted_evidence) {
  VoiceCreateRequest request;
  request.name = "unchanged";
  ChatPlanError error;
  for (const auto* json :
       {R"({"type":"prompt","name":"n","prompt":"p","consent":"c"})",
        R"({"type":"prompt","name":"n","prompt":"p","embedding":[]})",
        R"({"type":"prompt","name":"n","prompt":"p","owner":"a"})",
        R"({"type":"prompt","name":"n","prompt":"p","prompt":"q"})",
        R"({"type":"prompt","name":"n","prompt":null})",
        R"({"type":"","name":"n"})"}) {
    Q4T_CHECK(
        !ParseVoiceCreateRequest("application/json", json, &request, &error));
    Q4T_CHECK(request.name == "unchanged" && error.http_status == 400);
  }
  Q4T_CHECK(!ParseVoiceCreateRequest(
      kType,
      Form({{"name", "n"}, {"consent", "c"}, {"model", "auto"}},
           "audio_sample"),
      &request, &error));
  Q4T_CHECK(error.param == "model");
  return true;
}

Q4T_TEST(voice_consent_form_requires_real_file_and_preserves_language_tag) {
  VoiceConsentRequest request;
  ChatPlanError error;
  Q4T_CHECK(ParseVoiceConsentRequest(
      kType, Form({{"name", "Owner"}, {"language", "zh-Hans-CN"}}, "recording"),
      &request, &error));
  Q4T_CHECK(request.language == "zh-Hans-CN" &&
            request.recording.bytes == "audio");
  for (const auto& fields :
       {Fields{{"name", "n"}}, Fields{{"language", "en-US"}},
        Fields{{"name", "n"}, {"language", "en_US"}},
        Fields{{"name", "n"}, {"name", "b"}, {"language", "en-US"}}}) {
    Q4T_CHECK(!ParseVoiceConsentRequest(kType, Form(fields, "recording"),
                                        &request, &error));
    Q4T_CHECK(request.name == "Owner");
  }
  Q4T_CHECK(!ParseVoiceConsentRequest(
      kType, Form({{"name", "n"}, {"language", "en-US"}}), &request, &error));
  Q4T_CHECK(error.param == "recording");
  return true;
}

Q4T_TEST(voice_language_accepts_rfc_examples_and_rejects_duplicate_subtags) {
  for (const auto* tag :
       {"en-US", "zh-Hans-CN", "es-419", "zh-cmn-Hans-CN", "de-CH-1901",
        "sl-rozaj-biske-1994", "x-private", "en-a-myext-b-another-x-private",
        "en-Latn-US", "i-klingon", "sgn-BE-FR", "art-lojban", "qaa-Qaaa-QM"})
    Q4T_CHECK(WellFormedVoiceLanguage(tag));
  for (const auto* tag : {"", "en_Us", "a", "en-", "en--US", "en-a",
                          "en-a-foo-A-bar", "de-1901-1901", "zh-Hans-Hans",
                          "en-abcdefghi", "x", "en-x", "en-US-CN", "中文"})
    Q4T_CHECK(!WellFormedVoiceLanguage(tag));
  return true;
}

Q4T_TEST(voice_forms_reject_bad_utf8_nul_duplicate_and_unclosed_wire) {
  VoiceConsentRequest request;
  request.name = "previous";
  ChatPlanError error;
  for (const auto& name :
       {std::string("\xff"), std::string("a\0b", 3), std::string("  ")}) {
    Q4T_CHECK(!ParseVoiceConsentRequest(
        kType, Form({{"name", name}, {"language", "en"}}, "recording"),
        &request, &error));
    Q4T_CHECK(request.name == "previous");
  }
  auto form = Form({{"name", "n"}, {"language", "en"}}, "recording");
  form.resize(form.size() - 8);
  Q4T_CHECK(!ParseVoiceConsentRequest(kType, form, &request, &error));
  Q4T_CHECK(error.param == "body" && request.name == "previous");
  Q4T_CHECK(!ParseVoiceConsentRequest(
      kType,
      Form({{"name", "n"}, {"language", "en"}, {"recording", "first"}},
           "recording"),
      &request, &error));
  return true;
}

Q4T_TEST(voice_audio_limit_is_ten_mib_and_does_not_publish_partial_output) {
  VoiceConsentRequest request;
  ChatPlanError error;
  const Fields fields{{"name", "n"}, {"language", "en"}};
  Q4T_CHECK(ParseVoiceConsentRequest(
      kType, Form(fields, "recording", std::string(kVoiceAudioBytes, 'x')),
      &request, &error));
  Q4T_CHECK(request.recording.bytes.size() == kVoiceAudioBytes);
  Q4T_CHECK(!ParseVoiceConsentRequest(
      kType, Form(fields, "recording", std::string(kVoiceAudioBytes + 1, 'x')),
      &request, &error));
  Q4T_CHECK(error.http_status == 413 && error.param == "recording" &&
            request.recording.bytes.size() == kVoiceAudioBytes);
  Q4T_CHECK(!ParseVoiceConsentRequest(
      kType, std::string(kVoiceFormBytes + 1, 'x'), &request, &error));
  Q4T_CHECK(error.http_status == 413);
  return true;
}

Q4T_TEST(voice_mime_contract_uses_content_decoder_after_declared_type) {
  VoiceConsentRequest request;
  ChatPlanError error;
  for (const auto* mime :
       {"audio/mpeg", "audio/x-wav", "Audio/WAV", "audio/ogg; codecs=opus",
        "audio/aac", "audio/flac", "audio/webm", "audio/mp4",
        "application/octet-stream"})
    Q4T_CHECK(ParseVoiceConsentRequest(
        kType,
        Form({{"name", "n"}, {"language", "en"}}, "recording", "audio", mime),
        &request, &error));
  Q4T_CHECK(!ParseVoiceConsentRequest(kType,
                                      Form({{"name", "n"}, {"language", "en"}},
                                           "recording", "audio", "text/html"),
                                      &request, &error));
  return true;
}

Q4T_TEST(voice_text_limits_count_unicode_characters_and_reject_empty_hint) {
  std::string hint;
  for (int i = 0; i < 2000; ++i) hint += "你";
  VoiceCreateRequest request;
  ChatPlanError error;
  const auto form = [&](std::string s) {
    return Form({{"type", "prompt"},
                 {"name", "n"},
                 {"prompt", "p"},
                 {"script_hint", std::move(s)}});
  };
  Q4T_CHECK(ParseVoiceCreateRequest(kType, form(hint), &request, &error));
  for (const auto& invalid : {hint + "你", std::string("  "), std::string{}}) {
    Q4T_CHECK(!ParseVoiceCreateRequest(kType, form(invalid), &request, &error));
    Q4T_CHECK(error.param == "script_hint");
  }
  return true;
}

Q4T_TEST(voice_consent_update_can_only_change_name) {
  std::string name = "original";
  ChatPlanError error;
  Q4T_CHECK(ParseVoiceConsentUpdate("application/json", R"({"name":"new"})",
                                    &name, &error));
  Q4T_CHECK(name == "new");
  for (const auto* json : {R"({"name":"bad","language":"en"})",
                           R"({"name":"bad","recording":"data"})",
                           R"({"name":"a","name":"b"})", R"({})"}) {
    Q4T_CHECK(
        !ParseVoiceConsentUpdate("application/json", json, &name, &error));
    Q4T_CHECK(name == "new");
  }
  Q4T_CHECK(ParseVoiceConsentUpdate(kType, Form({{"name", "renamed"}}), &name,
                                    &error));
  Q4T_CHECK(name == "renamed");
  return true;
}
