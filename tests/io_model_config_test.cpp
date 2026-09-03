// Tests for the model config parser, against the real model's config.json.
#include "q4t/io/model_config.h"
#include "q4t/test.h"

#include <cstdio>
#include <fcntl.h>
#include <unistd.h>

namespace {

using q4t::io::ModelConfig;
using q4t::io::ParseModelConfig;

const char* kRealConfig =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "config.json";

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}

}  // namespace

Q4T_TEST(model_config_parse_real) {
  if (!FileExists(kRealConfig)) {
    std::printf("  (skipped: real config.json not present)\n");
    return true;
  }
  ModelConfig cfg;
  auto s = ParseModelConfig(kRealConfig, &cfg);
  Q4T_CHECK(s.ok());

  // Core dims.
  Q4T_CHECK(cfg.model_type == "qwen4_exp");
  Q4T_CHECK(cfg.num_hidden_layers == 48);
  Q4T_CHECK(cfg.hidden_size == 2560);
  Q4T_CHECK(cfg.vocab_size == 248320);
  Q4T_CHECK(cfg.max_position_embeddings == 262144);
  Q4T_CHECK(cfg.layer_types.size() == 48);

  // Attention.
  Q4T_CHECK(cfg.num_attention_heads == 24);
  Q4T_CHECK(cfg.num_key_value_heads == 2);
  Q4T_CHECK(cfg.head_dim == 256);
  Q4T_CHECK(cfg.full_attention_interval == 4);

  // MoE.
  Q4T_CHECK(cfg.num_experts == 512);
  Q4T_CHECK(cfg.num_experts_per_tok == 10);
  Q4T_CHECK(cfg.moe_intermediate_size == 640);
  Q4T_CHECK(cfg.shared_expert_intermediate_size == 640);

  // Linear attention.
  Q4T_CHECK(cfg.linear_num_key_heads == 16);
  Q4T_CHECK(cfg.linear_num_value_heads == 48);
  Q4T_CHECK(cfg.linear_key_head_dim == 128);
  Q4T_CHECK(cfg.linear_value_head_dim == 128);
  Q4T_CHECK(cfg.linear_conv_kernel_dim == 4);

  // QSA indexer.
  Q4T_CHECK(cfg.indexer_budget == 2048);
  Q4T_CHECK(cfg.indexer_compress_ratio == 4);
  Q4T_CHECK(cfg.indexer_head_dim == 128);
  Q4T_CHECK(cfg.indexer_kv_heads == 1);
  Q4T_CHECK(cfg.indexer_n_heads == 4);

  // PLE.
  Q4T_CHECK(cfg.ngram_size == 3);
  Q4T_CHECK(cfg.heads_per_ngram == 8);
  Q4T_CHECK(cfg.ple_embed_dim == 2560);
  Q4T_CHECK(cfg.ple_conv_kernel_size == 4);
  Q4T_CHECK(cfg.ple_layer_ids.size() == 1 && cfg.ple_layer_ids[0] == 2);
  Q4T_CHECK(cfg.ngram_vocab_size_base == 20000000);

  // Hyper-connection.
  Q4T_CHECK(cfg.hc_count == 4);
  Q4T_CHECK(cfg.hc_lowrank == 320);

  // Rope.
  Q4T_CHECK(cfg.rope.mrope_section.size() == 3);
  Q4T_CHECK(cfg.rope.mrope_section[0] == 11);
  Q4T_CHECK(cfg.rope.mrope_section[1] == 11);
  Q4T_CHECK(cfg.rope.mrope_section[2] == 10);
  Q4T_CHECK(cfg.rope.partial_rotary_factor == 0.25);
  Q4T_CHECK(cfg.rope.rope_theta == 10000000.0);
  Q4T_CHECK(cfg.rope.mrope_interleaved == true);

  // MTP.
  Q4T_CHECK(cfg.mtp.num_hidden_layers == 1);
  Q4T_CHECK(cfg.mtp.layer_types.size() == 1 &&
            cfg.mtp.layer_types[0] == "full_attention");

  // Quant.
  Q4T_CHECK(cfg.quant.quant_algo == "NVFP4");
  Q4T_CHECK(!cfg.quant.ignore.empty());

  // Tokens.
  Q4T_CHECK(cfg.bos_token_id == 248044);
  Q4T_CHECK(cfg.eos_token_id == 248044);
  Q4T_CHECK(cfg.pad_token_id == -1);  // null in config
  Q4T_CHECK(cfg.image_token_id == 248056);
  Q4T_CHECK(cfg.video_token_id == 248057);

  // Derived: every 4th layer (1-indexed) is full_attention -> 12 layers.
  Q4T_CHECK(cfg.num_full_attention_layers() == 12);
  Q4T_CHECK(cfg.IsFullAttention(3));
  Q4T_CHECK(!cfg.IsFullAttention(2));
  Q4T_CHECK(cfg.IsFullAttention(47));
  Q4T_CHECK(!cfg.IsFullAttention(46));
  return true;
}

Q4T_TEST(model_config_missing_file) {
  ModelConfig cfg;
  auto s = ParseModelConfig("/tmp/q4t_no_such_config.json", &cfg);
  Q4T_CHECK(!s.ok());
  return true;
}
