#include "q4t/server/structured_decoder.h"
#include "q4t/server/structured_admission.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <exception>
#include <limits>
#include <utility>

#ifdef Q4T_ENABLE_XGRAMMAR
#include <xgrammar/xgrammar.h>
#endif

namespace q4t::server {
#ifdef Q4T_ENABLE_XGRAMMAR
struct StructuredDecoder::Impl {
  Impl(const xgrammar::CompiledGrammar& grammar, int size)
      : matcher(grammar), vocab(size), mask((size + 31) / 32, -1) {}
  xgrammar::GrammarMatcher matcher;
  int vocab;
  std::vector<int32_t> mask;
};
#else
struct StructuredDecoder::Impl {};
#endif

StructuredDecoder::StructuredDecoder(std::unique_ptr<Impl> impl)
    : impl_(std::move(impl)) {}
StructuredDecoder::~StructuredDecoder() = default;

Status StructuredDecoder::Create(const OutputConstraints& plan,
                                 const std::vector<std::string>& decoded_vocab,
                                 int model_vocab,
                                 const std::vector<int32_t>& stop_ids,
                                 std::unique_ptr<StructuredDecoder>* out) {
  out->reset();
#ifdef Q4T_ENABLE_XGRAMMAR
  if (!plan.active || plan.structural_tag.empty() || model_vocab <= 0 ||
      model_vocab > 300000 || decoded_vocab.empty() ||
      decoded_vocab.size() > static_cast<size_t>(model_vocab) ||
      stop_ids.empty())
    return Status::Fail("invalid grammar/vocabulary configuration");
  for (const int32_t id : stop_ids)
    if (id < 0 || static_cast<size_t>(id) >= decoded_vocab.size())
      return Status::Fail("grammar stop token is outside decoded vocabulary");
  try {
    const xgrammar::TokenizerInfo tokenizer(
        decoded_vocab, xgrammar::VocabType::RAW, model_vocab, stop_ids, false);
    // No unbounded process-wide grammar cache, no compiler thread fanout.
    // max_memory_bytes is a cache bound, NOT a peak compilation RSS limit.
    xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
    const auto grammar = compiler.CompileStructuralTag(plan.structural_tag);
    if (grammar.MemorySizeBytes() > kCompiledGrammarLimit)
      return Status::Fail("compiled grammar exceeds 64 MiB");
    out->reset(
        new StructuredDecoder(std::make_unique<Impl>(grammar, model_vocab)));
    return Status();
  } catch (const std::exception& error) {
    return Status::Fail(std::string("grammar compilation failed: ") +
                        error.what());
  }
#else
  (void)plan;
  (void)decoded_vocab;
  (void)model_vocab;
  (void)stop_ids;
  return Status::Fail("structured decoding backend is not built");
#endif
}

Status StructuredDecoder::Select(std::span<const uint16_t> logits,
                                 int32_t* token) {
  *token = -1;
#ifdef Q4T_ENABLE_XGRAMMAR
  if (logits.size() != static_cast<size_t>(impl_->vocab) ||
      impl_->matcher.IsTerminated())
    return Status::Fail("invalid constrained decoding state/logit shape");
  try {
    std::fill(impl_->mask.begin(), impl_->mask.end(), -1);
    int64_t shape = static_cast<int64_t>(impl_->mask.size());
    DLTensor tensor{};
    tensor.data = impl_->mask.data();
    tensor.device = {kDLCPU, 0};
    tensor.ndim = 1;
    tensor.dtype = {kDLInt, 32, 1};
    tensor.shape = &shape;
    const bool masked = impl_->matcher.FillNextTokenBitmask(&tensor);
    float best = -std::numeric_limits<float>::infinity();
    for (int32_t i = 0; i < impl_->vocab; ++i) {
      if (masked && !(static_cast<uint32_t>(impl_->mask[i / 32]) &
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
    if (*token < 0) return Status::Fail("no finite grammar-allowed token");
    if (!impl_->matcher.AcceptToken(*token)) {
      *token = -1;
      return Status::Fail("grammar rejected its selected token");
    }
    return Status();
  } catch (const std::exception& error) {
    *token = -1;
    return Status::Fail(std::string("grammar selection failed: ") +
                        error.what());
  }
#else
  (void)logits;
  return Status::Fail("structured decoding backend is not built");
#endif
}

bool StructuredDecoder::Terminated() const {
#ifdef Q4T_ENABLE_XGRAMMAR
  return impl_->matcher.IsTerminated();
#else
  return false;
#endif
}

}  // namespace q4t::server
