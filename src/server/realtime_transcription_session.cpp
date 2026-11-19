#include "q4t/server/realtime_transcription_session.h"

#include <algorithm>
#include <bit>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_asr_policy.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
Json Event(std::string type) {
  return Object({{"type", String(std::move(type))},
                 {"event_id", String(detail::NewRequestId("event_"))}});
}
Json Failure(const ObjectReply& reply) {
  return Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())},
       {"param",
        reply.value.Has("param") ? *reply.value.Find("param") : Json{}}});
}
}  // namespace

RealtimeTranscriptionSession::RealtimeTranscriptionSession(
    std::string id, WebSocketOutput& output,
    RealtimeTranscriptionCallbacks callbacks, uint32_t max_audio_seconds,
    size_t message_bytes)
    : id_(std::move(id)),
      output_(output),
      callbacks_(std::move(callbacks)),
      max_item_ticks_(uint64_t(max_audio_seconds) * kRealtimeTicksPerSecond),
      message_bytes_(message_bytes) {}

RealtimeTranscriptionSession::~RealtimeTranscriptionSession() {
  Cancel();
  Join();
}
void RealtimeTranscriptionSession::Release() noexcept {
  if (!control_) return;
  try {
    if (callbacks_.release) callbacks_.release(control_);
  } catch (...) {
  }
  control_.reset();
}
void RealtimeTranscriptionSession::Cancel() noexcept {
  cancelled_ = true;
  if (control_) control_->Cancel(RequestControl::State::kDisconnect);
  queue_.Cancel(1);
  for (auto& job : jobs_)
    if (job.stream) job.stream->Cancel(job.audio.epoch);
}
void RealtimeTranscriptionSession::Join() noexcept {
  if (worker_.joinable()) worker_.join();
  Release();
}
bool RealtimeTranscriptionSession::Emit(Json event, bool deferred) {
  if (cancelled_ || (callbacks_.interrupted && callbacks_.interrupted()))
    return false;
  auto text = OutputJson(event);
  if (!deferred && vad_events_.empty())
    return output_.TryPush(WebSocketOpcode::kText, text);
  if (vad_events_.size() >= 4 || text.size() > 8192 - vad_event_bytes_)
    return false;
  if (vad_events_.empty())
    vad_event_deadline_ = Clock::now() + std::chrono::seconds(15);
  vad_event_bytes_ += text.size();
  vad_events_.push_back(std::move(text));
  return true;
}
bool RealtimeTranscriptionSession::FlushVadEvents() {
  while (!vad_events_.empty()) {
    if (output_.Closed() || Clock::now() >= vad_event_deadline_) return false;
    if (!output_.TryPush(WebSocketOpcode::kText, vad_events_.front()))
      return true;
    vad_event_bytes_ -= vad_events_.front().size();
    vad_events_.pop_front();
  }
  return true;
}
bool RealtimeTranscriptionSession::ErrorEvent(ObjectReply reply,
                                              std::string_view event_id) {
  auto error = Failure(reply);
  const auto id = event_id.empty() ? reply.value.GetString("event_id")
                                   : std::string(event_id);
  if (!id.empty()) Set(&error, "event_id", String(id));
  auto event = Event("error");
  Set(&event, "error", std::move(error));
  return Emit(std::move(event));
}
bool RealtimeTranscriptionSession::SettingsEvent(bool legacy, bool created) {
  auto session = RealtimeTranscriptionSettingsJson(settings_, legacy);
  Set(&session, "id", String(id_));
  Set(&session, "object", String("realtime.transcription_session"));
  auto event =
      Event(std::string(legacy ? "transcription_session." : "session.") +
            (created ? "created" : "updated"));
  Set(&event, "session", std::move(session));
  return Emit(std::move(event));
}
bool RealtimeTranscriptionSession::Start() {
  if (started_ || cancelled_ || !queue_.Valid() || id_.empty() ||
      !output_.Valid() || max_item_ticks_ == 0 ||
      max_item_ticks_ > 600 * kRealtimeTicksPerSecond || message_bytes_ == 0 ||
      message_bytes_ > kWebSocketMessageLimit || !callbacks_.validate ||
      !callbacks_.acquire || !callbacks_.release || !callbacks_.execute)
    return false;
  if (!callbacks_.validate(settings_).ok()) return false;
  started_ = true;
  return SettingsEvent(false, true);
}
bool RealtimeTranscriptionSession::Message(WebSocketOpcode opcode,
                                           std::string_view message) {
  if (!started_ || cancelled_ ||
      (callbacks_.interrupted && callbacks_.interrupted()))
    return false;
  if (opcode != WebSocketOpcode::kText)
    return ErrorEvent(Error(400, "invalid_event", "JSON text event required"));
  RealtimeTranscriptionCommand command;
  auto reply =
      ParseRealtimeTranscriptionCommand(message, message_bytes_, &command);
  if (!reply.ok()) return ErrorEvent(std::move(reply));
  const auto fail = [&](std::string code, std::string text) {
    return ErrorEvent(Error(400, std::move(code), std::move(text)),
                      command.event_id);
  };
  using K = RealtimeTranscriptionCommandKind;
  if (command.kind == K::kUpdate) {
    RealtimeTranscriptionSettings next;
    reply = UpdateRealtimeTranscriptionSettings(command.session, command.legacy,
                                                settings_, &next);
    if (reply.ok()) reply = callbacks_.validate(next);
    if (!reply.ok()) return ErrorEvent(std::move(reply), command.event_id);
    if (!next.delay.empty() &&
        (!RealtimeAsrDelayPolicy(next.delay).Valid() ||
         next.turn != RealtimeTranscriptionSettings::Turn::kManual))
      return fail("realtime_delay_turn_conflict",
                  "Continuous transcription uses explicit audio commits");
    const auto& a = settings_.request.chunking;
    const auto& b = next.request.chunking;
    const bool pipeline_changed = next.format != settings_.format ||
                                  next.noise != settings_.noise ||
                                  next.delay != settings_.delay;
    const bool vad_changed = pipeline_changed || next.turn != settings_.turn ||
                             next.format != settings_.format ||
                             a.threshold != b.threshold ||
                             a.prefix_padding_ms != b.prefix_padding_ms ||
                             a.silence_duration_ms != b.silence_duration_ms;
    if (next.turn == RealtimeTranscriptionSettings::Turn::kServerVad &&
        !callbacks_.vad_step)
      return fail("realtime_processor_unavailable",
                  "VAD processor unavailable");
    const bool pending =
        live_input_ || stream_fault_ || queue_.PendingBytes() ||
        (noise_input_ &&
         (noise_input_->PartialSample() ||
          noise_input_->AcceptedTicks() != queue_.PendingBeginTicks()));
    if (vad_changed && pending)
      return fail("input_audio_buffer_not_empty",
                  "Clear or commit pending audio before changing turn policy");
    if (pipeline_changed) {
      if (!jobs_.empty())
        return fail("input_audio_buffer_not_empty",
                    "Finish prior items before changing the audio pipeline");
      const auto format =
          next.noise == RealtimeTranscriptionSettings::Noise::kNone
              ? next.format
              : RealtimeAudioFormat{RealtimeAudioEncoding::kFloat32, 16000};
      const auto status = queue_.ChangeFormat(1, format);
      if (!status)
        return fail("input_audio_buffer_not_empty", status.message());
    }
    settings_ = std::move(next);
    if (pipeline_changed) ResetNoise(queue_.AcceptedTicks());
    if (vad_changed) ResetVad();
    return SettingsEvent(command.legacy);
  }
  if (!settings_.delay.empty()) return StreamMessage(command);
  if (command.kind == K::kAppend) {
    const size_t width =
        settings_.format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    if (noise_input_) {
      const auto samples =
          (command.audio.size() + size_t(noise_input_->PartialSample())) /
          width;
      const auto ticks =
          samples * (kRealtimeTicksPerSecond / settings_.format.rate);
      if (noise_input_->AcceptedTicks() - queue_.PendingBeginTicks() + ticks >
          max_item_ticks_)
        return fail("audio_duration_limit",
                    "Uncommitted audio duration exceeded");
      const auto status = noise_input_->Append(
          {reinterpret_cast<const uint8_t*>(command.audio.data()),
           command.audio.size()},
          [&](auto samples) { return ProcessedAudio(samples, true); });
      if (!status) {
        ErrorEvent(Error(503, "audio_processing_failed", status.message()),
                   command.event_id);
        return false;
      }
      return true;
    }
    const auto samples = (queue_.PendingBytes() + command.audio.size()) / width;
    if (samples >
        max_item_ticks_ / (kRealtimeTicksPerSecond / settings_.format.rate))
      return fail("audio_duration_limit",
                  "Uncommitted audio duration exceeded");
    const auto status = queue_.Append(
        1, {reinterpret_cast<const uint8_t*>(command.audio.data()),
            command.audio.size()});
    return status ? true : fail("input_audio_buffer_full", status.message());
  }
  if (command.kind == K::kClear) {
    const auto status = queue_.Clear(1);
    if (!status) return fail("input_audio_buffer_invalid", status.message());
    if (noise_input_) {
      const auto ticks = noise_input_->AcceptedTicks();
      if (!queue_.AdvanceEmptyClock(1, ticks)) return false;
      ResetNoise(ticks);
    }
    ResetVad();
    return Emit(Event("input_audio_buffer.cleared"));
  }
  // Explicit commit owns the complete pending tail, including any samples not
  // yet analyzed by VAD. It starts a new detector epoch at the accepted clock.
  if (noise_input_) {
    if (noise_input_->PartialSample() || noise_input_->FinalSamples() == 0 ||
        queue_.Items() == kRealtimeQueuedItems)
      return fail(
          "input_audio_buffer_commit_failed",
          "Commit requires complete samples and available item capacity");
    const auto status = noise_input_->Finish(
        [&](auto samples) { return ProcessedAudio(samples, false); });
    if (!status) {
      ErrorEvent(Error(503, "audio_processing_failed", status.message()),
                 command.event_id);
      return false;
    }
  } else if (queue_.PendingBytes() %
             (settings_.format.encoding == RealtimeAudioEncoding::kPcm16 ? 2
                                                                         : 1))
    return fail("input_audio_buffer_commit_failed",
                "Commit requires complete uncommitted samples");
  return Commit(queue_.AcceptedTicks(), command.event_id, false);
}

