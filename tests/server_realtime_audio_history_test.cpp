#include "q4t/server/realtime_audio_history.h"

#include <algorithm>
#include <array>
#include <limits>

#include "q4t/test.h"
#include "q4t/server/realtime_noise_input.h"

namespace {
using namespace q4t::server;
const RealtimeTranscriptTicket kTicket{"sess_audio", "item_a", 1};
const RealtimeTranscriptTicket kNext{"sess_audio", "item_b", 2};
const std::array<uint8_t, 8> kBytes{0, 32, 0, 64, 0, 128, 255, 127};
const RealtimeAudioItem kItem{
    1, 1, 0, 12, 4, 8, {RealtimeAudioEncoding::kPcm16, 16000}};
std::array<std::span<const uint8_t>, 2> Split(size_t at) {
  return {std::span(kBytes).first(at), std::span(kBytes).subspan(at)};
}
}  // namespace

Q4T_TEST(realtime_audio_history_manual_pcm_survives_source_and_index_release) {
  RealtimeAudioHistory history("sess_audio", 32);
  std::shared_ptr<RealtimeAudioRecord> audio;
  auto source = kBytes;
  Q4T_CHECK(history.Decode(kTicket, kItem, {source, {}}, 16, &audio));
  Q4T_CHECK(history.ChargedBytes() == 16 && history.Items() == 1);
  source.fill(0);
  const auto pcm = audio->Samples();
  Q4T_CHECK(pcm.size() == 4 && pcm[0] == .25f && pcm[1] == .5f &&
            pcm[2] == -1 && pcm[3] == 32767.0f / 32768);
  Q4T_CHECK(!audio->Stream());
  Q4T_CHECK(history.Erase(kTicket) && !history.Find(kTicket));
  Q4T_CHECK(history.Items() == 0 && history.ChargedBytes() == 16 &&
            audio->Samples().data() == pcm.data());
  audio.reset();
  Q4T_CHECK(history.ChargedBytes() == 0);
  return true;
}

Q4T_TEST(realtime_audio_history_exact_decoder_reused_across_ring_splits) {
  for (const auto format :
       {RealtimeAudioFormat{},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 8000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kALaw, 8000}}) {
    auto item = kItem;
    item.format = format;
    item.samples = format.encoding == RealtimeAudioEncoding::kPcm16 ? 4 : 8;
    item.end_ticks = item.samples * (48000 / format.rate);
    std::vector<float> expected;
    Q4T_CHECK(DecodeRealtimePcm(item, Split(8), 32, &expected));
    for (size_t split = 0; split <= kBytes.size(); ++split) {
      RealtimeAudioHistory history("sess_audio", 128);
      std::shared_ptr<RealtimeAudioRecord> audio;
      Q4T_CHECK(history.Decode(kTicket, item, Split(split), 32, &audio));
      Q4T_CHECK(std::ranges::equal(audio->Samples(), expected));
    }
  }
  return true;
}

Q4T_TEST(realtime_audio_history_budget_includes_erased_borrowers) {
  RealtimeAudioHistory history("sess_audio", 16);
  std::shared_ptr<RealtimeAudioRecord> held, next;
  Q4T_CHECK(history.Decode(kTicket, kItem, Split(3), 16, &held));
  Q4T_CHECK(history.Erase(kTicket));
  Q4T_CHECK(!history.Decode(kNext, kItem, Split(3), 16, &next));
  Q4T_CHECK(!next && history.Items() == 0 && history.ChargedBytes() == 16);
  held.reset();
  Q4T_CHECK(history.Decode(kNext, kItem, Split(3), 16, &next));
  Q4T_CHECK(history.ChargedBytes() == 16);
  return true;
}

Q4T_TEST(realtime_audio_history_failed_decode_releases_reservation_atomically) {
  RealtimeAudioHistory history("sess_audio", 32);
  std::shared_ptr<RealtimeAudioRecord> original;
  Q4T_CHECK(history.Decode(kTicket, kItem, Split(2), 16, &original));
  auto output = original;
  auto bad = kItem;
  ++bad.end_ticks;
  Q4T_CHECK(!history.Decode(kNext, bad, Split(2), 16, &output));
  Q4T_CHECK(!history.Decode(kNext, kItem, Split(2), 16, &output,
                            [] { return true; }));
  Q4T_CHECK(output == original && history.Items() == 1 &&
            history.ChargedBytes() == 16);
  Q4T_CHECK(history.Decode(kNext, kItem, Split(2), 16, &output));
  Q4T_CHECK(output != original && history.ChargedBytes() == 32);
  return true;
}

