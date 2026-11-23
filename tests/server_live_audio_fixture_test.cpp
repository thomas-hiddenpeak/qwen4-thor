#include "q4t/server/live_audio_fixture.h"

#include <algorithm>
#include <array>

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/realtime_transcription_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr std::array<RealtimeAudioFormat, 4> kFormats{{
    {RealtimeAudioEncoding::kPcm16, 24000},
    {RealtimeAudioEncoding::kPcm16, 16000},
    {RealtimeAudioEncoding::kMuLaw, 8000},
    {RealtimeAudioEncoding::kALaw, 8000},
}};
std::string Encoded(std::string_view bytes) {
  std::string encoded;
  if (!q4t::io::EncodeBase64(bytes, kWebSocketMessageLimit, &encoded))
    throw std::runtime_error("PCM fixture encoding");
  return encoded;
}
struct Observer {
  Clock::time_point now = Clock::now();
  std::vector<Json> events;
  size_t fail_at = 0, calls = 0;
  std::string bytes;
  ObjectReply Emit(Json event) {
    if (++calls == fail_at) return Error(503, "fixture_sink", "Full output");
    if (event.GetString("type") != "session.output_audio.delta")
      Set(&event, "event_id", String("event_" + std::to_string(calls)));
    ChatPlanError error;
    if (!ValidateLiveShape(event, LiveShape::kServerEvent, &error))
      return Error(500, "fixture_shape", error.param);
    if (event.GetString("type") == "session.output_audio.delta") {
      std::string raw;
      auto reply = DecodeRealtimeAudioBase64(event.GetString("delta"), &raw);
      if (!reply.ok()) return reply;
      bytes += raw;
    }
    events.push_back(std::move(event));
    return {};
  }
  std::unique_ptr<LiveAudioFixture> Make(RealtimeAudioFormat format = {}) {
    return std::make_unique<LiveAudioFixture>(
        format, [this](Json event) { return Emit(std::move(event)); },
        [this] { return now; });
  }
  size_t Count(std::string_view type) const {
    return std::ranges::count_if(events, [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
};
}  // namespace

Q4T_TEST(live_fixture_formats_preserve_real_extent_and_pcm24_bytes) {
  for (auto format : kFormats) {
    Observer o;
    auto fixture = o.Make(format);
    const std::string pcm(960, '\x15');
    Q4T_CHECK(fixture->Prepare(Encoded(pcm), "fixture_1").ok());
    Q4T_CHECK(o.events.empty() && fixture->Accept().ok());
    Q4T_CHECK(o.Count("session.output_audio.fixture.playing") == 1);
    Q4T_CHECK(fixture->Tick(100).ok() && !fixture->Completed());
    o.now += 20ms;
    Q4T_CHECK(fixture->Tick(999).ok() && fixture->Completed());
    Q4T_CHECK(fixture->EndTicks() == 1060 &&
              fixture->Bytes() == o.bytes.size());
    const auto width = format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    Q4T_CHECK(o.bytes.size() == 480 * format.rate / 24000 * width);
    if (format == RealtimeAudioFormat{}) Q4T_CHECK(o.bytes == pcm);
    Q4T_CHECK(o.Count("session.output_audio.fixture.played") == 1);
    Q4T_CHECK(!o.Count("session.output_transcript.delta"));
    Q4T_CHECK(o.events.front().GetString("client_event_id") == "fixture_1");
    Q4T_CHECK(o.events.back().GetString("client_event_id") == "fixture_1");
    Q4T_CHECK(fixture->Tick(999).ok() &&
              o.Count("session.output_audio.fixture.played") == 1);
  }
  return true;
}

Q4T_TEST(live_fixture_complete_validation_precedes_acceptance_and_bounds) {
  Observer o;
  auto fixture = o.Make();
  const std::string wave =
      "RIFF" + std::string(4, '\0') + "WAVEfmt " + std::string(28, '\0');
  for (const auto& data :
       {std::string(), std::string("%bad"), Encoded("abc"), Encoded(wave),
        Encoded(std::string(LiveAudioFixture::kMaximumBytes + 2, '\0'))}) {
    Q4T_CHECK(!fixture->Prepare(data, "bad").ok() && o.events.empty());
    Q4T_CHECK(!fixture->Accept().ok());
  }
  const std::string maximum(LiveAudioFixture::kMaximumBytes, '\0');
  Q4T_CHECK(fixture->Prepare(Encoded(maximum), "maximum").ok());
  Q4T_CHECK(o.events.empty() && fixture->Accept().ok());
  for (int i = 0; i <= 1500; ++i) {
    Q4T_CHECK(fixture->Tick(0).ok());
    if (i < 1500) Q4T_CHECK(!fixture->Completed());
    o.now += 20ms;
  }
  Q4T_CHECK(fixture->Completed() && o.bytes == maximum);
  Q4T_CHECK(fixture->EndTicks() == 30 * 48000);
  Q4T_CHECK(!fixture->Prepare(Encoded("abcd"), {}).ok());
  return true;
}

Q4T_TEST(live_fixture_subsample_extent_is_never_padded_or_invented) {
  for (auto format : kFormats) {
    Observer o;
    auto fixture = o.Make(format);
    Q4T_CHECK(fixture->Prepare(Encoded("\x01\x02"), {}).ok());
    Q4T_CHECK(fixture->Accept().ok() && fixture->Tick(7).ok());
    Q4T_CHECK(!fixture->Completed());
    o.now += 1ms;
    Q4T_CHECK(fixture->Tick(7).ok() && fixture->Completed());
    Q4T_CHECK(fixture->EndTicks() == 9);
    Q4T_CHECK(o.bytes.size() == (format.rate == 24000 ? 2 : 0));
    Q4T_CHECK(o.Count("session.output_audio.fixture.played") == 1);
  }
  return true;
}

Q4T_TEST(live_fixture_pacing_does_not_skip_or_claim_early_drain) {
  Observer o;
  auto fixture = o.Make();
  const std::string pcm(3840, '\x15');
  Q4T_CHECK(fixture->Prepare(Encoded(pcm), {}).ok());
  Q4T_CHECK(fixture->Accept().ok() && o.bytes.empty());
  for (int i = 0; i < 100; ++i) Q4T_CHECK(fixture->Tick(100).ok());
  Q4T_CHECK(o.bytes.size() == 960 && !fixture->Completed());
  o.now += 80ms;
  for (size_t n : {1920, 2880, 3840}) {
    Q4T_CHECK(fixture->Tick(100).ok() && o.bytes.size() == n);
    Q4T_CHECK(!fixture->Completed());
  }
  Q4T_CHECK(fixture->Tick(100).ok() && fixture->Completed());
  Q4T_CHECK(o.bytes == pcm && fixture->EndTicks() == 3940);
  return true;
}

Q4T_TEST(live_fixture_failure_cancel_and_clock_never_publish_success) {
  for (int mode = 0; mode < 3; ++mode) {
    Observer o;
    auto fixture = o.Make();
    Q4T_CHECK(fixture->Prepare(Encoded(std::string(1920, '\x15')), {}).ok());
    Q4T_CHECK(fixture->Accept().ok() && fixture->Tick(0).ok());
    Q4T_CHECK(o.bytes.size() == 960);
    if (mode == 0) {
      o.fail_at = 3;
      o.now += 20ms;
    } else if (mode == 1) {
      fixture->Cancel();
    } else {
      o.now -= 1ms;
    }
    Q4T_CHECK(!fixture->Tick(0).ok());
    const auto calls = o.calls;
    o.now += 1s;
    Q4T_CHECK(!fixture->Tick(0).ok() && o.calls == calls);
    Q4T_CHECK(!fixture->Completed() &&
              !o.Count("session.output_audio.fixture.played"));
  }
  return true;
}
