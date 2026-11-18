#include "q4t/server/voice_api.h"

#include <cstdlib>
#include <filesystem>
#include <stdexcept>

#include "q4t/server/application_routes.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/voice_verification.h"
#include "q4t/server/tts_assets.h"
#include "q4t/trace/sha256.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
constexpr auto kConsent = "/v1/audio/voice_consents";
constexpr auto kVoices = "/v1/audio/voices";
constexpr auto kType = "multipart/form-data; boundary=api-voice";
BinaryAccess Access(std::string scope = "client:voice-a") {
  return {std::move(scope), std::chrono::steady_clock::now() + 10s, {}};
}
struct Fixture {
  ObjectStoreOptions options;
  int calls = 0;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root =
        std::filesystem::absolute(env ? env : ".q4t-work/voice-api-cpu");
    std::filesystem::create_directories(root);
    auto name = (root / "api-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("voice fixture");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
  VoiceVerifier Verifier() {
    return [&](const VoiceAudio& audio, std::string_view language,
               VoiceSampleEvidence* out, std::string* phrase) {
      ++calls;
      VoiceSampleEvidence e;
      e.source_sha256 = q4t::trace::HexDigest(q4t::trace::Sha256(
          {reinterpret_cast<const uint8_t*>(audio.bytes.data()),
           audio.bytes.size()}));
      e.duration_ns = 3000000000ULL;
      e.transcript = "CPU fixture evidence, no model executed";
      e.identity.assign(192, .125F);
      if (language.empty()) e.condition.assign(2048, .03125F);
      *phrase = VoiceConsentPhrase(language);
      *out = std::move(e);
      return ObjectReply{};
    };
  }
};
std::string Form(std::string consent = {}, std::string language = "en-US") {
  std::string body;
  for (const auto& [key, value] :
       {std::pair{std::string("name"), std::string("音色")},
        {consent.empty() ? "language" : "consent",
         consent.empty() ? language : consent}})
    body += "--api-voice\r\nContent-Disposition: form-data; name=\"" + key +
            "\"\r\n\r\n" + value + "\r\n";
  body += "--api-voice\r\nContent-Disposition: form-data; name=\"";
  body += consent.empty() ? "recording" : "audio_sample";
  body += "\"; filename=\"not-opened.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
  body.append("cpu\0fixture", 11);
  return body + "\r\n--api-voice--\r\n";
}
ObjectReply Call(ObjectStore& store, Fixture& fixture, std::string_view method,
                 std::string_view target, std::string_view body = {},
                 BinaryAccess access = Access(),
                 std::string_view type = kType) {
  return HandleVoiceRequest(store, method, target, type, body, access,
                            fixture.Verifier());
}
}  // namespace