void RealtimeTranscriptionSession::ResetNoise(uint64_t ticks) {
  noise_input_.reset();
  using N = RealtimeTranscriptionSettings::Noise;
  if (settings_.noise == N::kNone || !settings_.delay.empty()) return;
  noise_input_ = std::make_unique<RealtimeNoiseInput>(
      settings_.format,
      settings_.noise == N::kNearField ? audio::NoiseReduction::kNearField
                                       : audio::NoiseReduction::kFarField,
      ticks, callbacks_.interrupted);
}
Status RealtimeTranscriptionSession::ProcessedAudio(
    std::span<const float> samples, bool advance) {
  if (samples.size() > 512)
    return Status::Fail("Realtime processed block exceeds its fixed bound");
  std::array<uint8_t, 2048> bytes;
  for (size_t i = 0; i < samples.size(); ++i) {
    const auto bits = std::bit_cast<uint32_t>(samples[i]);
    for (size_t b = 0; b < 4; ++b) bytes[i * 4 + b] = uint8_t(bits >> (8 * b));
  }
  auto status = queue_.Append(1, std::span(bytes).first(samples.size() * 4));
  if (status && advance && !Tick())
    return Status::Fail("Realtime processed audio consumer stopped");
  return status;
}

void RealtimeTranscriptionSession::ResetVad() {
  speech_id_.clear();
  vad_.reset();
  if (settings_.turn != RealtimeTranscriptionSettings::Turn::kServerVad) return;
  const auto& options = settings_.request.chunking;
  vad_ = std::make_unique<RealtimeVad>(
      queue_.Format(), queue_.AcceptedTicks(),
      RealtimeVadOptions{options.threshold, uint32_t(options.prefix_padding_ms),
                         uint32_t(options.silence_duration_ms)},
      callbacks_.vad_step);
}
bool RealtimeTranscriptionSession::SpeechEvent(bool started, uint64_t ticks,
                                               bool deferred) {
  auto event = Event(started ? "input_audio_buffer.speech_started"
                             : "input_audio_buffer.speech_stopped");
  Set(&event, "item_id", String(speech_id_));
  // Wire milliseconds follow OpenAI's integer convention. Queue/processor
  // ownership always uses exact integer sample ticks, not this projection.
  Set(&event, started ? "audio_start_ms" : "audio_end_ms", Number(ticks / 48));
  return Emit(std::move(event), deferred);
}
bool RealtimeTranscriptionSession::AdvanceVad() {
  if (!vad_) return true;
  RealtimeVadProgress progress;
  const auto status = vad_->Advance(queue_, &progress);
  if (!status) return false;
  if (!progress.frame) return true;
  if (progress.started) {
    speech_id_ = detail::NewRequestId("item_");
    if (!SpeechEvent(true, progress.begin_ticks, true)) return false;
  }
  if (progress.stopped) return Commit(progress.end_ticks, {}, true);
  return bool(queue_.TrimPendingBefore(1, progress.trim_before));
}
bool RealtimeTranscriptionSession::Commit(uint64_t end_ticks,
                                          std::string_view event_id,
                                          bool automatic) {
  // Allocate the snapshot and identities before mutating the committed queue.
  RealtimeTranscriptionJob job;
  job.settings = settings_;
  job.id = speech_id_.empty() ? detail::NewRequestId("item_") : speech_id_;
  job.previous_id = previous_id_;
  jobs_.push_back(std::move(job));
  auto& last = jobs_.back();
  const auto status = queue_.CommitPrefix(1, end_ticks, &last.audio);
  if (!status) {
    jobs_.pop_back();
    const bool sent = ErrorEvent(
        Error(400, "input_audio_buffer_commit_failed", status.message()),
        event_id);
    return sent && !automatic;
  }
  previous_id_ = last.id;
  if (!speech_id_.empty() && !SpeechEvent(false, end_ticks, automatic))
    return false;
  if (automatic)
    speech_id_.clear();
  else {
    if (noise_input_) {
      const auto ticks = noise_input_->AcceptedTicks();
      if (!queue_.AdvanceEmptyClock(1, ticks)) return false;
      ResetNoise(ticks);
    }
    ResetVad();
  }
  return CommitEvents(last, automatic);
}
bool RealtimeTranscriptionSession::CommitEvents(
    const RealtimeTranscriptionJob& last, bool automatic) {
  auto committed = Event("input_audio_buffer.committed");
  Set(&committed, "item_id", String(last.id));
  Set(&committed, "previous_item_id",
      last.previous_id.empty() ? Json{} : String(last.previous_id));
  if (!Emit(std::move(committed), automatic)) return false;
  auto created = Event("conversation.item.created");
  Set(&created, "previous_item_id",
      last.previous_id.empty() ? Json{} : String(last.previous_id));
  Set(&created, "item",
      Object({{"id", String(last.id)},
              {"object", String("realtime.item")},
              {"type", String("message")},
              {"role", String("user")},
              {"status", String("completed")},
              {"content", Array({Object({{"type", String("input_audio")},
                                         {"transcript", Json{}}})})}}));
  return Emit(std::move(created), automatic);
}

