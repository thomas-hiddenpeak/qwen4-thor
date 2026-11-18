#include "q4t/server/voice_verification.h"

#include <cmath>
#include <limits>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::audio::SpeakerEmbedding;
using q4t::audio::SpeakerWindow;
SpeakerEmbedding Axis(float first = 1, float second = 0) {
  SpeakerEmbedding e{};
  e[0] = first;
  e[1] = second;
  return e;
}
}  // namespace

Q4T_TEST(voice_consent_catalog_keeps_language_and_complete_wording) {
  for (auto tag : {"de", "en", "es", "fr", "hi", "id", "it", "ja", "ko", "nl",
                   "pl", "pt", "ru", "uk", "vi", "zh"}) {
    Q4T_CHECK(VoiceConsentPhrase(tag).size() > 40);
    Q4T_CHECK(VoiceConsentPhrase(std::string(tag) + "-Latn") ==
              VoiceConsentPhrase(tag));
  }
  Q4T_CHECK(VoiceConsentPhrase("EN-us") == VoiceConsentPhrase("en"));
  Q4T_CHECK(VoiceConsentPhrase("zh-Hant-TW") == VoiceConsentPhrase("zh"));
  Q4T_CHECK(
      VoiceConsentPhrase("en").starts_with("I am the owner of this voice"));
  Q4T_CHECK(VoiceConsentPhrase("zh").starts_with("我是此声音的拥有者"));
  for (auto tag : {"", "en_Us", "en-", "x-en", "xx", "en-a-a", "i-klingon"})
    Q4T_CHECK(VoiceConsentPhrase(tag).empty());
  return true;
}
Q4T_TEST(voice_identity_normalizes_and_weights_observed_speech_cores) {
  const std::array<SpeakerWindow, 2> windows{
      {{0, 200, 0, 100}, {0, 400, 100, 400}}};
  const std::array<SpeakerEmbedding, 2> e{Axis(7), Axis(8, 6)};
  std::vector<float> result;
  Q4T_CHECK(BuildVoiceIdentity(windows, e, 400, &result));
  const double norm = std::sqrt(340.0 * 340.0 + 180.0 * 180.0);
  Q4T_CHECK(result.size() == 192 && std::abs(result[0] - 340 / norm) < 1e-7 &&
            std::abs(result[1] - 180 / norm) < 1e-7);
  for (size_t i = 2; i < result.size(); ++i) Q4T_CHECK(result[i] == 0);
  return true;
}
Q4T_TEST(voice_identity_rejects_other_speaker_hidden_by_longer_primary) {
  const std::array<SpeakerWindow, 3> windows{
      {{0, 100, 0, 100}, {100, 200, 100, 200}, {200, 201, 200, 201}}};
  const std::array<SpeakerEmbedding, 3> e{Axis(100), Axis(1), Axis(0, 1)};
  std::vector<float> result{23};
  Q4T_CHECK(!BuildVoiceIdentity(windows, e, 201, &result));
  Q4T_CHECK(result == std::vector<float>{23});
  return true;
}
Q4T_TEST(voice_identity_rejects_nonfinite_empty_and_overlapping_evidence) {
  std::vector<SpeakerWindow> windows{{0, 200, 0, 100}, {0, 200, 100, 200}};
  std::vector<SpeakerEmbedding> e{Axis(), Axis()};
  std::vector<float> result{23};
  for (const float value : {std::numeric_limits<float>::quiet_NaN(),
                            std::numeric_limits<float>::infinity(), 0.F}) {
    e[1] = Axis(value);
    Q4T_CHECK(!BuildVoiceIdentity(windows, e, 200, &result));
  }
  e[1] = Axis();
  windows[1].core_begin_sample = 99;
  Q4T_CHECK(!BuildVoiceIdentity(windows, e, 200, &result));
  windows[1].core_begin_sample = 100;
  Q4T_CHECK(!BuildVoiceIdentity(windows, e, 199, &result));
  Q4T_CHECK(!BuildVoiceIdentity(windows, e, 60 * 16000 + 1, &result));
  Q4T_CHECK(!BuildVoiceIdentity({}, {}, 200, &result));
  Q4T_CHECK(!BuildVoiceIdentity(windows, {}, 200, &result));
  Q4T_CHECK(result == std::vector<float>{23});
  return true;
}
Q4T_TEST(voice_identity_cancellation_never_publishes_partial_aggregate) {
  const std::array<SpeakerWindow, 2> windows{
      {{0, 200, 0, 100}, {0, 200, 100, 200}}};
  const std::array<SpeakerEmbedding, 2> e{Axis(), Axis()};
  for (int stop : {1, 2, 3}) {
    int calls = 0;
    std::vector<float> result{42};
    Q4T_CHECK(!BuildVoiceIdentity(windows, e, 200, &result,
                                  [&] { return ++calls == stop; }));
    Q4T_CHECK(result == std::vector<float>{42} && calls == stop);
  }
  return true;
}