Q4T_TEST(voice_api_sample_consent_lifecycle_persists_and_revokes) {
  Fixture f;
  std::string consent, voice;
  {
    ObjectStore objects(f.options);
    auto reply = Call(objects, f, "POST", kConsent, Form());
    Q4T_CHECK(reply.ok());
    consent = reply.value.GetString("id");
    Q4T_CHECK(reply.value.GetString("object") == "audio.voice_consent" &&
              reply.value.GetString("language") == "en-US");
    reply = Call(objects, f, "POST", kVoices, Form(consent));
    Q4T_CHECK(reply.ok());
    voice = reply.value.GetString("id");
    Q4T_CHECK(reply.value.GetString("type") == "audio_sample" && f.calls == 2);
  }
  ObjectStore objects(f.options);
  VoiceRegistry registry(objects);
  VoiceSynthesisBinding binding;
  Q4T_CHECK(registry.Resolve(voice, Access(), &binding).ok());
  Q4T_CHECK(binding.condition == std::vector<float>(2048, .03125F));
  auto reply = Call(objects, f, "POST", std::string(kConsent) + "/" + consent,
                    R"({"name":"renamed"})", Access(), "application/json");
  Q4T_CHECK(reply.ok() && reply.value.GetString("name") == "renamed");
  reply = Call(objects, f, "GET", std::string(kConsent) + "?limit=1");
  Q4T_CHECK(reply.ok() && reply.value.GetArray("data") &&
            reply.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(
      Call(objects, f, "DELETE", std::string(kConsent) + "/" + consent).ok());
  Q4T_CHECK(!registry.Resolve(voice, Access(), &binding).ok());
  Q4T_CHECK(
      Call(objects, f, "DELETE", std::string(kVoices) + "/" + voice).ok());
  Q4T_CHECK(f.calls == 2);
  return true;
}
Q4T_TEST(voice_api_cross_owner_is_rejected_before_audio_execution) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto consent =
      Call(objects, f, "POST", kConsent, Form()).value.GetString("id");
  Q4T_CHECK(!consent.empty() && f.calls == 1);
  for (auto method : {"GET", "POST", "DELETE"}) {
    auto reply = Call(objects, f, method, std::string(kConsent) + "/" + consent,
                      std::string(method) == "POST" ? R"({"name":"x"})" : "",
                      Access("client:voice-b"), "application/json");
    Q4T_CHECK(reply.http_status == 404);
  }
  Q4T_CHECK(
      Call(objects, f, "POST", kVoices, Form(consent), Access("client:voice-b"))
          .http_status == 404);
  Q4T_CHECK(Call(objects, f, "POST", kVoices, Form(consent), Access(""))
                .http_status == 503);
  Q4T_CHECK(f.calls == 1);
  return true;
}
Q4T_TEST(voice_api_revoke_during_verification_cannot_publish_voice) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto consent =
      Call(objects, f, "POST", kConsent, Form()).value.GetString("id");
  Q4T_CHECK(!consent.empty());
  auto normal = f.Verifier();
  bool deleted = false;
  VoiceVerifier revoke = [&](const VoiceAudio& audio, std::string_view language,
                             VoiceSampleEvidence* evidence,
                             std::string* phrase) {
    deleted = VoiceRegistry(objects).DeleteConsent(consent, Access()).ok();
    return normal(audio, language, evidence, phrase);
  };
  const auto reply = HandleVoiceRequest(objects, "POST", kVoices, kType,
                                        Form(consent), Access(), revoke);
  Q4T_CHECK(deleted && !reply.ok() && f.calls == 2);
  Q4T_CHECK(reply.http_status == 404 || reply.http_status == 409);
  return true;
}
Q4T_TEST(voice_api_closed_methods_queries_and_malformed_forms_do_not_execute) {
  Fixture f;
  ObjectStore objects(f.options);
  for (const auto target : {"/v1/audio/voice_consents?limit=0",
                            "/v1/audio/voice_consents?limit=101",
                            "/v1/audio/voice_consents?after=../x",
                            "/v1/audio/voice_consents?owner=b",
                            "/v1/audio/voice_consents?limit=1&limit=2"})
    Q4T_CHECK(Call(objects, f, "GET", target).http_status == 400);
  Q4T_CHECK(Call(objects, f, "GET", kVoices).http_status == 405);
  Q4T_CHECK(Call(objects, f, "POST", "/v1/audio/voices/voice_x", Form())
                .http_status == 405);
  Q4T_CHECK(Call(objects, f, "POST", "/v1/audio/voice_consents?", Form())
                .http_status == 400);
  Q4T_CHECK(Call(objects, f, "POST", kConsent, Form({}, "xx")).http_status ==
            400);
  Q4T_CHECK(!Call(objects, f, "POST", kConsent, "bad form").ok());
  Q4T_CHECK(Call(objects, f, "GET", kConsent, "body").http_status == 400);
  Q4T_CHECK(
      Call(objects, f, "GET", "/v1/audio/voice_consents/a/b").http_status ==
      400);
  Q4T_CHECK(f.calls == 0);
  return true;
}
Q4T_TEST(voice_api_failed_or_wrong_phrase_evidence_never_creates_consent) {
  Fixture f;
  ObjectStore objects(f.options);
  for (int mode : {0, 1, 2}) {
    auto normal = f.Verifier();
    VoiceVerifier broken =
        [&](const VoiceAudio& audio, std::string_view language,
            VoiceSampleEvidence* evidence, std::string* phrase) {
          auto reply = normal(audio, language, evidence, phrase);
          if (mode == 0) {
            reply.code = "voice_consent_mismatch";
            reply.http_status = 400;
          }
          if (mode == 1) *phrase = "I do not consent";
          if (mode == 2) evidence->source_sha256 = std::string(64, '0');
          return reply;
        };
    Q4T_CHECK(!HandleVoiceRequest(objects, "POST", kConsent, kType, Form(),
                                  Access(), broken)
                   .ok());
  }
  const auto list = Call(objects, f, "GET", kConsent);
  Q4T_CHECK(list.ok() && list.value.GetArray("data") &&
            list.value.GetArray("data")->array.empty());
  Q4T_CHECK(!HandleVoiceRequest(objects, "POST", kConsent, kType, Form(),
                                Access(), {})
                 .ok());
  return true;
}
Q4T_TEST(voice_routes_reserve_control_lane_without_granting_uploads) {
  for (auto method : {"GET", "POST", "DELETE"}) {
    Q4T_CHECK(VoiceMethodAllowed(method, "/v1/audio/voice_consents/cons_a"));
    Q4T_CHECK(IsVoiceMetadata(method, "/v1/audio/voice_consents/cons_a"));
    Q4T_CHECK(
        ClassifyHttpConnection(method, "/v1/audio/voice_consents/cons_a") ==
        HttpConnectionClass::kObservation);
  }
  for (auto target : {kConsent, kVoices}) {
    Q4T_CHECK(IsVoiceCreation("POST", target) &&
              IsBinaryUpload("POST", target));
    Q4T_CHECK(!IsVoiceMetadata("POST", target));
    Q4T_CHECK(ClassifyHttpConnection("POST", target) ==
              HttpConnectionClass::kBusiness);
  }
  for (auto target : {"/v1/audio/voices/a/b", "/v1/audio/voicesx/a",
                      "/v1/audio/voice_consents/", "/v1/audio/voices/a#x"})
    Q4T_CHECK(!VoiceMethodAllowed("DELETE", target));
  return true;
}
Q4T_TEST(voice_capabilities_distinguish_synthesis_from_registration_assets) {
  for (bool configured : {false, true}) {
    q4t::io::Json value;
    Q4T_CHECK(q4t::io::ParseJson(
        ChatCapabilitiesJson("test", false, false, 0, nullptr, false,
                             configured, false, configured, 64, configured,
                             false, false, true),
        &value));
    const auto* speech = value.Find("speech");
    const auto* registration = value.Find("voice_registration");
    Q4T_CHECK(speech && registration && speech->GetBool("enabled") &&
              speech->GetBool("registered_custom_voices") &&
              !speech->GetBool("builtin_enabled") &&
              speech->GetString("model") == kNativeTtsBaseModel &&
              speech->GetString("weight_sha256") == kNativeTtsBaseWeightSha256 &&
              speech->GetString("registered_voice_model") ==
                  kNativeTtsBaseModel);
    Q4T_CHECK(registration->GetBool("enabled") == configured &&
              !registration->GetBool("metadata_requires_healthy_model") &&
              !registration->GetBool("prompt_voice_design"));
  }
  return true;
}
