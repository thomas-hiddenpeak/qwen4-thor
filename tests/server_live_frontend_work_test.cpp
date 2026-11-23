#include "q4t/server/live_frontend_work.h"

#include <algorithm>
#include <map>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/live_shape.h"
#include "q4t/server/realtime_transcription_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
RealtimeSpeechEvent Chunk(std::string bytes) {
  RealtimeSpeechEvent event;
  event.bytes = std::move(bytes);
  return event;
}
RealtimeSpeechEvent Done(size_t bytes, std::string text = "hello") {
  RealtimeSpeechEvent event;
  event.kind = RealtimeSpeechEvent::Kind::kDone;
  event.transcript = std::move(text);
  event.receipt = {bytes / 2, bytes / 2, bytes};
  return event;
}
ResponseDelta Text(std::string value) {
  ResponseDelta delta;
  delta.text = std::move(value);
  return delta;
}
Json Message(std::string value) {
  return Object(
      {{"role", String("assistant")}, {"content", String(std::move(value))}});
}
RealtimeConversationJob Job() {
  RealtimeConversationJob job;
  job.response_id = "resp_live_fixture";
  job.settings = Object({{"type", String("realtime")},
                         {"model", String("q4t-test")},
                         {"instructions", String("frozen")},
                         {"output_modalities", Array({String("audio")})}});
  job.response = Object();
  job.snapshot.items = Array();
  return job;
}
RealtimeGenerationResult Result(std::string text = "hello") {
  RealtimeGenerationResult result;
  result.message = Message(std::move(text));
  RealtimeResponseUsage usage;
  usage.total = {11, 7};
  usage.input_text = 11;
  usage.output_text = 3;
  usage.output_audio = 4;
  result.usage = usage;
  return result;
}
bool Wait(const std::function<bool()>& predicate) {
  const auto deadline = Clock::now() + 2s;
  do {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  } while (Clock::now() < deadline);
  return false;
}
struct Observer {
  std::vector<Json> events;
  size_t fail_at = 0, calls = 0;
  bool uncertain = false, throws = false, retain = true;
  ObjectReply Emit(Json event) {
    if (++calls == fail_at) {
      if (throws) throw std::runtime_error("CPU sink failure");
      auto reply = Error(503, "fixture_output_failed", "Output unavailable");
      reply.commit_unknown = uncertain;
      return reply;
    }
    // The actual LiveSession owns unique event IDs. Audio deltas alone have
    // no ID; this sink applies that same final envelope before SDK validation.
    if (event.GetString("type") != "session.output_audio.delta")
      Set(&event, "event_id", String("event_cpu_" + std::to_string(calls)));
    ChatPlanError error;
    const auto status =
        ValidateLiveShape(event, LiveShape::kServerEvent, &error);
    if (!status) return Error(500, "fixture_shape", status.message());
    if (retain) events.push_back(std::move(event));
    return {};
  }
  LiveEventSink Sink() {
    return [this](Json event) { return Emit(event); };
  }
  size_t Count(std::string_view type) const {
    return std::count_if(events.begin(), events.end(), [&](const auto& event) {
      return event.GetString("type") == type;
    });
  }
  bool Bytes(std::string* bytes) const {
    bytes->clear();
    for (const auto& event : events) {
      if (event.GetString("type") != "session.output_audio.delta") continue;
      Q4T_CHECK(event.object.size() == 2 && !event.Has("event_id"));
      std::string part;
      Q4T_CHECK(
          DecodeRealtimeAudioBase64(event.GetString("delta"), &part).ok());
      *bytes += part;
    }
    return true;
  }
};
struct Fixture {
  Observer observer;
  RequestRegistry registry;
  RealtimeConversation history{"sess_live_fixture"};
  RealtimeSpeechHistory speech{"sess_live_fixture"};
  std::map<std::string, RealtimeItemTicket> tickets;
  std::atomic<size_t> executed{0}, produced{0};
  std::atomic<bool> entered{false}, exited{false};
  size_t acquired = 0, released = 0;
  bool released_early = false, wrong_thread = false;
  bool snapshot_ok = false;
  std::thread::id owner = std::this_thread::get_id();
  LiveFrontendCallbacks cb;
  decltype(cb.execute) execute;
  std::unique_ptr<LiveFrontendWork> work;
  Fixture() {
    cb.prepare = [](const auto&) { return ObjectReply{}; };
    cb.acquire = [this](const auto& id) {
      ++acquired;
      return registry.Register(id, "", Clock::now() + 3s);
    };
    cb.release = [this](const auto& control) {
      released_early |= executed && !exited;
      wrong_thread |= std::this_thread::get_id() != owner;
      registry.Remove(control);
      ++released;
    };
    cb.execute = [this](const auto& job, const auto& control, const auto& delta,
                        const auto& audio) {
      ++executed;
      snapshot_ok = job.settings.GetString("instructions") == "frozen" &&
                    job.response_id == control->id();
      entered = true;
      try {
        auto result =
            execute ? execute(job, control, delta, audio) : Speak(delta, audio);
        exited = true;
        return result;
      } catch (...) {
        exited = true;
        throw;
      }
    };
    cb.item = [this](const Json& item, bool begin, bool terminal) {
      wrong_thread |= std::this_thread::get_id() != owner;
      const auto id = item.GetString("id");
      q4t::Status status;
      if (begin) {
        RealtimeItemTicket ticket;
        status = history.BeginOutput(item, id, true, 16384, &ticket);
        if (status) {
          tickets.emplace(id, ticket);
          if (item.GetString("type") == "message")
            status = speech.Begin(ticket, {}, 65536);
        }
      } else {
        status = history.Update(tickets.at(id), item, terminal);
      }
      return status ? ObjectReply{}
                    : Error(503, "fixture_history", status.message());
    };
    cb.audio = [this](std::string_view id, std::string_view bytes,
                      RealtimeAudioCommit commit) {
      wrong_thread |= std::this_thread::get_id() != owner;
      const auto& ticket = tickets.at(std::string(id));
      const auto status =
          commit == RealtimeAudioCommit::kAppend ? speech.Append(ticket, bytes)
          : commit == RealtimeAudioCommit::kComplete ? speech.Complete(ticket)
                                                     : speech.Interrupt(ticket);
      return status ? ObjectReply{}
                    : Error(503, "fixture_speech", status.message());
    };
    cb.emit = observer.Sink();
    cb.timeline = [] { return uint64_t{48000}; };
  }
  static RealtimeGenerationResult Speak(const RealtimeDeltaSink& delta,
                                        const RealtimeSpeechSink& audio) {
    auto result = Result();
    result.reply = delta(Text("hello"));
    if (result.reply.ok()) result.reply = audio(Chunk(std::string(4800, '\0')));
    if (result.reply.ok()) result.reply = audio(Done(4800));
    return result;
  }
  ObjectReply Start(RealtimeConversationJob job = Job(),
                    RealtimeAudioFormat format = {}) {
    work = std::make_unique<LiveFrontendWork>(
        std::move(job), "conv_live_fixture", format, cb, "client_cpu");
    return work->Start();
  }
  bool Drain() {
    bool valid = true;
    Q4T_CHECK(Wait([&] {
      valid &= work->Tick().ok();
      return !valid || work->Finished();
    }));
    Q4T_CHECK(valid && work->Finished());
    return true;
  }
};
}  // namespace

