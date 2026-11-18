// Exact caller-owned storage for native TTS. No CUDA or file I/O here.
#pragma once

#include <array>

#include "q4t/audio/tts_config.h"

namespace q4t::audio {

struct TtsTensorExtent {
  std::string name;
  size_t file_offset = 0;  // Relative to the safetensors data region.
  size_t offset = 0;       // Compact device payload, in contract name order.
  size_t bytes = 0;
};

struct TtsWeightLayout {
  std::vector<TtsTensorExtent> tensors;
  size_t file_bytes = 0;
  size_t bytes = 0;
};

// Entire file identity must be checked before reading the returned extents.
// The codec packs only the 271 active decoder tensors, not its encoder.
Status ParseTtsWeightLayout(const io::Json& header, size_t data_bytes,
                            TtsVariant variant, TtsWeightLayout* out);
Status ParseTtsCodecWeightLayout(const io::Json& header, size_t data_bytes,
                                 TtsWeightLayout* out);
Status ValidateTtsWeightLayout(const TtsWeightLayout& layout,
                               TtsVariant variant, bool codec = false);

struct TtsAffineWeights {
  const uint16_t* weight = nullptr;
  const uint16_t* bias = nullptr;
};

struct TtsLayerWeights {
  const uint16_t* input_norm = nullptr;
  const uint16_t* post_norm = nullptr;
  const uint16_t* q_norm = nullptr;
  const uint16_t* k_norm = nullptr;
  const uint16_t* q = nullptr;
  const uint16_t* k = nullptr;
  const uint16_t* v = nullptr;
  const uint16_t* o = nullptr;
  const uint16_t* gate = nullptr;
  const uint16_t* up = nullptr;
  const uint16_t* down = nullptr;
};

struct TtsDenseWeights {
  const uint16_t* norm = nullptr;
  // Predictor uses only its first five slots; the talker uses all 28.
  std::array<TtsLayerWeights, 28> layers;
};

struct TtsDeviceWeights {
  TtsVariant variant = TtsVariant::kCustomVoice17B;
  const uint16_t* text_embedding = nullptr;
  const uint16_t* codec_embedding = nullptr;
  const uint16_t* codec_head = nullptr;
  TtsAffineWeights text_fc1, text_fc2, predictor_projection;
  TtsDenseWeights talker, predictor;
  std::array<const uint16_t*, 15> code_embeddings{}, code_heads{};
};

Status BindTtsDeviceWeights(const void* data, size_t bytes,
                            const TtsWeightLayout& layout, TtsVariant variant,
                            TtsDeviceWeights* out);

struct TtsDenseLayout {
  int hidden = 0, intermediate = 0, layers = 0;
  int rows = 0, padded_rows = 0, context = 0, padded_context = 0;
  size_t bytes = 0;
  size_t hidden_state = 0, normalized = 0, projected = 0;
  size_t q = 0, k = 0, v = 0, head_q = 0, rope = 0;
  size_t scores = 0, head_output = 0, attention = 0, gate = 0, up = 0;
  size_t cache_k = 0, cache_v = 0, gemm = 0;
  static constexpr size_t kGemmBytes = 32ULL << 20;
};

// Persistent K [layer,head,context,128], V [layer,head,128,context].
// One score matrix is reused over heads. Inputs larger than a request's
// chosen prefill/context or physical RAM budget fail before allocation.
Status PlanTtsDenseWorkspace(TtsVariant variant, bool predictor,
                             size_t rows, size_t context, size_t max_bytes,
                             TtsDenseLayout* out);

struct TtsGenerationLayout {
  TtsDenseLayout talker, predictor;
  size_t bytes = 0, talker_offset = 0, predictor_offset = 0;
  size_t ids = 0, text_lookup = 0, text_intermediate = 0, text_projected = 0;
  size_t affine_fp32 = 0, prefill = 0, past_hidden = 0;
  size_t prompt_rows = 0;
  size_t codec_embeddings = 0, decode_input = 0, predictor_input = 0;
  size_t predictor_projected = 0, logits = 0;
};

Status PlanTtsGenerationWorkspace(TtsVariant variant, size_t text_tokens,
                                  size_t prefill_tokens, size_t max_frames,
                                  size_t max_bytes,
                                  TtsGenerationLayout* out);

}  // namespace q4t::audio
