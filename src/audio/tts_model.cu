#include "q4t/audio/tts_model.h"

#include <algorithm>
#include <limits>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

__global__ void TextLookup(const float* table, const int32_t* ids,
                           float* output, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count)
    output[i] = table[static_cast<size_t>(ids[i / 2048]) * 2048 + i % 2048];
}

__global__ void AffineOutput(const float* input, const float* bias,
                             float* output, int count, int width, bool silu) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const float x = __fadd_rn(input[i], Read(bias, i % width));
  output[i] = silu ? x / (1.0f + expf(-x)) : x;
}

__global__ void PromptRows(const float* text, const float* codec,
                           const TtsPromptRow* rows, const float* speaker,
                           const float* reference, float* output, int width,
                           int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const auto row = rows[i / width];
  const int d = i % width;
  const auto source = static_cast<size_t>(row.text_index) * width + d;
  if (row.codec_id == TtsPromptRow::kSpeakerCondition) {
    output[i] = __fadd_rn(Read(text, source), Read(speaker, d));
  } else if (row.codec_id <= TtsPromptRow::kReferenceFrame) {
    const size_t frame = TtsPromptRow::kReferenceFrame - row.codec_id;
    output[i] =
        __fadd_rn(Read(text, source), Read(reference, frame * width + d));
  } else {
    output[i] =
        row.codec_id < 0
            ? text[source]
            : __fadd_rn(
                  Read(text, source),
                  Read(codec, static_cast<size_t>(row.codec_id) * width + d));
  }
}

struct ReferenceTables {
  const float* groups[16];
};

__global__ void ReferenceFrames(ReferenceTables tables, const int32_t* ids,
                                float* output, int count, int width) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int frame = i / width, d = i % width;
  float sum = 0;
  for (int group = 0; group < 16; ++group)
    sum += Read(tables.groups[group],
                static_cast<size_t>(ids[frame * 16 + group]) * width + d);
  output[i] = sum;
}

__global__ void SumFrame(const float* codes, const float* text, float* output,
                         int width) {
  const int d = blockIdx.x * blockDim.x + threadIdx.x;
  if (d >= width) return;
  float sum = 0;
  for (int group = 0; group < 16; ++group)
    sum += Read(codes, group * width + d);
  output[d] = __fadd_rn(sum, Read(text, d));
}

Status Affine(const float* input, TtsAffineWeights weights, float* output,
              int m, int n, int k, bool silu, float* temporary, void* gemm,
              cudaStream_t stream) {
  auto status = Matmul(input, weights.weight, temporary, m, n, k, k, CUDA_R_32F,
                       CUDA_R_32F, gemm, TtsDenseLayout::kGemmBytes, stream);
  if (status)
    AffineOutput<<<(m * n + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        temporary, weights.bias, output, m * n, n, silu);
  return status;
}

bool Present(TtsAffineWeights w) { return w.weight && w.bias; }
}  // namespace