Q4T_TEST(live_audio_output_formats_preserve_waveform_duration_and_shape) {
  const std::vector<RealtimeAudioFormat> formats = {
      {},
      {RealtimeAudioEncoding::kPcm16, 16000},
      {RealtimeAudioEncoding::kMuLaw, 8000},
      {RealtimeAudioEncoding::kALaw, 8000}};
  for (const auto format : formats) {
    Observer observer;
    LiveAudioOutput output(format, 48000, observer.Sink(), {}, "client_cpu");
    Q4T_CHECK(output.Valid());
    std::string pcm(48000, '\0');
    if (format.rate == 24000)
      for (size_t i = 0; i < pcm.size(); ++i) pcm[i] = char(i & 255);
    for (size_t i = 0; i < pcm.size(); i += 997)
      Q4T_CHECK(output.Append(Chunk(pcm.substr(i, 997))).ok());
    Q4T_CHECK(observer.Count("session.output_audio.delta") > 0);
    Q4T_CHECK(observer.Count("session.output_transcript.delta") == 0);
    Q4T_CHECK(output.Append(Done(pcm.size())).ok() && output.Completed());
    std::string bytes;
    Q4T_CHECK(observer.Bytes(&bytes));
    if (format.rate == 24000) Q4T_CHECK(bytes == pcm);
    if (format.rate == 16000) Q4T_CHECK(bytes == std::string(32000, '\0'));
    if (format.rate == 8000)
      Q4T_CHECK(bytes ==
                std::string(
                    8000, char(format.encoding == RealtimeAudioEncoding::kMuLaw
                                   ? 255
                                   : 213)));
    Q4T_CHECK(output.Bytes() == bytes.size() && output.EndTicks() == 96000);
    const auto& transcript = observer.events.back();
    Q4T_CHECK(transcript.GetString("delta") == "hello");
    Q4T_CHECK(transcript.Find("start_ms")->number == 1000);
    Q4T_CHECK(transcript.Find("end_ms")->number == 2000);
    Q4T_CHECK(transcript.GetString("client_event_id") == "client_cpu");
    const auto calls = observer.calls;
    Q4T_CHECK(!output.Append(Done(pcm.size())).ok());
    Q4T_CHECK(observer.calls == calls);
  }
  return true;
}

