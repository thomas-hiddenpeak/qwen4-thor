// Canonical byte encoding shared by stored files, tools and audio output.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::io {
inline constexpr std::string_view kBase64Alphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
inline Status EncodeBase64(std::string_view bytes, size_t max_output_bytes,
                           std::string* out) {
  // Divide before multiplying: both the bound and capacity calculation avoid
  // overflow, including an almost-SIZE_MAX caller budget.
  const size_t groups = bytes.size() / 3 + (bytes.size() % 3 != 0);
  if (!out || groups > max_output_bytes / 4)
    return Status::Fail("Base64 output exceeds its byte budget");
  std::string encoded;
  encoded.reserve(groups * 4);
  for (size_t i = 0; i < bytes.size(); i += 3) {
    const auto a = static_cast<uint8_t>(bytes[i]);
    const auto b =
        i + 1 < bytes.size() ? static_cast<uint8_t>(bytes[i + 1]) : 0;
    const auto c =
        i + 2 < bytes.size() ? static_cast<uint8_t>(bytes[i + 2]) : 0;
    encoded += kBase64Alphabet[a >> 2];
    encoded += kBase64Alphabet[((a & 3) << 4) | (b >> 4)];
    encoded += i + 1 < bytes.size()
                   ? kBase64Alphabet[((b & 15) << 2) | (c >> 6)]
                   : '=';
    encoded += i + 2 < bytes.size() ? kBase64Alphabet[c & 63] : '=';
  }
  *out = std::move(encoded);
  return {};
}
}  // namespace q4t::io