TtsComputeResult TtsGenerationRun::Begin(
    const TtsDeviceWeights& weights, const TtsPrompt& prompt, size_t max_frames,
    const TtsSampling& talker_sampling, const TtsSampling& predictor_sampling,
    uint64_t seed, void* workspace, size_t bytes, cudaStream_t stream) {
  if (initialized_ || failed_ || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256 || !weights.text_embedding ||
      !weights.codec_embedding || !weights.codec_head ||
      !Present(weights.text_fc1) || !Present(weights.text_fc2) ||
      (weights.variant != TtsVariant::kCustomVoice06B &&
       !Present(weights.predictor_projection)))
    return Invalid("TTS synthesis initialization invalid");
  for (size_t i = 0; i < 15; ++i)
    if (!weights.code_embeddings[i] || !weights.code_heads[i])
      return Invalid("TTS synthesis code-predictor weights missing");
  if (auto s = ValidateTtsPrompt(prompt); !s) return {s, cudaSuccess};
  if ((weights.variant == TtsVariant::kBase17B) !=
      !prompt.speaker_embedding.empty())
    return Invalid("TTS condition and trained weight variant differ");
  if (auto s = PlanTtsGenerationWorkspace(
          weights.variant, prompt.text_ids.size(), prompt.prefill.size(),
          max_frames, bytes, &layout_, prompt.reference_codes.size());
      !s)
    return {s, cudaSuccess};
  const auto base = reinterpret_cast<uintptr_t>(workspace);
  if (base > std::numeric_limits<uintptr_t>::max() - layout_.bytes)
    return Invalid("TTS synthesis workspace address overflow");
  if (auto s = talker_sampler_.Init(talker_sampling, seed); !s)
    return {s, cudaSuccess};
  if (auto s = predictor_sampler_.Init(predictor_sampling,
                                       seed ^ 0xa0761d6478bd642fULL);
      !s)
    return {s, cudaSuccess};
  if (auto s = state_.Init(max_frames); !s) return {s, cudaSuccess};
  weights_ = &weights;
  workspace_ = static_cast<uint8_t*>(workspace);
  prompt_ = prompt;
  const auto record = [&](TtsComputeResult r) {
    if (!r.ok()) failed_ = true;
    return r;
  };
  auto result =
      talker_.Init(weights.talker, weights.variant, false, layout_.talker.rows,
                   layout_.talker.context, workspace_ + layout_.talker_offset,
                   layout_.talker.bytes, stream);
  if (!result.ok()) return record(result);
  result = predictor_.Init(weights.predictor, weights.variant, true, 2, 16,
                           workspace_ + layout_.predictor_offset,
                           layout_.predictor.bytes, stream);
  if (!result.ok()) return record(result);
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace_ + offset);
  };
  auto copied =
      cudaMemcpyAsync(workspace_ + layout_.ids, prompt_.text_ids.data(),
                      prompt_.text_ids.size() * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream);
  Status status;
  const int rows = static_cast<int>(prompt_.text_ids.size());
  if (copied == cudaSuccess) {
    TextLookup<<<(rows * 2048 + kThreads - 1) / kThreads, kThreads, 0,
                 stream>>>(weights.text_embedding,
                           reinterpret_cast<int32_t*>(workspace_ + layout_.ids),
                           at(layout_.text_lookup), rows * 2048);
    auto* temporary =
        reinterpret_cast<float*>(workspace_ + layout_.affine_fp32);
    auto* gemm = workspace_ + layout_.talker_offset + layout_.talker.gemm;
    status = Affine(at(layout_.text_lookup), weights.text_fc1,
                    at(layout_.text_intermediate), rows, 2048, 2048, true,
                    temporary, gemm, stream);
    if (status)
      status = Affine(at(layout_.text_intermediate), weights.text_fc2,
                      at(layout_.text_projected), rows, layout_.talker.hidden,
                      2048, false, temporary, gemm, stream);
    if (status)
      copied = cudaMemcpyAsync(workspace_ + layout_.prompt_rows,
                               prompt_.prefill.data(),
                               prompt_.prefill.size() * sizeof(TtsPromptRow),
                               cudaMemcpyHostToDevice, stream);
    if (status && copied == cudaSuccess && !prompt_.speaker_embedding.empty())
      copied =
          cudaMemcpyAsync(workspace_ + layout_.speaker_condition,
                          prompt_.speaker_embedding.data(),
                          2048 * sizeof(float), cudaMemcpyHostToDevice, stream);
    if (status && copied == cudaSuccess && !prompt_.reference_codes.empty()) {
      copied = cudaMemcpyAsync(
          workspace_ + layout_.reference_codes, prompt_.reference_codes.data(),
          prompt_.reference_codes.size() * sizeof(TtsCodes),
          cudaMemcpyHostToDevice, stream);
      if (copied == cudaSuccess) {
        ReferenceTables tables{};
        tables.groups[0] = weights.codec_embedding;
        for (size_t i = 1; i < 16; ++i)
          tables.groups[i] = weights.code_embeddings[i - 1];
        const int count = static_cast<int>(prompt_.reference_codes.size()) *
                          layout_.talker.hidden;
        ReferenceFrames<<<(count + kThreads - 1) / kThreads, kThreads, 0,
                          stream>>>(
            tables,
            reinterpret_cast<int32_t*>(workspace_ + layout_.reference_codes),
            at(layout_.reference_embeddings), count, layout_.talker.hidden);
      }
    }
    if (status && copied == cudaSuccess) {
      const int count = layout_.talker.rows * layout_.talker.hidden;
      PromptRows<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          at(layout_.text_projected), weights.codec_embedding,
          reinterpret_cast<TtsPromptRow*>(workspace_ + layout_.prompt_rows),
          prompt_.speaker_embedding.empty() ? nullptr
                                            : at(layout_.speaker_condition),
          prompt_.reference_codes.empty() ? nullptr
                                          : at(layout_.reference_embeddings),
          at(layout_.prefill), layout_.talker.hidden, count);
    }
  }
  result = Complete(status, stream, copied);
  if (!result.ok()) return record(result);
