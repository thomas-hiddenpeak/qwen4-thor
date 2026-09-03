// Model configuration: parse config.json into a typed struct.
//
// The model directory's config.json has a top-level object with `text_config`
// (the LLM hyperparameters), `vision_config`, `quantization_config`, and a few
// scalar token ids. This module parses the fields the qwen4_exp forward pass
// needs into a `ModelConfig` struct, with sanity checks. The raw JSON is not
// retained; if a field is needed later, add it here.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "q4t/status.h"

namespace q4t {
namespace io {

struct RopeParams {
  std::vector<int64_t> mrope_section;  // e.g. [11, 11, 10]
  double partial_rotary_factor = 0.25;
  double rope_theta = 10000000.0;
  bool mrope_interleaved = true;
  std::string rope_type = "default";
};

struct MtpConfig {
  int num_hidden_layers = 1;
  std::vector<std::string> layer_types;
  double rope_theta = 10000000.0;
  bool hybrid = true;
};

struct QuantConfig {
  std::string quant_algo;  // e.g. "NVFP4"
  std::vector<std::string> ignore;  // dotted-name globs kept in BF16
};

struct ModelConfig {
  std::string model_type;
  int num_hidden_layers = 0;
  std::vector<std::string> layer_types;  // size == num_hidden_layers
  int hidden_size = 0;
  int vocab_size = 0;
  int max_position_embeddings = 0;

  // Full attention.
  int num_attention_heads = 0;
  int num_key_value_heads = 0;
  int head_dim = 0;
  int full_attention_interval = 4;

  // MoE.
  int num_experts = 0;
  int num_experts_per_tok = 0;
  int moe_intermediate_size = 0;
  int shared_expert_intermediate_size = 0;
  double router_aux_loss_coef = 0.001;

  // Linear (DeltaNet) attention.
  int linear_num_key_heads = 0;
  int linear_num_value_heads = 0;
  int linear_key_head_dim = 0;
  int linear_value_head_dim = 0;
  int linear_conv_kernel_dim = 0;

  // QSA sparse-attention indexer.
  int indexer_budget = 0;
  int indexer_compress_ratio = 0;
  int indexer_head_dim = 0;
  int indexer_kv_heads = 0;
  int indexer_n_heads = 0;

  // PLE.
  int ngram_size = 3;
  int heads_per_ngram = 8;
  int ple_embed_dim = 0;
  int ple_conv_kernel_size = 4;
  std::vector<int64_t> ple_layer_ids;  // 0-indexed layer ids
  int64_t ngram_vocab_size_base = 0;
  int make_ngram_vocab_size_divisible_by = 128;
  int split_ngram_parts = 128;

  // Hyper-connection.
  int hc_count = 4;
  int hc_lowrank = 0;

  RopeParams rope;
  MtpConfig mtp;
  QuantConfig quant;

  // Tokens.
  int64_t bos_token_id = 0;
  int64_t eos_token_id = 0;
  int64_t pad_token_id = -1;  // -1 = unset
  int image_token_id = 0;
  int video_token_id = 0;
  int vision_start_token_id = 0;
  int vision_end_token_id = 0;

  // Misc.
  double rms_norm_eps = 1e-6;
  std::string hidden_act = "silu";
  std::string output_gate_type = "sigmoid";
  bool tie_word_embeddings = false;

  // Count of full_attention layers (derived; == num_hidden_layers / interval).
  int num_full_attention_layers() const;
  // True if `layer_id` (0-indexed) is a full_attention layer.
  bool IsFullAttention(int layer_id) const;
};

// Parse config.json at `path`. Validates model_type and key invariants.
Status ParseModelConfig(const std::string& path, ModelConfig* out);

}  // namespace io
}  // namespace q4t