Q4T_TEST(live_audio_output_rejects_missing_extra_and_mismatched_eof) {
  for (int mode = 0; mode < 7; ++mode) {
    Observer observer;
    LiveAudioOutput output({}, 0, observer.Sink());
    const size_t bytes = mode == 1 ? 4799 : 4800;
    if (mode != 0)
      Q4T_CHECK(output.Append(Chunk(std::string(bytes, '\0'))).ok());
    auto done = Done(4800);
    if (mode == 2) ++done.receipt.input_samples;
    if (mode == 3) ++done.receipt.output_samples;
    if (mode == 4) ++done.receipt.bytes;
    if (mode == 5) done.bytes = "extra";
    if (mode == 6) done.transcript.clear();
    Q4T_CHECK(!output.Append(done).ok() && !output.Completed());
    Q4T_CHECK(observer.Count("session.output_transcript.delta") == 0);
    const auto calls = observer.calls;
    Q4T_CHECK(!output.Append(Done(bytes)).ok() && observer.calls == calls);
  }
  return true;
}

Q4T_TEST(live_audio_output_cancel_and_failed_sink_never_complete) {
  for (int mode = 0; mode < 4; ++mode) {
    Observer observer;
    bool interrupted = false;
    LiveAudioOutput output({}, 0, observer.Sink(), [&] { return interrupted; });
    Q4T_CHECK(output.Append(Chunk(std::string(4800, '\0'))).ok());
    if (mode == 0) output.Cancel();
    if (mode == 1) interrupted = true;
    if (mode >= 2) {
      observer.fail_at = observer.calls + 1;
      observer.uncertain = true;
      observer.throws = mode == 3;
    }
    Q4T_CHECK(!output.Append(Done(4800)).ok() && !output.Completed());
    Q4T_CHECK(observer.Count("session.output_transcript.delta") == 0);
    const auto calls = observer.calls;
    Q4T_CHECK(!output.Append(Done(4800)).ok() && observer.calls == calls);
  }
  return true;
}

Q4T_TEST(live_audio_output_enforces_timeline_and_byte_capacity) {
  Observer observer;
  observer.retain = false;
  LiveAudioOutput invalid({}, kRealtimeSessionTicks, observer.Sink());
  Q4T_CHECK(!invalid.Valid());
  LiveAudioOutput boundary({}, kRealtimeSessionTicks - 2, observer.Sink());
  Q4T_CHECK(boundary.Append(Chunk(std::string(2, '\0'))).ok());
  Q4T_CHECK(boundary.Append(Done(2)).ok());
  Q4T_CHECK(boundary.EndTicks() == kRealtimeSessionTicks);
  LiveAudioOutput overflow({}, kRealtimeSessionTicks - 2, observer.Sink());
  Q4T_CHECK(!overflow.Append(Chunk(std::string(4, '\0'))).ok());
  Q4T_CHECK(!overflow.Completed() && overflow.Bytes() == 0);
  LiveAudioOutput bounded({}, 0, observer.Sink());
  const auto chunk = Chunk(std::string(32768, '\0'));
  for (size_t i = 0; i < kRealtimeAudioBytes / chunk.bytes.size(); ++i)
    Q4T_CHECK(bounded.Append(chunk).ok());
  Q4T_CHECK(bounded.Bytes() == kRealtimeAudioBytes);
  Q4T_CHECK(!bounded.Append(Chunk(std::string(2, '\0'))).ok());
  Q4T_CHECK(!bounded.Completed() && bounded.Bytes() == kRealtimeAudioBytes);
  return true;
}

