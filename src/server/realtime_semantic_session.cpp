#include "q4t/server/realtime_transcription_session.h"

#include <algorithm>
#include <limits>

#include "object_protocol.h"
#include "q4t/server/asr_response.h"
#include "q4t/server/realtime_asr_policy.h"
#include "q4t/server/realtime_pcm.h"

namespace q4t::server {
bool RealtimeTranscriptionSession::StartSemantic(uint64_t begin_ticks) {
  using namespace object_detail;
  constexpr size_t kStreamStorage = 64ULL << 20;
  const auto maximum = max_item_ticks_ / 3;
  if (semantic_ || live_input_ || speech_id_.empty() ||
      jobs_.size() >= kRealtimeQueuedItems ||
      maximum * sizeof(float) > kStreamStorage - stream_storage_ ||
      stream_epoch_ == std::numeric_limits<uint64_t>::max() ||
      queue_.Format() !=
          RealtimeAudioFormat{RealtimeAudioEncoding::kFloat32, 16000}) {
    ErrorEvent(Error(503, "input_audio_buffer_full",
                     "Semantic transcription storage unavailable"));
    return false;
  }
  RealtimeTranscriptionJob job;
  job.settings = settings_;
  job.id = speech_id_;
  job.previous_id = previous_id_;
  job.audio.epoch = stream_epoch_ + 1;
  job.audio.begin_ticks = begin_ticks;
  job.audio.format = queue_.Format();
  job.semantic = std::make_shared<RealtimeSemanticTurn>(
      job.audio.epoch, begin_ticks, settings_.eagerness);
  if (!job.semantic->Valid()) return false;
  if (!history_.Begin(job.id, kAsrTranscriptBytes, &job.transcript)) {
    ErrorEvent(Error(400, "conversation_capacity_exceeded",
                     "Delete prior items before appending more transcription"));
    return false;
  }
  const auto status = audio_history_.BeginStream(
      job.transcript, job.audio.epoch, maximum,
      RealtimeAsrDelayPolicy("minimal").chunk_samples, &job.processed_audio);
  if (!status) {
    if (!history_.Fail(job.transcript)) return false;
    ErrorEvent(Error(503, "conversation_audio_unavailable", status.message()));
    return false;
  }
  job.stream = job.processed_audio->Stream();
  jobs_.push_back(std::move(job));
  const auto& last = jobs_.back();
  semantic_ = last.semantic;
  semantic_audio_end_ = begin_ticks;
  stream_epoch_ = last.audio.epoch;
  stream_storage_ += last.stream->StorageBytes();
  live_input_ = last.stream;
  live_id_ = last.id;
  return bool(queue_.TrimPendingBefore(1, begin_ticks));
}

bool RealtimeTranscriptionSession::FeedSemantic(uint64_t end_ticks) {
  if (!semantic_ || !live_input_ || end_ticks < semantic_audio_end_ ||
      end_ticks > queue_.AcceptedTicks() ||
      (end_ticks - semantic_audio_end_) % 3)
    return false;
  std::array<float, 512> samples;
  while (semantic_audio_end_ < end_ticks) {
    if (callbacks_.interrupted && callbacks_.interrupted()) return false;
    const auto bytes = queue_.PendingFrom(semantic_audio_end_);
    const auto count =
        std::min<size_t>(samples.size(), (end_ticks - semantic_audio_end_) / 3);
    if (bytes[0].size() + bytes[1].size() < count * 4) return false;
    const auto byte = [&](size_t at) {
      return at < bytes[0].size() ? bytes[0][at]
                                  : bytes[1][at - bytes[0].size()];
    };
    for (size_t i = 0; i < count; ++i)
      samples[i] = DecodeRealtimeSample(RealtimeAudioEncoding::kFloat32,
                                        byte(i * 4), byte(i * 4 + 1),
                                        byte(i * 4 + 2), byte(i * 4 + 3));
    if (!live_input_->Append(stream_epoch_, std::span(samples).first(count)))
      return false;
    semantic_audio_end_ += count * 3;
  }
  return bool(queue_.TrimPendingBefore(1, end_ticks));
}

bool RealtimeTranscriptionSession::FinishSemantic(uint64_t end_ticks,
                                                  bool automatic) {
  if (!semantic_ || jobs_.empty() || jobs_.back().id != live_id_ ||
      !FeedSemantic(end_ticks))
    return false;
  auto& job = jobs_.back();
  if (!history_.Commit(job.transcript) ||
      !SpeechEvent(false, end_ticks, automatic) ||
      !CommitEvents(job, automatic))
    return false;
  // The same ASR worker drains the exact waveform. Closing the turn cancels
  // only its classifier; it cannot kill the final transcription invocation.
  semantic_->Close();
  if (!live_input_->Seal(stream_epoch_)) return false;
  previous_id_ = live_id_;
  live_id_.clear();
  speech_id_.clear();
  semantic_.reset();
  live_input_.reset();
  const auto& options = settings_.request.chunking;
  vad_ = std::make_unique<RealtimeVad>(
      queue_.Format(), end_ticks,
      RealtimeVadOptions{options.threshold, uint32_t(options.prefix_padding_ms),
                         RealtimeSemanticMaximumWaitMs(settings_.eagerness)},
      callbacks_.vad_step);
  return vad_->Valid();
}
}  // namespace q4t::server