#ifdef Q4T_TTS_TRACE
  TraceTtsOperand("text-projected", 0, 0,
                  {at(layout_.text_projected),
                   prompt_.text_ids.size() * layout_.talker.hidden});
  TraceTtsOperand(
      "prefill", 0, 0,
      {at(layout_.prefill), prompt_.prefill.size() * layout_.talker.hidden});
#endif
  result = talker_.Begin(
      {at(layout_.prefill), prompt_.prefill.size() * layout_.talker.hidden},
      stream);
  if (!result.ok()) return record(result);
  initialized_ = true;
  return result;
}

TtsComputeResult TtsGenerationRun::Head(TtsDenseRun& run, const float* head,
                                        std::span<float> logits,
                                        bool save_hidden, cudaStream_t stream) {
  std::span<const float> hidden;
  auto result = run.Finish(&hidden, stream);
  if (!result.ok()) return result;
  auto* device_logits = reinterpret_cast<float*>(workspace_ + layout_.logits);
  auto status =
      Matmul(hidden.data(), head, device_logits, 1,
             static_cast<int>(logits.size()), static_cast<int>(hidden.size()),
             static_cast<int>(hidden.size()), CUDA_R_32F, CUDA_R_32F,
             workspace_ + layout_.talker_offset + layout_.talker.gemm,
             TtsDenseLayout::kGemmBytes, stream);
  cudaError_t copied = cudaSuccess;
  if (status && save_hidden)
    copied =
        cudaMemcpyAsync(workspace_ + layout_.past_hidden, hidden.data(),
                        hidden.size_bytes(), cudaMemcpyDeviceToDevice, stream);
  if (status && copied == cudaSuccess)
    copied = cudaMemcpyAsync(logits.data(), device_logits, logits.size_bytes(),
                             cudaMemcpyDeviceToHost, stream);
  result = Complete(status, stream, copied);
#ifdef Q4T_TTS_TRACE
  if (result.ok()) {
    TraceTtsOperand(save_hidden ? "talker-hidden" : "predictor-hidden",
                    state_.frames(), state_.next_group(), hidden);
    TraceTtsOperand(save_hidden ? "talker-logits" : "predictor-logits",
                    state_.frames(), state_.next_group(),
                    {device_logits, logits.size()});
  }
#endif
  return result;
}

TtsComputeResult TtsGenerationRun::ProjectPredictor(int rows,
                                                    cudaStream_t stream) {
  const auto* input =
      reinterpret_cast<float*>(workspace_ + layout_.predictor_input);
  auto* output =
      reinterpret_cast<float*>(workspace_ + layout_.predictor_projected);
  const int hidden = layout_.talker.hidden;
  TtsComputeResult result;
  if (hidden == 1024) {
    result = Complete(
        {}, stream,
        cudaMemcpyAsync(output, input,
                        static_cast<size_t>(rows) * 1024 * sizeof(float),
                        cudaMemcpyDeviceToDevice, stream));
  } else {
    result = Complete(
        Affine(input, weights_->predictor_projection, output, rows, 1024,
               hidden, false,
               reinterpret_cast<float*>(workspace_ + layout_.affine_fp32),
               workspace_ + layout_.predictor_offset + layout_.predictor.gemm,
               stream),
        stream);
  }
  if (!result.ok()) return result;
  return predictor_.Begin({output, static_cast<size_t>(rows) * 1024}, stream);
}

