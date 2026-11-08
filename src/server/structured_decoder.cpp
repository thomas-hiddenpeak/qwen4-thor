#include "q4t/server/structured_decoder.h"
#include "q4t/server/structured_admission.h"
#include "q4t/server/sampling.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <exception>
#include <limits>
#include <utility>

#include "grammar_worker_internal.h"

namespace q4t::server {
#ifdef Q4T_ENABLE_XGRAMMAR
struct StructuredDecoder::Impl {
  explicit Impl(int size) : vocab(size), mask((size + 31) / 32, -1) {}
  std::unique_ptr<detail::GrammarProcess> process;
  int vocab;
  std::vector<int32_t> mask;
  bool masked = false;
  bool terminated = false;
  std::vector<SchemaReceipt> receipts;
  GrammarFailure failure = GrammarFailure::kNone;

  Status Transfer(void* data, size_t bytes, bool write,
                  std::chrono::steady_clock::time_point deadline) {
    auto status = process->Transfer(data, bytes, write, deadline);
    if (!status) failure = process->Failure();
    return status;
  }

  Status Receive(std::chrono::steady_clock::time_point deadline,
                 bool ready = false) {
    detail::GrammarReply reply;
    auto status = Transfer(&reply, sizeof(reply), false, deadline);
    if (!status) return status;
    if (reply.magic != detail::kGrammarWireMagic || reply.flags > 3 ||
        reply.reserved || reply.receipts > 128) {
      failure = GrammarFailure::kWorker;
      process->Stop();
      return Status::Fail("invalid grammar worker response");
    }
    if (reply.code != detail::WorkerCode::kOk) {
      failure = reply.code == detail::WorkerCode::kResource
                    ? GrammarFailure::kResource
                : reply.code == detail::WorkerCode::kDependency
                    ? GrammarFailure::kDependency
                    : GrammarFailure::kWorker;
      process->Stop();
      return Status::Fail(
          failure == GrammarFailure::kResource
              ? "grammar worker memory limit exceeded"
          : failure == GrammarFailure::kDependency
              ? "structured output validation dependency unavailable"
              : "grammar worker compilation or matching failed");
    }
    process->ReportRss(reply.reported_rss_bytes);
    terminated = (reply.flags & 1) != 0;
    masked = (reply.flags & 2) != 0;
    const size_t expected = (ready || terminated) ? 0 : mask.size();
    if (reply.words != expected || (ready && (reply.flags || reply.receipts)) ||
        (terminated && masked)) {
      failure = GrammarFailure::kWorker;
      process->Stop();
      return Status::Fail("invalid grammar worker mask shape");
    }
    status = Transfer(mask.data(), expected * sizeof(int32_t), false, deadline);
    if (!status) return status;
    std::vector<SchemaReceipt> next(reply.receipts);
    if (!next.empty())
      status = Transfer(next.data(), next.size() * sizeof(SchemaReceipt), false,
                        deadline);
    if (!status) return status;
    if (next.size() < receipts.size() ||
        !std::equal(receipts.begin(), receipts.end(), next.begin())) {
      failure = GrammarFailure::kWorker;
      process->Stop();
      return Status::Fail("grammar worker changed completed value receipts");
    }
    receipts = std::move(next);
    return {};
  }
};
#else
struct StructuredDecoder::Impl {};
#endif

StructuredDecoder::StructuredDecoder(std::unique_ptr<Impl> impl)
    : impl_(std::move(impl)) {}
StructuredDecoder::~StructuredDecoder() = default;

GrammarFailure StructuredDecoder::Failure() const {
#ifdef Q4T_ENABLE_XGRAMMAR
  return impl_->failure;
#else
  return GrammarFailure::kUnavailable;
#endif
}

Status StructuredDecoder::Create(const OutputConstraints& plan,
                                 const std::vector<std::string>& decoded_vocab,
                                 int model_vocab,
                                 const std::vector<int32_t>& stop_ids,
                                 std::unique_ptr<StructuredDecoder>* out,
                                 const GrammarControl& control,
                                 GrammarFailure* failure) {
  out->reset();
  if (failure) *failure = GrammarFailure::kNone;
#ifdef Q4T_ENABLE_XGRAMMAR
  if (!plan.active || plan.structural_tag.empty() ||
      plan.structural_tag.size() > detail::kGrammarDescriptionLimit ||
      model_vocab <= 0 ||
      model_vocab > 300000 || decoded_vocab.empty() ||
      decoded_vocab.size() > static_cast<size_t>(model_vocab) ||
      stop_ids.empty() || stop_ids.size() > 32)
    return Status::Fail("invalid grammar/vocabulary configuration");
  for (const int32_t id : stop_ids)
    if (id < 0 || static_cast<size_t>(id) >= decoded_vocab.size())
      return Status::Fail("grammar stop token is outside decoded vocabulary");
  auto check = [&]() -> Status {
    if (control.cancelled && control.cancelled()) {
      if (failure) *failure = GrammarFailure::kCancelled;
      return Status::Fail("grammar preparation cancelled");
    }
    if (std::chrono::steady_clock::now() >= control.compile_deadline) {
      if (failure) *failure = GrammarFailure::kTimeout;
      return Status::Fail("grammar compilation time limit exceeded");
    }
    return Status();
  };
  try {
    auto status = check();
    if (!status) return status;
    size_t size = 16 + plan.structural_tag.size() + stop_ids.size() * 4;
    VocabularyStorageBudget storage;
    if (!storage.Charge(decoded_vocab.capacity() * sizeof(std::string)))
      throw std::bad_alloc();
    for (const auto& token : decoded_vocab) {
      if (!storage.Charge(token.capacity() + 1) ||
          token.size() + 4 > detail::kGrammarInputLimit - size)
        throw std::bad_alloc();
      size += token.size() + 4;
    }
    std::vector<char> bytes(size);
    size_t offset = 0;
    auto integer = [&](uint32_t value) {
      std::memcpy(bytes.data() + offset, &value, sizeof(value));
      offset += sizeof(value);
    };
    auto string = [&](const std::string& value) {
      integer(static_cast<uint32_t>(value.size()));
      std::memcpy(bytes.data() + offset, value.data(), value.size());
      offset += value.size();
    };
    integer(model_vocab);
    integer(static_cast<uint32_t>(decoded_vocab.size()));
    integer(static_cast<uint32_t>(stop_ids.size()));
    string(plan.structural_tag);
    for (int32_t token : stop_ids) integer(token);
    for (const auto& token : decoded_vocab) string(token);
    status = check();
    if (!status) return status;
    auto impl = std::make_unique<Impl>(model_vocab);
    GrammarFailure start_failure = GrammarFailure::kNone;
    status = detail::GrammarProcess::Start("--q4t-grammar-worker", control,
                                           &impl->process, &start_failure);
    if (!status) {
      if (failure)
        *failure = start_failure == GrammarFailure::kUnavailable
                       ? start_failure
                       : GrammarFailure::kWorker;
      return status;
    }
    const auto deadline =
        std::min(control.compile_deadline,
                 std::chrono::steady_clock::now() +
                     std::chrono::milliseconds(kGrammarCompileMilliseconds));
    status = impl->Receive(deadline, true);
    if (status && control.worker_ready) control.worker_ready();
    uint32_t wire_size = static_cast<uint32_t>(bytes.size());
    if (status)
      status = impl->Transfer(&wire_size, sizeof(wire_size), true, deadline);
    if (status)
      status = impl->Transfer(bytes.data(), bytes.size(), true, deadline);
    if (status) status = impl->Receive(deadline, true);
    if (status && control.compile_started) control.compile_started();
    if (status) status = impl->Receive(deadline);
    if (!status) {
      if (failure) *failure = impl->failure;
      return status;
    }
    out->reset(new StructuredDecoder(std::move(impl)));
    return Status();
  } catch (const std::bad_alloc&) {
    if (failure) *failure = GrammarFailure::kResource;
    return Status::Fail("grammar preparation storage budget exceeded");
  } catch (const std::exception&) {
    if (failure) *failure = GrammarFailure::kWorker;
    return Status::Fail("grammar preparation failed");
  }
#else
  (void)plan;
  (void)decoded_vocab;
  (void)model_vocab;
  (void)stop_ids;
  (void)control;
  return Status::Fail("structured decoding backend is not built");
#endif
}

Status StructuredDecoder::Select(std::span<const uint16_t> logits,
                                 int32_t* token, int32_t forced_token,
                                 TokenSampler* sampler, SampledToken* sample,
                                 std::span<const uint8_t> additional_mask) {
  *token = -1;
  if (sample) *sample = {};
#ifdef Q4T_ENABLE_XGRAMMAR
  if (logits.size() != static_cast<size_t>(impl_->vocab) || impl_->terminated ||
      forced_token < -1 || forced_token >= impl_->vocab ||
      (sampler && !sample) ||
      (!additional_mask.empty() && additional_mask.size() != logits.size()))
    return Status::Fail("invalid constrained decoding state/logit shape");
  try {
    if (sampler) {
      std::vector<uint8_t> allowed(impl_->vocab, 1);
      if (impl_->masked)
        for (int32_t i = 0; i < impl_->vocab; ++i)
          allowed[i] =
              (static_cast<uint32_t>(impl_->mask[i / 32]) >> (i % 32)) & 1;
      if (!additional_mask.empty())
        for (int32_t i = 0; i < impl_->vocab; ++i)
          allowed[i] = allowed[i] && additional_mask[i];
      auto selected = sampler->Select(logits, sample, allowed, forced_token);
      if (!selected) return selected;
      *token = sample->token;
    } else {
      float best = -std::numeric_limits<float>::infinity();
      for (int32_t i = 0; i < impl_->vocab; ++i) {
        if (forced_token >= 0 && i != forced_token) continue;
        if (!additional_mask.empty() && !additional_mask[i]) continue;
        if (impl_->masked && !(static_cast<uint32_t>(impl_->mask[i / 32]) &
                               (uint32_t{1} << (i % 32))))
          continue;
        const uint32_t bits = static_cast<uint32_t>(logits[i]) << 16;
        float value;
        std::memcpy(&value, &bits, sizeof(value));
        if (std::isfinite(value) && (*token < 0 || value > best)) {
          best = value;
          *token = i;
        }
      }
    }
    if (*token < 0) return Status::Fail("no finite grammar-allowed token");
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(kGrammarStepMilliseconds);
    auto status = impl_->Transfer(token, sizeof(*token), true, deadline);
    if (status) status = impl_->Receive(deadline);
    if (!status) {
      *token = -1;
      if (sample) *sample = {};
      return status;
    }
    return Status();
  } catch (const std::exception& error) {
    *token = -1;
    if (sample) *sample = {};
    return Status::Fail(std::string("grammar selection failed: ") +
                        error.what());
  }
#else
  (void)logits;
  (void)forced_token;
  (void)sampler;
  (void)additional_mask;
  return Status::Fail("structured decoding backend is not built");
#endif
}

bool StructuredDecoder::Terminated() const {
#ifdef Q4T_ENABLE_XGRAMMAR
  return impl_->terminated;
#else
  return false;
#endif
}

const std::vector<SchemaReceipt>& StructuredDecoder::Receipts() const {
#ifdef Q4T_ENABLE_XGRAMMAR
  return impl_->receipts;
#else
  static const std::vector<SchemaReceipt> empty;
  return empty;
#endif
}

}  // namespace q4t::server