Q4T_TEST(live_frontend_work_keeps_history_audio_and_usage_on_one_owner) {
  Fixture f;
  auto job = Job();
  Q4T_CHECK(f.Start(job, {RealtimeAudioEncoding::kPcm16, 16000}).ok());
  Set(&job.settings, "instructions", String("changed later"));
  Q4T_CHECK(f.Drain());
  Q4T_CHECK(f.work->Result().reply.ok() && f.snapshot_ok);
  Q4T_CHECK(f.executed == 1 && f.acquired == 1 && f.released == 1);
  Q4T_CHECK(!f.released_early && !f.wrong_thread);
  Q4T_CHECK(f.work->Result().usage->total.input == 11);
  Q4T_CHECK(f.work->Result().usage->total.output == 7);
  Q4T_CHECK(f.work->Result().usage->output_audio == 4);
  Q4T_CHECK(f.work->AudioEndTicks() == 52800 && f.work->QueuedBytes() == 0);
  Q4T_CHECK(f.history.Items() == 1 && f.tickets.size() == 1);
  RealtimeSpeechView view;
  Q4T_CHECK(f.speech.Get(f.tickets.begin()->second, &view));
  Q4T_CHECK(view.complete && view.bytes == std::string(4800, '\0'));
  std::string wire;
  Q4T_CHECK(f.observer.Bytes(&wire) && wire == std::string(3200, '\0'));
  Q4T_CHECK(f.observer.Count("session.output_transcript.delta") == 1);
  Q4T_CHECK(!f.work->Start().ok());
  Q4T_CHECK(f.work->Tick().ok() && f.executed == 1 && f.released == 1);
  return true;
}

Q4T_TEST(live_frontend_work_verifies_tool_only_without_invented_speech) {
  for (bool mismatch : {false, true}) {
    Fixture f;
    f.execute = [mismatch](const auto&, const auto&, const auto& delta,
                           const auto&) {
      auto result = Result("");
      ResponseDelta call;
      call.kind = ResponseDelta::Kind::kTool;
      call.call = {0, "call_cpu", "lookup", "{}"};
      result.reply = delta(call);
      Set(&result.message, "tool_calls",
          Array({Object(
              {{"id", String("call_cpu")},
               {"type", String("function")},
               {"function",
                Object({{"name", String("lookup")},
                        {"arguments", String(mismatch ? "[]" : "{}")}})}})}));
      return result;
    };
    Q4T_CHECK(f.Start().ok() && f.Drain());
    Q4T_CHECK(f.work->Result().reply.ok() == !mismatch);
    Q4T_CHECK(f.work->Output().array.size() == 1);
    Q4T_CHECK(f.work->Output().array[0].GetString("type") == "function_call");
    Q4T_CHECK(f.observer.events.empty() && f.work->AudioEndTicks() == 0);
    Q4T_CHECK(f.executed == 1 && f.released == 1);
  }
  return true;
}

Q4T_TEST(live_frontend_work_rejects_unresolved_or_unbound_execution) {
  for (int mode = 0; mode < 8; ++mode) {
    Fixture f;
    auto job = Job();
    RealtimeAudioFormat format;
    if (mode == 0) job.snapshot.pending_inputs.push_back(0);
    if (mode == 1) job.snapshot.pending_outputs.push_back(0);
    if (mode == 2) f.cb.execute = {};
    if (mode == 3) format.rate = 44100;
    if (mode == 4)
      Set(&job.settings, "output_modalities", Array({String("text")}));
    if (mode == 5) job.mcp_definitions_only = true;
    if (mode == 6) job.snapshot.owner.reserve(1024);
    if (mode == 7) job.snapshot.tickets.reserve(1024);
    // Move the reserved capacities to the worker. Copying an empty vector
    // correctly drops spare storage and would no longer exercise this bound.
    Q4T_CHECK(!f.Start(std::move(job), format).ok());
    Q4T_CHECK(f.executed == 0 && f.acquired == 0 && f.released == 0);
    Q4T_CHECK(f.observer.events.empty() && f.history.Items() == 0);
  }
  Fixture f;
  auto foreign =
      std::make_shared<RequestControl>("foreign", "", Clock::now() + 3s);
  f.cb.acquire = [foreign](const auto&) { return foreign; };
  Q4T_CHECK(!f.Start().ok() && !foreign->Cancelled());
  Q4T_CHECK(f.executed == 0 && f.released == 0);
  return true;
}

