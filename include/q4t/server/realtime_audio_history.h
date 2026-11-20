// Bounded immutable processed audio with charges lasting through every borrow.
#pragma once

#include <memory>
#include <vector>

#include "q4t/server/asr_stream_input.h"
#include "q4t/audio/noise_reduction.h"
#include "q4t/server/http_request.h"
#include "q4t/server/realtime_pcm.h"
#include "q4t/server/realtime_transcript_history.h"

namespace q4t::server {
inline constexpr size_t kRealtimeAudioHistoryBytes = 128ULL << 20;

// A manual item owns its exact decoded PCM. A continuous item owns its fixed
// producer storage; Stream() aliases this same control block so any outstanding
// worker/producer keeps both storage and its budget reservation alive.
class RealtimeAudioRecord
    : public std::enable_shared_from_this<RealtimeAudioRecord> {
 public:
  std::span<const float> Samples() const;
  std::shared_ptr<AsrStreamInput> Stream();

 private:
  friend class RealtimeAudioHistory;
  explicit RealtimeAudioRecord(std::shared_ptr<HttpBodyBudget> budget)
      : budget_(std::move(budget)), lease_(*budget_) {}
  // Destruction frees PCM/stream before releasing the shared budget lease.
  std::shared_ptr<HttpBodyBudget> budget_;
  HttpBodyLease lease_;
  std::vector<float> pcm_;
  std::unique_ptr<AsrStreamInput> stream_;
  uint64_t epoch_ = 0;
};

// Single session owner mutates this index. Exact owner/generation tickets
// prevent accidental cross-session lookup or late reuse. Erasing an index
// entry never frees memory still borrowed by a worker or producer.
class RealtimeAudioHistory {
 public:
  explicit RealtimeAudioHistory(std::string owner,
                                size_t bytes = kRealtimeAudioHistoryBytes,
                                size_t items = 128);
  RealtimeAudioHistory(const RealtimeAudioHistory&) = delete;
  RealtimeAudioHistory& operator=(const RealtimeAudioHistory&) = delete;
  bool Valid() const { return valid_; }
  size_t ChargedBytes() const { return budget_->Used(); }
  size_t Items() const { return entries_.size(); }
  // These reserve before allocating PCM; failed work preserves the index and
  // output. The transcript ledger separately controls public commit visibility.
  Status Decode(const RealtimeTranscriptTicket& ticket,
                const RealtimeAudioItem& item,
                const std::array<std::span<const uint8_t>, 2>& bytes,
                size_t max_samples,
                std::shared_ptr<RealtimeAudioRecord>* output,
                const std::function<bool()>& cancelled = {});
  Status BeginStream(const RealtimeTranscriptTicket& ticket, uint64_t epoch,
                     size_t maximum_samples, size_t chunk_samples,
                     std::shared_ptr<RealtimeAudioRecord>* output);
  // A native idle timeout with no received samples; no synthesized waveform.
  Status Empty(const RealtimeTranscriptTicket& ticket,
               std::shared_ptr<RealtimeAudioRecord>* output);
  // Independent finite wire input. Uses the same configured normalization as
  // streaming input and the same retained-audio budget; never borrows its ring.
  Status DecodeFinite(const RealtimeTranscriptTicket& ticket,
                      const RealtimeAudioItem& item,
                      std::span<const uint8_t> bytes,
                      audio::NoiseReduction noise, size_t max_samples,
                      std::shared_ptr<RealtimeAudioRecord>* output,
                      const std::function<bool()>& cancelled = {});
  std::shared_ptr<RealtimeAudioRecord> Find(
      const RealtimeTranscriptTicket& ticket) const;
  Status Erase(const RealtimeTranscriptTicket& ticket);

 private:
  struct Entry {
    RealtimeTranscriptTicket ticket;
    std::shared_ptr<RealtimeAudioRecord> audio;
  };
  bool CanAdd(const RealtimeTranscriptTicket& ticket) const;
  const std::string owner_;
  const size_t maximum_items_;
  const std::shared_ptr<HttpBodyBudget> budget_;
  bool valid_ = false;
  std::vector<Entry> entries_;
};
}  // namespace q4t::server
