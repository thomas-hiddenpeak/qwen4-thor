#include "q4t/server/overlapping_output.h"
#include "q4t/test.h"

namespace {
using namespace q4t;
using namespace q4t::server;
audio::AlignerTimes Times(std::vector<int64_t> values) {
  return {values, values, 0, 0};
}
Status Append(OverlappingTranscript* out, size_t track, size_t begin,
              size_t end, std::string_view text, std::string label,
              int64_t start_ms = 100, int64_t end_ms = 900,
              const std::function<bool()>& cancelled = {}) {
  const std::vector<audio::AlignerWord> words{
      {std::string(text), 0, text.size()}};
  const std::vector<audio::SpeakerTurn> turns{{begin, end, std::move(label)}};
  return out->Append(track, text, {begin, end, false}, words,
                     Times({start_ms, end_ms}), turns, 10, cancelled);
}
}  // namespace

Q4T_TEST(overlapping_output_keeps_real_crossing_times_and_both_voices) {
  OverlappingTranscript out(64000);
  Q4T_CHECK(Append(&out, 0, 0, 32000, "first!", "A", 100, 1800));
  Q4T_CHECK(out.DrainBefore(0) && out.Segments().empty());
  Q4T_CHECK(Append(&out, 1, 0, 32000, "second?", "B", 500, 1300));
  Q4T_CHECK(out.DrainBefore(32000));
  Q4T_CHECK(out.Segments().size() == 2);
  Q4T_CHECK(out.Segments()[0].begin_sample == 1600);
  Q4T_CHECK(out.Segments()[0].end_sample == 28800);
  Q4T_CHECK(out.Segments()[1].begin_sample == 8000);
  Q4T_CHECK(out.Segments()[1].end_sample == 20800);
  Q4T_CHECK(out.Text() == "first! second?");
  std::string response = "prior";
  Q4T_CHECK(!out.Finish({25, 3}, false, &response) && response == "prior");
  Q4T_CHECK(out.DrainBefore(64001));
  Q4T_CHECK(out.Finish({25, 3}, false, &response));
  io::Json json;
  Q4T_CHECK(io::ParseJson(response, &json));
  Q4T_CHECK(json.GetArray("segments")->array.size() == 2);
  Q4T_CHECK(json.Find("duration")->number == 4);
  return true;
}

Q4T_TEST(overlapping_output_watermark_preserves_sse_ids_and_usage) {
  OverlappingTranscript out(64000);
  Q4T_CHECK(Append(&out, 0, 0, 32000, "later", "B", 1000, 1400));
  Q4T_CHECK(Append(&out, 1, 0, 16000, "早。", "A", 100, 900));
  Q4T_CHECK(out.DrainBefore(16000));
  Q4T_CHECK(out.Segments().size() == 1 && out.Segments()[0].speaker == "A");
  Q4T_CHECK(out.Segments()[0].id == "0");
  Q4T_CHECK(Append(&out, 1, 16000, 32000, "again", "A", 300, 700));
  Q4T_CHECK(out.DrainBefore(64001));
  Q4T_CHECK(out.Segments().size() == 3);
  std::string reconstructed;
  for (size_t i = 0; i < out.Segments().size(); ++i) {
    const auto& segment = out.Segments()[i];
    Q4T_CHECK(segment.id == std::to_string(i));
    std::string delta, event;
    Q4T_CHECK(DiarizedTranscript::DeltaJson(segment, i > 0, &delta));
    Q4T_CHECK(DiarizedTranscript::SegmentJson(segment, &event));
    io::Json d, e;
    Q4T_CHECK(io::ParseJson(delta, &d) && io::ParseJson(event, &e));
    Q4T_CHECK(d.GetString("segment_id") == e.GetString("id"));
    reconstructed += d.GetString("delta");
  }
  Q4T_CHECK(reconstructed == out.Text());
  Q4T_CHECK(out.Text() == "早。 later again");
  std::string response;
  Q4T_CHECK(out.Finish({35, 7}, true, &response));
  io::Json json;
  Q4T_CHECK(io::ParseJson(response, &json));
  Q4T_CHECK(json.GetString("type") == "transcript.text.done");
  Q4T_CHECK(json.Find("usage")->GetInt("total_tokens") == 42);
  Q4T_CHECK(
      json.Find("usage")->Find("input_token_details")->GetInt("audio_tokens") ==
      30);
  return true;
}

Q4T_TEST(overlapping_output_rejects_late_track_or_watermark_regression) {
  for (int failure = 0; failure < 4; ++failure) {
    OverlappingTranscript out(64000);
    Q4T_CHECK(Append(&out, 0, 0, 16000, "word", "A"));
    Q4T_CHECK(out.DrainBefore(16000));
    if (failure == 0) Q4T_CHECK(!Append(&out, 1, 0, 16000, "late", "B"));
    if (failure == 1) Q4T_CHECK(!Append(&out, 2, 16000, 32000, "bad", "B"));
    if (failure == 2) Q4T_CHECK(!out.DrainBefore(15999));
    if (failure == 3) Q4T_CHECK(!out.DrainBefore(64002));
    std::string response = "prior";
    Q4T_CHECK(!out.DrainBefore(64001));
    Q4T_CHECK(!out.Finish({30, 5}, false, &response) && response == "prior");
  }
  return true;
}

Q4T_TEST(overlapping_output_cancel_and_silence_do_not_publish_partial_success) {
  for (bool drain : {false, true}) {
    OverlappingTranscript out(64000);
    if (drain) {
      Q4T_CHECK(Append(&out, 0, 0, 16000, "word", "A"));
      Q4T_CHECK(!out.DrainBefore(64001, [] { return true; }));
    } else {
      Q4T_CHECK(!Append(&out, 0, 0, 16000, "word", "A", 100, 900,
                        [] { return true; }));
    }
    std::string result = "prior";
    Q4T_CHECK(!out.Finish({10, 1}, true, &result) && result == "prior");
    Q4T_CHECK(out.Segments().empty());
  }
  OverlappingTranscript silence(64000);
  Q4T_CHECK(silence.DrainBefore(64001));
  std::string result;
  Q4T_CHECK(silence.Finish({}, true, &result));
  io::Json json;
  Q4T_CHECK(io::ParseJson(result, &json));
  Q4T_CHECK(json.GetString("text").empty());
  Q4T_CHECK(json.GetArray("segments")->array.empty());
  return true;
}

Q4T_TEST(overlapping_output_coverage_usage_and_shared_clip_capacity) {
  OverlappingTranscript out(96000);
  Q4T_CHECK(Append(&out, 0, 0, 16000, "word", "A"));
  Q4T_CHECK(out.DrainBefore(96001));
  std::string result = "prior";
  Q4T_CHECK(!out.Finish({9, 2}, false, &result) && result == "prior");
  OverlappingTranscript capacity(96000);
  for (size_t i = 0; i < 256; ++i)
    Q4T_CHECK(capacity.Append(i % 2, "", {i * 16, (i + 1) * 16, false}, {}, {},
                              {}, 0));
  Q4T_CHECK(!capacity.Append(0, "", {4096, 4112, false}, {}, {}, {}, 0));
  Q4T_CHECK(!capacity.DrainBefore(96001));
  Q4T_CHECK(!capacity.Finish({}, false, &result) && result == "prior");
  OverlappingTranscript bad(96000);
  Q4T_CHECK(!Append(&bad, 0, 0, 16000, "word", "A", 900, 100));
  Q4T_CHECK(!bad.DrainBefore(96001));
  return true;
}