TtsComputeResult TtsGenerationRun::BeginPredictor(int32_t code,
                                                  cudaStream_t stream) {
  auto result = predictor_.Reset(stream);
  if (!result.ok()) return result;
  const size_t row_bytes = layout_.talker.hidden * sizeof(float);
  auto copied = cudaMemcpyAsync(workspace_ + layout_.predictor_input,
                                workspace_ + layout_.past_hidden, row_bytes,
                                cudaMemcpyDeviceToDevice, stream);
  if (copied == cudaSuccess)
    copied =
        cudaMemcpyAsync(workspace_ + layout_.predictor_input + row_bytes,
                        weights_->codec_embedding +
                            static_cast<size_t>(code) * layout_.talker.hidden,
                        row_bytes, cudaMemcpyDeviceToDevice, stream);
  result = Complete({}, stream, copied);
  if (!result.ok()) return result;
  return ProjectPredictor(2, stream);
}

TtsComputeResult TtsGenerationRun::ContinuePredictor(size_t group, int32_t code,
                                                     cudaStream_t stream) {
  const auto copied = cudaMemcpyAsync(
      workspace_ + layout_.predictor_input,
      weights_->code_embeddings[group - 1] +
          static_cast<size_t>(code) * layout_.talker.hidden,
      layout_.talker.hidden * sizeof(float), cudaMemcpyDeviceToDevice, stream);
  auto result = Complete({}, stream, copied);
  if (!result.ok()) return result;
  return ProjectPredictor(1, stream);
}

TtsComputeResult TtsGenerationRun::ContinueTalker(cudaStream_t stream) {
  const int hidden = layout_.talker.hidden;
  const auto& codes = state_.completed().back();
  cudaError_t copied = cudaSuccess;
  for (int i = 0; i < 16 && copied == cudaSuccess; ++i) {
    const auto* table =
        i == 0 ? weights_->codec_embedding : weights_->code_embeddings[i - 1];
    copied = cudaMemcpyAsync(
        workspace_ + layout_.codec_embeddings + i * hidden * sizeof(float),
        table + static_cast<size_t>(codes[i]) * hidden, hidden * sizeof(float),
        cudaMemcpyDeviceToDevice, stream);
  }
  const size_t frame = state_.frames() - 1;
  const auto index = frame < prompt_.trailing_text.size()
                         ? prompt_.trailing_text[frame]
                         : prompt_.pad_index;
  const auto* text =
      reinterpret_cast<float*>(workspace_ + layout_.text_projected) +
      static_cast<size_t>(index) * hidden;
  auto* output = reinterpret_cast<float*>(workspace_ + layout_.decode_input);
  if (copied == cudaSuccess)
    SumFrame<<<(hidden + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        reinterpret_cast<float*>(workspace_ + layout_.codec_embeddings), text,
        output, hidden);
  auto result = Complete({}, stream, copied);
  if (!result.ok()) return result;
  return talker_.Begin({output, static_cast<size_t>(hidden)}, stream);
}

TtsComputeResult TtsGenerationRun::Advance(cudaStream_t stream) {
  if (!initialized_ || failed_ || done())
    return Invalid("TTS synthesis advance out of order");
  const auto record = [&](TtsComputeResult result) {
    if (!result.ok()) failed_ = true;
    return result;
  };
  if (!predicting_) {
    if (talker_.completed_layers() < 28)
      return record(talker_.NextLayer(stream));
    std::array<float, 3072> logits;
    auto result = Head(talker_, weights_->codec_head, logits, true, stream);
    if (!result.ok()) return record(result);
    int32_t token;
    auto status = talker_sampler_.Select(logits, true, state_.allow_eos(),
                                         state_.first_code_history(), &token);
    if (status) status = state_.Accept(0, token);
    if (!status) return record({status, cudaSuccess});
    if (state_.ended()) return {};
    result = BeginPredictor(token, stream);
    if (result.ok()) predicting_ = true;
    return record(result);
  }
  if (predictor_.completed_layers() < 5)
    return record(predictor_.NextLayer(stream));
  const auto group = state_.next_group();
  std::array<float, 2048> logits;
  auto result =
      Head(predictor_, weights_->code_heads[group - 1], logits, false, stream);
  if (!result.ok()) return record(result);
  int32_t token;
  auto status = predictor_sampler_.Select(logits, false, false, {}, &token);
  if (status) status = state_.Accept(group, token);
  if (!status) return record({status, cudaSuccess});
  if (state_.next_group() != 0)
    return record(ContinuePredictor(group, token, stream));
  predicting_ = false;
  if (state_.at_limit()) return {};
  return record(ContinueTalker(stream));
}

}  // namespace q4t::audio