Q4T_TEST(
    realtime_audio_history_stream_alias_keeps_charge_after_index_deletion) {
  RealtimeAudioHistory history("sess_audio", 32);
  std::shared_ptr<RealtimeAudioRecord> record, next;
  Q4T_CHECK(history.BeginStream(kTicket, 7, 8, 4, &record));
  auto input = record->Stream();
  Q4T_CHECK(input && record->Samples().empty());
  Q4T_CHECK(input->Append(7, std::array<float, 4>{.1f, .2f, 1.2f, -.5f}));
  Q4T_CHECK(record->Samples().empty() && input->Seal(7));
  const auto samples = record->Samples();
  Q4T_CHECK(samples.size() == 4 && samples[2] == 1.2f);
  Q4T_CHECK(history.Erase(kTicket) && input->Cancel(7));
  record.reset();
  Q4T_CHECK(history.ChargedBytes() == 32 && samples[3] == -.5f);
  Q4T_CHECK(!history.BeginStream(kNext, 8, 8, 4, &next));
  input.reset();
  Q4T_CHECK(history.ChargedBytes() == 0 &&
            history.BeginStream(kNext, 8, 8, 4, &next));
  return true;
}

Q4T_TEST(
    realtime_audio_history_borrow_outlives_history_and_uses_fresh_generation) {
  std::shared_ptr<RealtimeAudioRecord> older, newer;
  const RealtimeTranscriptTicket reused{"sess_audio", "item_a", 2};
  {
    RealtimeAudioHistory history("sess_audio", 32);
    Q4T_CHECK(history.Decode(kTicket, kItem, Split(1), 16, &older));
    Q4T_CHECK(history.Erase(kTicket));
    Q4T_CHECK(history.Decode(reused, kItem, Split(4), 16, &newer));
    Q4T_CHECK(!history.Find(kTicket) && !history.Erase(kTicket) &&
              history.Find(reused) == newer && history.ChargedBytes() == 32);
  }
  Q4T_CHECK(older->Samples()[1] == .5f && newer->Samples()[1] == .5f);
  older.reset();
  newer.reset();
  return true;
}

Q4T_TEST(realtime_audio_history_invalid_ticket_capacity_and_stream_are_atomic) {
  for (auto bounds : {std::pair<size_t, size_t>{0, 128},
                      {16, 0},
                      {16, 129},
                      {kRealtimeAudioHistoryBytes + 1, 1}}) {
    RealtimeAudioHistory invalid("sess_audio", bounds.first, bounds.second);
    std::shared_ptr<RealtimeAudioRecord> output;
    Q4T_CHECK(!invalid.Valid() &&
              !invalid.Decode(kTicket, kItem, Split(4), 16, &output));
  }
  RealtimeAudioHistory history("sess_audio", 32, 1);
  std::shared_ptr<RealtimeAudioRecord> output;
  for (const auto& ticket :
       {RealtimeTranscriptTicket{"other", "item_a", 1},
        RealtimeTranscriptTicket{"sess_audio", "root", 1},
        RealtimeTranscriptTicket{"sess_audio", "item_a", 0}})
    Q4T_CHECK(!history.Decode(ticket, kItem, Split(4), 16, &output));
  Q4T_CHECK(!history.BeginStream(kTicket, 1, 8, 0, &output));
  Q4T_CHECK(!history.BeginStream(kTicket, 0, 8, 4, &output));
  Q4T_CHECK(!history.BeginStream(kTicket, 1, 600 * 16000 + 1, 4, &output));
  Q4T_CHECK(history.ChargedBytes() == 0 && history.Items() == 0 && !output);
  Q4T_CHECK(history.Decode(kTicket, kItem, Split(4), 16, &output));
  const auto original = output;
  Q4T_CHECK(!history.Decode(kTicket, kItem, Split(4), 16, &output) &&
            !history.Decode(kNext, kItem, Split(4), 16, &output));
  Q4T_CHECK(output == original && history.ChargedBytes() == 16);
  return true;
}

