#include "q4t/server/asr_stream_state.h"

#include <algorithm>

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
Status Invalid() {
  return Status::Fail("streaming ASR state or budget invalid");
}

// Reuse the existing UTF8 decoder. A prefix is usable only where decoding has
// emitted the original bytes exactly and no scalar remains incomplete. A
// malformed subpart cannot become a fixed prompt via replacement characters.
size_t Utf8Prefix(std::span<const AsrStreamToken> tokens, size_t maximum) {
  detail::Utf8Stream decoder;
  std::string raw;
  size_t matched = 0, fixed = 0;
  for (size_t i = 0; i < maximum; ++i) {
    raw += tokens[i].bytes;
    const auto decoded = decoder.Append(tokens[i].bytes);
    if (decoded.size() > raw.size() - matched ||
        std::string_view(raw).substr(matched, decoded.size()) != decoded)
      break;
    matched += decoded.size();
    if (matched == raw.size()) fixed = i + 1;
  }
  return fixed;
}
}  // namespace

AsrStreamSchedule::AsrStreamSchedule(uint64_t epoch, uint64_t chunk_samples,
                                     uint64_t maximum_samples,
                                     uint64_t maximum_steps)
    : epoch_(epoch),
      chunk_(chunk_samples),
      maximum_(maximum_samples),
      maximum_steps_(maximum_steps) {
  valid_ = epoch_ && chunk_ && chunk_ <= 8 * 16000 && maximum_ &&
           maximum_ <= 600 * 16000 && maximum_steps_ && maximum_steps_ <= 4096;
}
Status AsrStreamSchedule::Append(uint64_t epoch, uint64_t samples) {
  if (!valid_ || epoch != epoch_ || sealed_ || cancelled_ ||
      samples > maximum_ - received_)
    return Invalid();
  received_ += samples;
  return {};
}
Status AsrStreamSchedule::Seal(uint64_t epoch) {
  if (!valid_ || epoch != epoch_ || cancelled_ || !received_) return Invalid();
  sealed_ = true;
  return {};
}
bool AsrStreamSchedule::Ready() const {
  return valid_ && !cancelled_ && !InFlight() && received_ > evaluated_ &&
         (sealed_ || received_ - evaluated_ >= chunk_);
}
Status AsrStreamSchedule::Next(uint64_t epoch, AsrStreamStep* step,
                               bool latest_prefix) {
  if (!step || epoch != epoch_ || !Ready() || sequence_ == maximum_steps_)
    return Invalid();
  const auto end = latest_prefix
                       ? received_
                       : evaluated_ + std::min(chunk_, received_ - evaluated_);
  active_ = {epoch_, ++sequence_, end, sealed_ && end == received_};
  *step = active_;
  return {};
}
Status AsrStreamSchedule::Complete(const AsrStreamStep& step) {
  if (!valid_ || !InFlight() || step != active_) return Invalid();
  // A cancelled borrower can release its exact ticket. It never drains the
  // stream successfully or advances the accepted model result.
  if (!cancelled_) evaluated_ = step.samples;
  active_ = {};
  return {};
}
Status AsrStreamSchedule::Cancel(uint64_t epoch) {
  if (!valid_ || epoch != epoch_) return Invalid();
  cancelled_ = sealed_ = true;
  return {};
}
bool AsrStreamSchedule::Drained() const {
  return valid_ && sealed_ && !cancelled_ && !InFlight() &&
         evaluated_ == received_;
}

AsrStreamPrefix::AsrStreamPrefix(uint64_t epoch, AsrStreamPrefixLimits limits)
    : epoch_(epoch), limits_(limits) {
  valid_ = epoch && limits.tokens && limits.tokens <= 4096 && limits.bytes &&
           limits.bytes <= 64 * 1024 && limits.unfixed_passes <= 16 &&
           limits.rollback_tokens <= limits.tokens && limits.vocabulary > 0;
}
Status AsrStreamPrefix::Fail() {
  failed_ = true;
  return Invalid();
}
Status AsrStreamPrefix::Accept(uint64_t epoch,
                               std::span<const AsrStreamToken> hypothesis,
                               bool actual_eos, bool final) {
  if (!valid_ || epoch != epoch_ || failed_ || cancelled_ || finished_)
    return Invalid();
  if (!actual_eos || hypothesis.size() > limits_.tokens ||
      hypothesis.size() < fixed_ || passes_ == 4096)
    return Fail();
  size_t bytes = 0;
  for (size_t i = 0; i < hypothesis.size(); ++i) {
    const auto& token = hypothesis[i];
    if (token.id < 0 || token.id >= limits_.vocabulary ||
        token.bytes.size() > limits_.bytes - bytes ||
        (i < fixed_ && token != latest_[i]))
      return Fail();
    bytes += token.bytes.size();
  }
  const size_t maximum =
      passes_ + 1 < limits_.unfixed_passes
          ? fixed_
          : std::max(fixed_,
                     hypothesis.size() -
                         std::min(hypothesis.size(), limits_.rollback_tokens));
  const auto fixed = Utf8Prefix(hypothesis, maximum);
  if (fixed < fixed_) return Fail();
  std::vector<AsrStreamToken> next(hypothesis.begin(), hypothesis.end());
  latest_ = std::move(next);
  fixed_ = fixed;
  ++passes_;
  finished_ = final;
  // At final EOS the caller emits the entire remaining hypothesis through the
  // existing AsrTranscript UTF8/output mapper. It is never used as a new
  // prompt.
  return {};
}
Status AsrStreamPrefix::Finalize(uint64_t epoch) {
  if (!valid_ || epoch != epoch_ || failed_ || cancelled_ || !passes_)
    return Invalid();
  finished_ = true;
  return {};
}
Status AsrStreamPrefix::Cancel(uint64_t epoch) {
  if (!valid_ || epoch != epoch_) return Invalid();
  cancelled_ = true;
  return {};  // Keep token/byte storage alive for the current model borrower.
}
}  // namespace q4t::server