bool RealtimeTranscriptionSession::StreamMessage(
    const RealtimeTranscriptionCommand& command) {
  using K = RealtimeTranscriptionCommandKind;
  const auto fail = [&](std::string code, std::string message) {
    return ErrorEvent(Error(400, std::move(code), std::move(message)),
                      command.event_id);
  };
  if (command.kind == K::kClear) {
    if (live_input_) {
      live_input_->Cancel(stream_epoch_);
      if (control_ && !jobs_.empty() && jobs_.front().stream == live_input_)
        control_->Cancel(RequestControl::State::kExplicit);
    }
    // Old jobs retain storage until their worker joins and terminal is queued.
    // A subsequent append allocates a different, separately charged owner.
    live_input_.reset();
    stream_processor_.reset();
    live_id_.clear();
    stream_fault_ = false;
    return Emit(Event("input_audio_buffer.cleared"));
  }
  if (stream_fault_)
    return fail("input_audio_buffer_failed",
                "Clear the failed streaming item before appending new audio");
  if (command.kind == K::kAppend) {
    if (command.audio.empty()) return true;
    const size_t width =
        settings_.format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    const auto samples =
        (command.audio.size() +
         size_t(stream_processor_ && stream_processor_->PartialSample())) /
        width;
    const auto ticks =
        samples * (kRealtimeTicksPerSecond / settings_.format.rate);
    const auto accepted = stream_processor_
                              ? stream_processor_->AcceptedTicks() -
                                    jobs_.back().audio.begin_ticks
                              : 0;
    if (ticks > max_item_ticks_ - accepted)
      return fail("audio_duration_limit",
                  "Uncommitted audio duration exceeded");
    if (!live_input_) {
      constexpr size_t kStreamStorage = 64ULL << 20;
      const auto maximum = max_item_ticks_ / 3;
      if (jobs_.size() >= kRealtimeQueuedItems ||
          maximum * sizeof(float) > kStreamStorage - stream_storage_)
        return fail("input_audio_buffer_full",
                    "Streaming audio capacity exhausted");
      const auto policy = RealtimeAsrDelayPolicy(settings_.delay);
      if (!policy.Valid() ||
          settings_.turn != RealtimeTranscriptionSettings::Turn::kManual)
        return fail("realtime_delay_turn_conflict",
                    "Continuous ASR policy invalid");
      const auto epoch = stream_epoch_ + 1;
      auto input = std::make_shared<AsrStreamInput>(epoch, maximum,
                                                    policy.chunk_samples);
      using N = RealtimeTranscriptionSettings::Noise;
      using M = audio::NoiseReduction;
      auto processor = std::make_unique<RealtimeNoiseInput>(
          settings_.format,
          settings_.noise == N::kNone        ? M::kOff
          : settings_.noise == N::kNearField ? M::kNearField
                                             : M::kFarField,
          queue_.AcceptedTicks(), callbacks_.interrupted);
      if (!input->Valid() || !processor->Valid()) return false;
      RealtimeTranscriptionJob job;
      job.settings = settings_;
      job.id = detail::NewRequestId("item_");
      job.previous_id = previous_id_;
      job.audio.epoch = epoch;
      job.audio.begin_ticks = queue_.AcceptedTicks();
      job.audio.format = {RealtimeAudioEncoding::kFloat32, 16000};
      job.stream = input;
      jobs_.push_back(std::move(job));
      stream_storage_ += input->StorageBytes();
      stream_epoch_ = epoch;
      live_input_ = std::move(input);
      live_id_ = jobs_.back().id;
      stream_processor_ = std::move(processor);
    }
    const auto status = stream_processor_->Append(
        {reinterpret_cast<const uint8_t*>(command.audio.data()),
         command.audio.size()},
        [&](auto samples) {
          return live_input_->Append(stream_epoch_, samples);
        });
    if (!status) {
      ErrorEvent(Error(503, "audio_processing_failed", status.message()),
                 command.event_id);
      return false;
    }
    return bool(
        queue_.AdvanceEmptyClock(1, stream_processor_->AcceptedTicks()));
  }
  if (command.kind != K::kCommit || !live_input_ || !stream_processor_ ||
      stream_processor_->PartialSample() ||
      stream_processor_->FinalSamples() == 0)
    return fail("input_audio_buffer_commit_failed",
                "Commit requires complete uncommitted samples");
  const auto status = stream_processor_->Finish([&](auto samples) {
    return live_input_->Append(stream_epoch_, samples);
  });
  if (!status) {
    ErrorEvent(Error(503, "audio_processing_failed", status.message()),
               command.event_id);
    return false;
  }
  // Queue both standard commit/item events before EOF permits final output.
  if (jobs_.empty() || jobs_.back().id != live_id_ ||
      !CommitEvents(jobs_.back(), false) || !live_input_->Seal(stream_epoch_))
    return false;
  previous_id_ = live_id_;
  live_input_.reset();
  stream_processor_.reset();
  live_id_.clear();
  return true;
}

