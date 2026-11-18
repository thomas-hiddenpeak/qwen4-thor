// Bounded waveform producer -> tempo -> encoding -> caller-owned transport.
#pragma once

#include <functional>
#include <optional>

#include "q4t/server/actual_usage.h"
#include "q4t/server/speech_codec_stream.h"

namespace q4t::server {
struct SpeechPcmSource {
  // Each call returns <=24000 actual 24kHz samples; empty is only legal at EOF.
  // This may perform bounded GPU steps, releasing its lock before returning.
  std::function<ObjectReply(std::vector<float>*, bool*)> next;
  // WAV requires a natural, final total by the first returned PCM chunk.
  // Other formats may leave this unknown until synthesis ends.
  std::function<std::optional<size_t>()> final_samples;
};
struct SpeechEncodingReceipt {
  size_t input_samples = 0, output_samples = 0, bytes = 0;
  std::string sha256;
  io::Json codec;
};
// The sink must consume bytes before returning. A failed source/codec/sink
// never publishes a receipt; preceding output cannot be declared complete.
ObjectReply WriteSpeechAudio(
    const SpeechRequest& request, size_t max_input_samples,
    const SpeechEncodeLimits& limits, const BinaryAccess& access,
    const SpeechPcmSource& source,
    const std::function<ObjectReply(std::string_view)>& sink,
    SpeechEncodingReceipt* receipt, GrammarControl control = {});
Status SpeechAudioDelta(std::string_view bytes, std::string* out);
Status SpeechAudioDone(const ActualUsage& usage, std::string* out);
}  // namespace q4t::server
