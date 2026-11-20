#pragma once

#include <stdexcept>
#include <string>

#include "q4t/io/json.h"

namespace {
// Independent decoding oracle for the text model's source-bound ASR frame.
inline q4t::io::Json AudioTranscriptData(const std::string& text) {
  constexpr std::string_view end = "\n[End audio input transcript]\n\n";
  const auto begin = text.find("\n{");
  q4t::io::Json data;
  if (!text.starts_with("\n\n[Audio input transcript: ") ||
      !text.ends_with(end) || begin == text.npos ||
      !q4t::io::ParseJson(
          text.substr(begin + 1, text.size() - end.size() - begin - 1), &data,
          true) ||
      !data.IsObject() || data.object.size() != 2 || !data.Find("source") ||
      !data.Find("source")->IsString() || !data.Find("transcript") ||
      !data.Find("transcript")->IsString())
    throw std::runtime_error("missing source-bound ASR transcript frame");
  return data;
}
inline std::string NativeTranscript(const std::string& text) {
  return AudioTranscriptData(text).GetString("transcript");
}
}  // namespace