Q4T_TEST(live_frontend_work_cancel_joins_actual_borrower_before_release) {
  Fixture f;
  f.execute = [](const auto& job, const auto& control, const auto&,
                 const auto&) {
    while (!control->Cancelled()) std::this_thread::sleep_for(1ms);
    auto result = Result();
    result.reply =
        Error(409, control->Reason(), job.settings.GetString("instructions"));
    return result;
  };
  Q4T_CHECK(f.Start().ok() && Wait([&] { return f.entered.load(); }));
  Q4T_CHECK(f.released == 0 && !f.exited);
  f.work->Cancel();
  Q4T_CHECK(f.Drain());
  Q4T_CHECK(f.work->Result().state == RealtimeResponseEnd::State::kCancelled);
  Q4T_CHECK(f.work->Result().usage->total.output == 7);
  Q4T_CHECK(f.released == 1 && !f.released_early && !f.wrong_thread);
  Q4T_CHECK(f.observer.events.empty());
  return true;
}

Q4T_TEST(live_frontend_work_bounded_mailbox_wakes_producer_on_cancel) {
  Fixture f;
  f.execute = [&f](const auto&, const auto&, const auto& delta, const auto&) {
    auto result = Result();
    for (size_t i = 0; i < 10000; ++i) {
      result.reply = delta(Text(std::string(4096, 'x')));
      if (!result.reply.ok()) break;
      ++f.produced;
    }
    return result;
  };
  Q4T_CHECK(f.Start().ok());
  Q4T_CHECK(
      Wait([&] { return f.produced == LiveFrontendWork::kMailboxItems; }));
  Q4T_CHECK(f.work->QueuedBytes() > 0);
  Q4T_CHECK(f.work->QueuedBytes() <= LiveFrontendWork::kMailboxBytes);
  Q4T_CHECK(!f.exited && f.released == 0);
  const auto begin = Clock::now();
  f.work->Cancel();
  Q4T_CHECK(f.Drain() && Clock::now() - begin < 1s);
  Q4T_CHECK(f.produced == LiveFrontendWork::kMailboxItems);
  Q4T_CHECK(f.work->QueuedBytes() == 0 && !f.released_early);
  Q4T_CHECK(f.observer.events.empty() && f.work->Result().usage.has_value());
  return true;
}

Q4T_TEST(live_frontend_work_failure_preserves_actual_usage_without_replay) {
  for (int mode = 0; mode < 4; ++mode) {
    Fixture f;
    if (mode == 0) {
      f.execute = [](const auto&, const auto&, const auto&, const auto&) {
        auto result = Result();
        result.reply =
            Error(503, "fixture_model_failure", "Actual compute failed");
        return result;
      };
    } else if (mode == 1 || mode == 3) {
      f.observer.throws = mode == 3;
      f.observer.fail_at = 1;
      f.observer.uncertain = true;
    } else {
      f.execute = [](const auto&, const auto&, const auto& delta, const auto&) {
        auto result = Result();
        result.reply =
            delta(Text(std::string(LiveFrontendWork::kMailboxBytes + 1, 'x')));
        return result;
      };
    }
    Q4T_CHECK(f.Start().ok() && f.Drain());
    Q4T_CHECK(!f.work->Result().reply.ok());
    Q4T_CHECK(f.work->Result().state == RealtimeResponseEnd::State::kFailed);
    Q4T_CHECK(f.work->Result().usage->total.input == 11);
    Q4T_CHECK(f.work->Result().usage->total.output == 7);
    Q4T_CHECK(f.observer.Count("session.output_transcript.delta") == 0);
    Q4T_CHECK(f.executed == 1 && f.released == 1 && !f.released_early);
    Q4T_CHECK(f.work->Tick().ok() && !f.work->Start().ok());
    Q4T_CHECK(f.executed == 1 && f.released == 1);
  }
  return true;
}
