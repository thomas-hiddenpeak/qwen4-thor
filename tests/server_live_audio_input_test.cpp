#include "q4t/server/live_audio_input.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
constexpr std::array<RealtimeAudioFormat, 4> kTailFormats{{
    {RealtimeAudioEncoding::kPcm16, 16000},
    {RealtimeAudioEncoding::kPcm16, 24000},
    {RealtimeAudioEncoding::kMuLaw, 8000},
    {RealtimeAudioEncoding::kALaw, 8000},
}};
std::string Quiet(RealtimeAudioFormat format, size_t samples) {
  const auto encoding = format.encoding;
  const auto width = encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  const auto byte = encoding == RealtimeAudioEncoding::kPcm16 ? char(0)
                    : encoding == RealtimeAudioEncoding::kMuLaw ? char(0xff)
                                                               : char(0xd5);
  return std::string(samples * width, byte);
}
std::string SpeechThenQuiet(RealtimeAudioFormat format) {
  auto raw = Quiet(format, format.rate / 2);
  std::fill_n(raw.begin(), raw.size() / 4, '\x15');
  return raw;
}
std::string Base64(std::string_view bytes) {
  std::string result;
  if (!q4t::io::EncodeBase64(bytes, kWebSocketMessageLimit, &result))
    throw std::runtime_error("base64 fixture");
  return result;
}
struct Fixture {
  RealtimeConversation history{"live_input_history"};
  RequestRegistry requests;
  RealtimeTranscriptionCallbacks native;
  LiveAudioInputCallbacks callbacks;
  std::unique_ptr<LiveAudioInput> input;
  std::vector<Json> events;
  std::vector<RealtimeInputCommit> commits;
  std::vector<std::string> audio;
  std::vector<RealtimeAudioItem> ranges;
  std::atomic<int> calls{0}, releases{0};
  std::atomic<bool> hold{false}, worker_cancelled{false};
  bool voiced = false, ready = true, output_ok = true, native_ok = true;
  bool owner_thread = true;
  size_t frames = 0, starts = 0, stops = 0;
  std::string final_text = "read project";
  const std::thread::id thread = std::this_thread::get_id();
  Fixture() {
    native.validate = [](const auto&) { return ObjectReply{}; };
    native.vad_step = [&](auto, auto*, float* p) {
      *p = voiced && (frames == 1 || frames == 2) ? 1.f : 0.f;
      ++frames;
      return q4t::Status{};
    };
    native.acquire = [&](const std::string& id) {
      return requests.Register(id, "", Clock::now() + 2s);
    };
    native.release = [&](const auto& control) {
      requests.Remove(control);
      ++releases;
    };
    native.execute = [&](const auto& job, const auto& spans,
                         const auto& control, const auto& emit) {
      std::string actual;
      for (auto span : spans)
        actual.append(reinterpret_cast<const char*>(span.data()), span.size());
      audio.push_back(std::move(actual));
      ranges.push_back(job.audio);
      ++calls;
      while (hold.load() && !control->Cancelled())
        std::this_thread::sleep_for(1ms);
      if (control->Cancelled()) {
        worker_cancelled = true;
        return Error(409, "fixture_cancelled", "Fixture cancelled");
      }
      if (!native_ok)
        return Error(503, "fixture_asr_failed", "Actual CPU executor failed");
      for (const auto delta : {"read ", "project"})
        if (!emit(Object({{"delta", String(delta)}})))
          return Error(409, "fixture_output_failed", "CPU output failed");
      ObjectReply result;
      result.value =
          Object({{"text", String(final_text)},
                  {"usage", Object({{"type", String("tokens")},
                                    {"input_tokens", Number(13)},
                                    {"output_tokens", Number(7)},
                                    {"total_tokens", Number(20)}})}});
      return result;
    };
    callbacks.emit = [&](Json event) {
      owner_thread &= std::this_thread::get_id() == thread;
      if (!output_ok) return Error(503, "fixture_output_full", "Output full");
      Set(&event, "event_id", String("event_fixture"));
      ChatPlanError error;
      if (!ValidateLiveShape(event, LiveShape::kServerEvent, &error))
        return Error(500, "invalid_fixture_event", error.param);
      events.push_back(std::move(event));
      return ObjectReply{};
    };
    callbacks.ready = [&] { return ready; };
    callbacks.speech = [&](bool started, auto, auto, bool) {
      started ? ++starts : ++stops;
      return true;
    };
    callbacks.committed = [&](RealtimeInputCommit notice) {
      commits.push_back(std::move(notice));
      return ObjectReply{};
    };
    callbacks.capture_response = [](RealtimePromptSelection* selected) {
      selected->settings = Object({{"instructions", String("at commit")}});
      return ObjectReply{};
    };
  }
  ObjectReply Start(RealtimeAudioFormat format = {}) {
    input = std::make_unique<LiveAudioInput>("live_input", history, native,
                                             callbacks, 30);
    return input->Prepare(format);
  }
  bool Wait(const std::function<bool()>& done) {
    const auto end = Clock::now() + 1s;
    while (Clock::now() < end) {
      if (!input->Tick().ok()) return false;
      if (done()) return true;
      std::this_thread::sleep_for(1ms);
    }
    return false;
  }
  bool Finish() {
    return input->Close().ok() && Wait([&] { return input->Drained(); });
  }
  size_t Count(std::string_view type) const {
    return std::ranges::count_if(events, [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
};
}  // namespace

Q4T_TEST(live_audio_input_close_after_vad_silence_avoids_second_asr) {
  for (auto format : kTailFormats) {
    Fixture f;
    f.voiced = true;
    Q4T_CHECK(f.Start(format).ok());
    Q4T_CHECK(f.input->Append(Base64(SpeechThenQuiet(format))).ok());
    Q4T_CHECK(f.Wait([&] { return f.releases == 1; }));
    Q4T_CHECK(f.input->AudioBytes() > 0 && f.calls == 1);
    Q4T_CHECK(f.Finish() && f.calls == 1 && f.releases == 1);
    Q4T_CHECK(f.commits.size() == 1 && f.commits[0].automatic);
    Q4T_CHECK(f.input->AudioBytes() == 0 &&
              f.input->AcceptedTicks() == 24000 && !f.Count("error"));
  }
  return true;
}

Q4T_TEST(live_audio_input_close_pending_vad_is_nonblocking_and_drains_once) {
  for (auto format : kTailFormats) {
    Fixture f;
    f.voiced = true;
    f.hold = true;
    Q4T_CHECK(f.Start(format).ok());
    Q4T_CHECK(f.input->Append(Base64(SpeechThenQuiet(format))).ok());
    Q4T_CHECK(f.input->Close().ok() && f.frames == 0 && f.calls == 0);
    Q4T_CHECK(!f.input->Drained() && f.input->AudioBytes() > 0);
    Q4T_CHECK(f.Wait([&] { return f.calls == 1; }));
    Q4T_CHECK(!f.input->Drained() && f.releases == 0);
    f.hold = false;
    Q4T_CHECK(f.Wait([&] { return f.input->Drained(); }));
    Q4T_CHECK(f.calls == 1 && f.releases == 1 && f.commits.size() == 1);
    Q4T_CHECK(f.input->AcceptedTicks() == 24000 && !f.Count("error"));
  }
  return true;
}

Q4T_TEST(live_audio_input_close_quiet_input_never_starts_asr) {
  for (auto format : kTailFormats) {
    Fixture f;
    Q4T_CHECK(f.Start(format).ok());
    Q4T_CHECK(f.input->Append(Base64(Quiet(format, format.rate / 2))).ok());
    Q4T_CHECK(f.Finish() && f.frames > 0 && f.calls == 0);
    Q4T_CHECK(f.commits.empty() && f.input->AudioBytes() == 0);
    Q4T_CHECK(f.input->AcceptedTicks() == 24000 && f.events.empty());
  }
  return true;
}

Q4T_TEST(live_audio_input_close_unscored_signal_preserves_exact_waveform) {
  for (auto format : kTailFormats) {
    Fixture f;
    auto raw = Quiet(format, format.rate * 64 / 1000);
    const auto width = format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    raw.append(width, '\x15');
    Q4T_CHECK(f.Start(format).ok() && f.input->Append(Base64(raw)).ok());
    Q4T_CHECK(f.Finish() && f.frames > 0 && f.calls == 1);
    Q4T_CHECK(f.audio == std::vector<std::string>({raw}));
    Q4T_CHECK(f.commits.size() == 1 && !f.commits[0].automatic);
    Q4T_CHECK(f.input->AcceptedTicks() == 3072 + 48000 / format.rate);
  }
  return true;
}

Q4T_TEST(live_audio_input_close_active_speech_never_pads_or_drops_samples) {
  for (auto format : kTailFormats) {
    Fixture f;
    f.native.vad_step = [](auto, auto*, float* probability) {
      *probability = 1;
      return q4t::Status{};
    };
    auto raw = Quiet(format, format.rate / 10);
    std::fill(raw.begin(), raw.end(), '\x15');
    Q4T_CHECK(f.Start(format).ok() && f.input->Append(Base64(raw)).ok());
    Q4T_CHECK(f.Finish() && f.calls == 1 && f.releases == 1);
    Q4T_CHECK(f.audio == std::vector<std::string>({raw}));
    Q4T_CHECK(f.commits.size() == 1 && !f.commits[0].automatic);
    Q4T_CHECK(f.input->AcceptedTicks() == 4800 && !f.Count("error"));
  }
  return true;
}

Q4T_TEST(live_audio_input_pcm_formats_preserve_native_vad_ranges_and_context) {
  for (uint32_t rate : {16000, 24000}) {
    Fixture f;
    f.voiced = true;
    f.ready = false;
    Q4T_CHECK(f.Start({RealtimeAudioEncoding::kPcm16, rate}).ok());
    const std::string raw(rate,
                          '\x15');  // Half a second, complete PCM samples.
    Q4T_CHECK(f.input->Append(Base64(raw)).ok());
    Q4T_CHECK(f.Wait([&] { return f.commits.size() == 1; }));
    Q4T_CHECK(f.calls == 0 && f.starts == 1 && f.stops == 1);
    const auto& commit = f.commits.front();
    Q4T_CHECK(commit.automatic && commit.context_status.ok() &&
              commit.response);
    Q4T_CHECK(commit.response->settings.GetString("instructions") ==
              "at commit");
    const auto span = commit.audio;
    Q4T_CHECK(span.format.rate == rate && span.begin_ticks < span.end_ticks);
    f.ready = true;
    Q4T_CHECK(f.Wait([&] { return f.releases == 1; }));
    Q4T_CHECK(f.audio.size() == 1 && f.ranges.front() == span);
    const auto offset = span.begin_ticks * rate / 48000 * 2;
    const auto count = (span.end_ticks - span.begin_ticks) * rate / 48000 * 2;
    Q4T_CHECK(f.audio.front() == raw.substr(offset, count));
    for (const auto& event : f.events) {
      if (event.GetString("type") != "session.input_transcript.delta") continue;
      Q4T_CHECK(event.GetInt("start_ms") == int64_t(span.begin_ticks / 48));
      Q4T_CHECK(event.GetInt("end_ms") == int64_t(span.end_ticks / 48));
    }
    auto frozen = commit.context;
    bool ready = false;
    Q4T_CHECK(f.history.Resolve(&frozen, &ready) && ready);
    Q4T_CHECK(f.Finish() && f.owner_thread);
    Q4T_CHECK(f.input->AcceptedTicks() == 24000);
  }
  return true;
}

Q4T_TEST(live_audio_input_g711_mute_is_format_correct_silence) {
  for (auto encoding :
       {RealtimeAudioEncoding::kMuLaw, RealtimeAudioEncoding::kALaw}) {
    Fixture f;
    Q4T_CHECK(f.Start({encoding, 8000}).ok());
    Q4T_CHECK(f.input->Mute(true, "mute_1").ok());
    Q4T_CHECK(f.input->Append(Base64("1234")).ok() && f.Finish());
    Q4T_CHECK(f.audio ==
              std::vector<std::string>({std::string(
                  4, encoding == RealtimeAudioEncoding::kMuLaw ? char(0xff)
                                                               : char(0xd5))}));
    Q4T_CHECK(f.ranges.front().format.encoding == encoding);
    Q4T_CHECK(f.input->AcceptedTicks() == 24);
    Q4T_CHECK(f.Count("session.input_audio.muted") == 1);
    Q4T_CHECK(f.events.front().GetString("client_event_id") == "mute_1");
  }
  return true;
}

Q4T_TEST(live_audio_input_partial_pcm_mute_and_invalid_base64_are_atomic) {
  Fixture f;
  Q4T_CHECK(f.Start({RealtimeAudioEncoding::kPcm16, 16000}).ok());
  Q4T_CHECK(f.input->Append(Base64("\x01")).ok());
  Q4T_CHECK(!f.input->AcceptedTicks());
  Q4T_CHECK(!f.input->Close().ok() && !f.input->Drained());
  Q4T_CHECK(f.input->Mute(true).ok());
  Q4T_CHECK(f.input->Append(Base64("\x02\x03")).ok());
  Q4T_CHECK(f.input->AcceptedTicks() == 3);
  Q4T_CHECK(!f.input->Append("%%%%").ok());
  Q4T_CHECK(f.input->Mute(false).ok());
  Q4T_CHECK(f.input->Append(Base64("\x04\x05\x06")).ok());
  Q4T_CHECK(f.Finish() && f.input->AcceptedTicks() == 9);
  Q4T_CHECK(f.audio ==
            std::vector<std::string>({std::string("\x01\x02\0\0\x05\x06", 6)}));
  Q4T_CHECK(f.calls == 1 && f.Count("session.input_audio.muted") == 1 &&
            f.Count("session.input_audio.unmuted") == 1);
  return true;
}

Q4T_TEST(live_audio_input_transcript_terminal_only_adds_unemitted_suffix) {
  Fixture f;
  f.final_text = "read project.";
  Q4T_CHECK(f.Start().ok() && f.input->Append(Base64("\x01\x02\x03\x04")).ok());
  Q4T_CHECK(f.Finish());
  std::string text;
  for (const auto& event : f.events)
    if (event.GetString("type") == "session.input_transcript.delta")
      text += event.GetString("delta");
  Q4T_CHECK(text == f.final_text &&
            f.Count("session.input_transcript.delta") == 3);
  Q4T_CHECK(f.Count("session.input_transcript.done") == 0);
  Q4T_CHECK(f.calls == 1 && f.releases == 1 && f.owner_thread);
  return true;
}

Q4T_TEST(live_audio_input_close_waits_actual_borrower_and_output_drain) {
  Fixture f;
  f.hold = true;
  Q4T_CHECK(f.Start().ok() && f.input->Append(Base64("\x01\x02\x03\x04")).ok());
  Q4T_CHECK(f.input->Close().ok());
  Q4T_CHECK(f.Wait([&] { return f.calls == 1; }));
  Q4T_CHECK(!f.input->Drained() && f.input->AudioBytes() >= 2);
  Q4T_CHECK(!f.input->Append(Base64("\x03\x04")).ok());
  Q4T_CHECK(!f.input->Mute(true).ok());
  f.hold = false;
  Q4T_CHECK(f.Wait([&] { return f.input->Drained(); }));
  Q4T_CHECK(f.releases == 1 && f.calls == 1 && f.input->AudioBytes() == 0);
  return true;
}

Q4T_TEST(live_audio_input_disconnect_cancels_join_without_closed_or_replay) {
  Fixture f;
  f.hold = true;
  Q4T_CHECK(f.Start().ok() && f.input->Append(Base64("\x01\x02\x03\x04")).ok());
  Q4T_CHECK(f.input->Close().ok() && f.Wait([&] { return f.calls == 1; }));
  f.input->Cancel();
  f.input->Join();
  Q4T_CHECK(f.worker_cancelled && f.releases == 1 && !f.input->Drained());
  Q4T_CHECK(!f.input->Tick().ok() && !f.input->Close().ok());
  Q4T_CHECK(!f.Count("session.closed") && f.calls == 1);
  return true;
}

Q4T_TEST(live_audio_input_processor_and_sink_failures_do_not_restart) {
  Fixture vad;
  vad.native.vad_step = [](auto, auto*, float*) {
    return q4t::Status::Fail("native CPU VAD failed");
  };
  Q4T_CHECK(vad.Start({RealtimeAudioEncoding::kPcm16, 16000}).ok());
  Q4T_CHECK(vad.input->Append(Base64(std::string(1024, '\0'))).ok());
  Q4T_CHECK(!vad.input->Tick().ok());
  Q4T_CHECK(!vad.input->Tick().ok() && !vad.input->Drained() && vad.calls == 0);
  Q4T_CHECK(vad.Count("error") == 1);
  Fixture sink;
  Q4T_CHECK(sink.Start().ok());
  sink.output_ok = false;
  Q4T_CHECK(!sink.input->Mute(true).ok() && !sink.input->Tick().ok());
  Q4T_CHECK(!sink.input->Drained() && sink.calls == 0);
  Fixture asr;
  asr.native_ok = false;
  Q4T_CHECK(asr.Start().ok() &&
            asr.input->Append(Base64("\x01\x02\x03\x04")).ok());
  Q4T_CHECK(asr.Finish() && asr.Count("error") == 1 && asr.calls == 1);
  Q4T_CHECK(!asr.Count("session.input_transcript.delta"));
  return true;
}

Q4T_TEST(live_audio_input_missing_native_processor_never_prepares_success) {
  for (int kind = 0; kind < 4; ++kind) {
    Fixture f;
    if (kind == 0) f.native.vad_step = {};
    if (kind == 1) f.native.execute = {};
    if (kind == 2) f.native.validate = {};
    if (kind == 3) f.callbacks.emit = {};
    Q4T_CHECK(!f.Start().ok());
    Q4T_CHECK(f.calls == 0 && f.events.empty() && !f.input->Drained());
  }
  return true;
}

Q4T_TEST(
    live_audio_input_close_fractional_asr_tail_keeps_clock_without_fake_work) {
  for (uint32_t rate : {16000, 24000}) {
    Fixture f;
    Q4T_CHECK(f.Start({RealtimeAudioEncoding::kPcm16, rate}).ok());
    Q4T_CHECK(f.input->Append(Base64("\x01\x02")).ok() && f.Finish());
    Q4T_CHECK(f.input->AcceptedTicks() == 48000 / rate);
    Q4T_CHECK(f.calls == (rate == 16000 ? 1 : 0));
    Q4T_CHECK(f.Count("session.input_transcript.delta") ==
              (rate == 16000 ? 2 : 0));
    Q4T_CHECK(!f.Count("error") && f.input->AudioBytes() == 0);
  }
  return true;
}
