#include "q4t/text/voice_consent.h"

#include <string>

#include <unicode/normalizer2.h>
#include <unicode/uchar.h>
#include <unicode/unistr.h>
#include <unicode/utf8.h>

namespace q4t::text {
namespace {
Status Normalize(std::string_view source, size_t limit, std::string* out) {
  if (source.empty() || source.size() > limit)
    return Status::Fail("Consent text is empty or exceeds its byte budget");
  // ICU's convenience UTF-8 constructor substitutes invalid input. Validate
  // scalars first and reject invisible formatting/control characters rather
  // than accidentally making adversarial text equal to an allowed phrase.
  for (int32_t i = 0; i < static_cast<int32_t>(source.size());) {
    UChar32 code;
    U8_NEXT(source.data(), i, static_cast<int32_t>(source.size()), code);
    if (code < 0 || code == 0 ||
        ((u_charType(code) == U_CONTROL_CHAR ||
          u_charType(code) == U_FORMAT_CHAR) &&
         !u_isUWhiteSpace(code)))
      return Status::Fail("Consent text contains invalid or hidden characters");
  }
  auto input = icu::UnicodeString::fromUTF8(
      icu::StringPiece(source.data(), static_cast<int32_t>(source.size())));
  UErrorCode error = U_ZERO_ERROR;
  const auto* nfc = icu::Normalizer2::getNFCInstance(error);
  if (U_FAILURE(error) || !nfc)
    return Status::Fail("Consent normalization unavailable");
  icu::UnicodeString canonical;
  nfc->normalize(input, canonical, error);
  if (U_FAILURE(error)) return Status::Fail("Consent normalization failed");
  canonical.foldCase(U_FOLD_CASE_DEFAULT);
  icu::UnicodeString folded;
  nfc->normalize(canonical, folded, error);
  if (U_FAILURE(error))
    return Status::Fail("Consent case normalization failed");
  icu::UnicodeString retained;
  for (int32_t i = 0; i < folded.length();) {
    const auto code = folded.char32At(i);
    i += U16_LENGTH(code);
    if (u_isUWhiteSpace(code) || u_ispunct(code)) continue;
    const auto type = u_charType(code);
    if (!u_isalnum(code) && type != U_NON_SPACING_MARK &&
        type != U_COMBINING_SPACING_MARK && type != U_ENCLOSING_MARK)
      return Status::Fail("Consent text contains a non-speech symbol");
    retained.append(code);
  }
  if (retained.isEmpty())
    return Status::Fail("Consent text contains no speech");
  retained.toUTF8String(*out);
  return {};
}
}  // namespace

Status MatchVoiceConsentTranscript(std::string_view transcript,
                                   std::string_view expected, bool* matches) {
  if (!matches) return Status::Fail("Missing consent comparison owner");
  std::string observed, trusted;
  auto status = Normalize(transcript, 64 * 1024, &observed);
  if (status) status = Normalize(expected, 16 * 1024, &trusted);
  if (!status) return status;
  *matches = observed == trusted;
  return {};
}
}  // namespace q4t::text
