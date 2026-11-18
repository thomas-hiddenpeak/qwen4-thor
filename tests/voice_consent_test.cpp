#include "q4t/text/voice_consent.h"

#include <string>

#include "q4t/test.h"

namespace {
using q4t::text::MatchVoiceConsentTranscript;
constexpr auto kEnglish =
    "I am the owner of this voice and I consent to OpenAI using this voice "
    "to create a synthetic voice model.";
constexpr auto kChinese =
    "我是此声音的拥有者并授权OpenAI使用此声音创建语音合成模型";
}  // namespace

Q4T_TEST(voice_consent_matches_case_spacing_and_punctuation_only) {
  bool matches = false;
  Q4T_CHECK(MatchVoiceConsentTranscript(
      "I AM the owner of this voice, and I consent to Open AI using this "
      "voice to create a synthetic voice model!",
      kEnglish, &matches));
  Q4T_CHECK(matches);
  Q4T_CHECK(MatchVoiceConsentTranscript(
      "我是此声音的拥有者，并授权 OpenAI 使用此声音创建语音合成模型。",
      kChinese, &matches));
  Q4T_CHECK(matches);
  return true;
}

Q4T_TEST(voice_consent_preserves_negation_order_and_additional_speech) {
  bool matches = true;
  for (const auto& observed :
       {std::string("I do not consent to this recording."),
        std::string("Do not say: ") + kEnglish,
        std::string(kEnglish) + " I withdraw my consent.",
        std::string(
            "此声音的拥有者不是我，并授权OpenAI使用此声音创建语音合成模型"),
        std::string("I consent to OpenAI. I am the owner of this voice.")}) {
    Q4T_CHECK(MatchVoiceConsentTranscript(observed, kEnglish, &matches));
    Q4T_CHECK(!matches);
  }
  Q4T_CHECK(MatchVoiceConsentTranscript(
      "我是此声音的拥有者但不授权OpenAI使用此声音创建语音合成模型", kChinese,
      &matches));
  Q4T_CHECK(!matches);
  return true;
}

Q4T_TEST(voice_consent_canonical_unicode_case_keeps_diacritics) {
  bool matches = false;
  Q4T_CHECK(
      MatchVoiceConsentTranscript("E\xCC\x81L\xC3\x88VE", "élève", &matches));
  Q4T_CHECK(matches);
  Q4T_CHECK(MatchVoiceConsentTranscript("eleve", "élève", &matches));
  Q4T_CHECK(!matches);
  Q4T_CHECK(MatchVoiceConsentTranscript("STRASSE", "Straße", &matches));
  Q4T_CHECK(matches);
  return true;
}

Q4T_TEST(voice_consent_combining_vowels_and_digits_are_not_discarded) {
  bool matches = false;
  Q4T_CHECK(MatchVoiceConsentTranscript("आवाज", "आवाज", &matches));
  Q4T_CHECK(matches);
  Q4T_CHECK(MatchVoiceConsentTranscript("आवज", "आवाज", &matches));
  Q4T_CHECK(!matches);
  Q4T_CHECK(MatchVoiceConsentTranscript("version 12", "version 21", &matches));
  Q4T_CHECK(!matches);
  return true;
}

Q4T_TEST(voice_consent_confusable_letters_do_not_gain_permission) {
  bool matches = true;
  // The first o is Cyrillic, not a differently cased Latin letter.
  Q4T_CHECK(MatchVoiceConsentTranscript("оwner", "owner", &matches));
  Q4T_CHECK(!matches);
  Q4T_CHECK(MatchVoiceConsentTranscript("ＯｐｅｎＡＩ", "OpenAI", &matches));
  Q4T_CHECK(!matches);
  return true;
}

Q4T_TEST(voice_consent_hidden_control_and_malformed_utf8_fail_atomically) {
  for (const auto& text :
       {std::string("\xff"), std::string("a\0b", 3),
        std::string("owner\xE2\x80\x8B"), std::string("\xE2\x80\xAEowner"),
        std::string("owner\x7f"), std::string("😀")}) {
    bool matches = true;
    Q4T_CHECK(!MatchVoiceConsentTranscript(text, "owner", &matches));
    Q4T_CHECK(matches);
  }
  return true;
}

Q4T_TEST(voice_consent_empty_oversized_and_invalid_expected_fail_atomically) {
  bool matches = true;
  for (const auto& text : {std::string{}, std::string(" "), std::string("!!!"),
                           std::string(64 * 1024 + 1, 'x')}) {
    Q4T_CHECK(!MatchVoiceConsentTranscript(text, "owner", &matches));
    Q4T_CHECK(matches);
  }
  Q4T_CHECK(!MatchVoiceConsentTranscript("owner", "\xff", &matches));
  Q4T_CHECK(!MatchVoiceConsentTranscript(
      "owner", std::string(16 * 1024 + 1, 'x'), &matches));
  Q4T_CHECK(!MatchVoiceConsentTranscript("owner", "owner", nullptr));
  Q4T_CHECK(matches);
  return true;
}

Q4T_TEST(voice_consent_word_substitution_and_truncation_are_mismatches) {
  std::string changed = kEnglish;
  changed.replace(changed.find("owner"), 5, "user");
  bool matches = true;
  Q4T_CHECK(MatchVoiceConsentTranscript(changed, kEnglish, &matches));
  Q4T_CHECK(!matches);
  Q4T_CHECK(MatchVoiceConsentTranscript(std::string(kEnglish).substr(0, 50),
                                        kEnglish, &matches));
  Q4T_CHECK(!matches);
  return true;
}