bool RealtimeTranscriptionSession::Tick() {
  if (!started_ || cancelled_ ||
      (callbacks_.interrupted && callbacks_.interrupted()))
    return false;
  if (!FlushVadEvents()) return false;
  if (!vad_events_.empty()) return true;
  if (!AdvanceVad()) return false;
  if (!FlushVadEvents()) return false;
  if (!vad_events_.empty()) return true;
  if (worker_.joinable() && done_.load(std::memory_order_acquire)) {
    worker_.join();
    const auto& job = jobs_.front();
    if (result_.ok() && job.stream && !job.stream->Drained())
      result_ = Error(502, "transcription_result_invalid",
                      "Streaming audio did not drain before completion");
    if (result_.ok() && (!result_.value.Find("text") ||
                         !result_.value.Find("text")->IsString() ||
                         !result_.value.Find("usage") ||
                         !result_.value.Find("usage")->IsObject()))
      result_ = Error(502, "transcription_result_invalid",
                      "Transcription text or accounting missing");
    if (result_.ok() && (control_->Cancelled() || !control_->Finish()))
      result_ = Error(409, control_->Reason(), "Transcription cancelled");
    if (!result_.ok() && job.stream && job.stream == live_input_) {
      job.stream->Cancel(job.audio.epoch);
      stream_fault_ = true;
      live_input_.reset();
      stream_processor_.reset();
      live_id_.clear();
    }
    auto event =
        Event(std::string("conversation.item.input_audio_transcription.") +
              (result_.ok() ? "completed" : "failed"));
    Set(&event, "item_id", String(jobs_.front().id));
    Set(&event, "content_index", Number(0));
    if (result_.ok()) {
      Set(&event, "transcript", String(result_.value.GetString("text")));
      for (const auto key : {"usage", "logprobs"})
        if (const auto* value = result_.value.Find(key))
          Set(&event, key, *value);
    } else {
      Set(&event, "error", Failure(result_));
    }
    terminal_ = OutputJson(event);
    terminal_deadline_ = Clock::now() + std::chrono::seconds(15);
    Release();
  }
  if (!terminal_.empty()) {
    if (terminal_.size() > kWebSocketOutputMessageBytes || output_.Closed() ||
        Clock::now() >= terminal_deadline_)
      return false;
    if (!output_.TryPush(WebSocketOpcode::kText, terminal_)) return true;
    terminal_.clear();
    const auto& job = jobs_.front();
    if (job.stream)
      stream_storage_ -= job.stream->StorageBytes();
    else if (!queue_.Acknowledge(job.audio.epoch, job.audio.sequence))
      return false;
    jobs_.pop_front();
  }
  if (worker_.joinable() || jobs_.empty()) return true;
  if (!jobs_.front().stream) {
    const auto* audio = queue_.Front();
    if (!audio || *audio != jobs_.front().audio) return false;
  }
  control_ = callbacks_.acquire(jobs_.front().id);
  if (!control_) return false;
  // Preallocate the exception fallback before starting the worker. Exception
  // handling inside the producer must not allocate during host exhaustion.
  result_ =
      Error(500, "transcription_failed", "Transcription execution failed");
  done_.store(false, std::memory_order_relaxed);
  const auto bytes = jobs_.front().stream
                         ? std::array<std::span<const uint8_t>, 2>{}
                         : queue_.FrontBytes();
  const auto* borrowed_job = &jobs_.front();
  worker_ = std::thread([this, bytes, borrowed_job] {
    const auto& job = *borrowed_job;
    try {
      const auto delta = [&](const Json& part) {
        auto event = Event("conversation.item.input_audio_transcription.delta");
        Set(&event, "item_id", String(job.id));
        Set(&event, "content_index", Number(0));
        Set(&event, "delta", String(part.GetString("delta")));
        if (const auto* p = part.Find("logprobs")) Set(&event, "logprobs", *p);
        return output_.Push(WebSocketOpcode::kText, OutputJson(event),
                            *control_);
      };
      result_ = callbacks_.execute(job, bytes, control_, delta);
    } catch (...) {
      // Preserve the preallocated failure; no success from a partial stream.
    }
    done_.store(true, std::memory_order_release);
  });
  return true;
}
}  // namespace q4t::server