Q4T_TEST(realtime_audio_history_finite_normalization_matches_streamed_input) {
  using N = q4t::audio::NoiseReduction;
  for (const auto format :
       {RealtimeAudioFormat{},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 8000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kALaw, 8000}}) {
    for (const auto noise : {N::kOff, N::kNearField, N::kFarField}) {
      std::vector<uint8_t> bytes(4096);
      for (size_t i = 0; i < bytes.size(); ++i) bytes[i] = (i * 37) % 256;
      const size_t width =
          format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
      RealtimeAudioItem item{1,
                             7,
                             0,
                             bytes.size() / width * (48000 / format.rate),
                             bytes.size() / width,
                             bytes.size(),
                             format};
      RealtimeAudioHistory history("sess_audio");
      std::shared_ptr<RealtimeAudioRecord> result;
      Q4T_CHECK(
          history.DecodeFinite(kTicket, item, bytes, noise, 16384, &result));
      RealtimeNoiseInput stream(format, noise, 0);
      std::vector<float> expected;
      const auto sink = [&](auto block) {
        expected.insert(expected.end(), block.begin(), block.end());
        return q4t::Status{};
      };
      for (size_t begin = 0; begin < bytes.size(); begin += 197)
        Q4T_CHECK(stream.Append(
            std::span(bytes).subspan(
                begin, std::min<size_t>(197, bytes.size() - begin)),
            sink));
      Q4T_CHECK(stream.Finish(sink));
      Q4T_CHECK(std::ranges::equal(result->Samples(), expected) &&
                !result->Stream());
    }
  }
  return true;
}

Q4T_TEST(realtime_audio_history_finite_faults_release_all_new_storage) {
  using N = q4t::audio::NoiseReduction;
  RealtimeAudioHistory history("sess_audio", 32);
  std::shared_ptr<RealtimeAudioRecord> first;
  Q4T_CHECK(history.Decode(kTicket, kItem, Split(0), 16, &first));
  auto result = first;
  for (bool cancel : {false, true}) {
    auto item = kItem;
    if (!cancel) ++item.end_ticks;
    Q4T_CHECK(!history.DecodeFinite(kNext, item, kBytes, N::kNearField, 16,
                                    &result, [=] { return cancel; }));
    Q4T_CHECK(result == first && history.ChargedBytes() == 16 &&
              history.Items() == 1);
  }
  Q4T_CHECK(
      history.DecodeFinite(kNext, kItem, kBytes, N::kNearField, 16, &result));
  Q4T_CHECK(result != first && history.ChargedBytes() == 32);
  return true;
}

Q4T_TEST(realtime_audio_history_finite_and_stream_share_borrow_budget) {
  RealtimeAudioHistory history("sess_audio", 16);
  std::shared_ptr<RealtimeAudioRecord> finite, stream;
  Q4T_CHECK(history.DecodeFinite(kTicket, kItem, kBytes,
                                 q4t::audio::NoiseReduction::kNearField, 16,
                                 &finite));
  Q4T_CHECK(history.Erase(kTicket) && history.ChargedBytes() == 16);
  Q4T_CHECK(!history.BeginStream(kNext, 1, 4, 2, &stream));
  finite.reset();
  Q4T_CHECK(history.BeginStream(kNext, 1, 4, 2, &stream));
  Q4T_CHECK(history.ChargedBytes() == 16 && !finite);
  return true;
}

Q4T_TEST(realtime_audio_history_finite_owns_readonly_source_and_extent) {
  using N = q4t::audio::NoiseReduction;
  RealtimeAudioHistory history("sess_audio", 64);
  auto bytes = kBytes;
  std::shared_ptr<RealtimeAudioRecord> result;
  Q4T_CHECK(
      history.DecodeFinite(kTicket, kItem, bytes, N::kFarField, 16, &result) &&
      bytes == kBytes);
  const std::vector<float> expected(result->Samples().begin(),
                                    result->Samples().end());
  bytes.fill(0);
  Q4T_CHECK(history.Erase(kTicket) &&
            std::ranges::equal(result->Samples(), expected));
  const auto retained = result;
  auto bad = kItem;
  bad.format.rate = 0;
  Q4T_CHECK(
      !history.DecodeFinite(kNext, bad, kBytes, N::kFarField, 16, &result) &&
      result == retained);
  Q4T_CHECK(!history.DecodeFinite(kNext, kItem, kBytes, static_cast<N>(99), 16,
                                  &result));
  Q4T_CHECK(result == retained && history.ChargedBytes() == 16);
  return true;
}
