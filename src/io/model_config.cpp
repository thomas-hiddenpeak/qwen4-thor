// Model configuration parser implementation.
#include "q4t/io/model_config.h"

#include <fstream>
#include <sstream>

#include "q4t/io/json.h"

namespace q4t {
namespace io {

int ModelConfig::num_full_attention_layers() const {
  int n = 0;
  for (const auto& t : layer_types) {
    if (t == "full_attention") ++n;
  }
  return n;
}

bool ModelConfig::IsFullAttention(int layer_id) const {
  if (layer_id < 0 || layer_id >= static_cast<int>(layer_types.size())) {
    return false;
  }
  return layer_types[layer_id] == "full_attention";
}

namespace {

Status ReadFile(const std::string& path, std::string* out) {
  std::ifstream in(path, std::ios::binary);
  if (!in) {
    return Status::Fail("cannot open " + path);
  }
  std::ostringstream ss;
  ss << in.rdbuf();
  *out = ss.str();
  return Status();
}

}  // namespace

Status ParseModelConfig(const std::string& path, ModelConfig* out) {
  std::string text;
  Status s = ReadFile(path, &text);
  if (!s.ok()) return s;

  Json root;
  s = ParseJson(text, &root);
  if (!s.ok()) return s;
  if (!root.IsObject()) return Status::Fail("config.json is not an object");

  const Json* text_cfg = root.Find("text_config");
  if (!text_cfg || !text_cfg->IsObject()) {
    return Status::Fail("config.json missing text_config");
  }

  ModelConfig cfg;
  cfg.model_type = root.GetString("model_type");
  if (cfg.model_type != "qwen4_exp") {
    return Status::Fail("unsupported model_type '" + cfg.model_type +
                        "' (expected qwen4_exp)");
  }

  cfg.num_hidden_layers = static_cast<int>(text_cfg->GetInt("num_hidden_layers"));
  cfg.hidden_size = static_cast<int>(text_cfg->GetInt("hidden_size"));
  cfg.vocab_size = static_cast<int>(text_cfg->GetInt("vocab_size"));
  cfg.max_position_embeddings = static_cast<int>(
      text_cfg->GetInt("max_position_embeddings"));

  // layer_types.
  const Json* lt = text_cfg->GetArray("layer_types");
  if (lt) {
    for (const Json& v : lt->array) {
      if (v.IsString()) cfg.layer_types.push_back(v.str);
    }
  }
  if (static_cast<int>(cfg.layer_types.size()) != cfg.num_hidden_layers) {
    return Status::Fail("layer_types size (" +
                        std::to_string(cfg.layer_types.size()) +
                        ") != num_hidden_layers (" +
                        std::to_string(cfg.num_hidden_layers) + ")");
  }

  // Full attention.
  cfg.num_attention_heads =
      static_cast<int>(text_cfg->GetInt("num_attention_heads"));
  cfg.num_key_value_heads =
      static_cast<int>(text_cfg->GetInt("num_key_value_heads"));
  cfg.head_dim = static_cast<int>(text_cfg->GetInt("head_dim"));
  cfg.full_attention_interval =
      static_cast<int>(text_cfg->GetInt("full_attention_interval", 4));

  // MoE.
  cfg.num_experts = static_cast<int>(text_cfg->GetInt("num_experts"));
  cfg.num_experts_per_tok =
      static_cast<int>(text_cfg->GetInt("num_experts_per_tok"));
  cfg.moe_intermediate_size =
      static_cast<int>(text_cfg->GetInt("moe_intermediate_size"));
  cfg.shared_expert_intermediate_size = static_cast<int>(
      text_cfg->GetInt("shared_expert_intermediate_size"));
  cfg.router_aux_loss_coef = text_cfg->GetNumber("router_aux_loss_coef");

  // Linear (DeltaNet) attention.
  cfg.linear_num_key_heads =
      static_cast<int>(text_cfg->GetInt("linear_num_key_heads"));
  cfg.linear_num_value_heads =
      static_cast<int>(text_cfg->GetInt("linear_num_value_heads"));
  cfg.linear_key_head_dim =
      static_cast<int>(text_cfg->GetInt("linear_key_head_dim"));
  cfg.linear_value_head_dim =
      static_cast<int>(text_cfg->GetInt("linear_value_head_dim"));
  cfg.linear_conv_kernel_dim =
      static_cast<int>(text_cfg->GetInt("linear_conv_kernel_dim"));

  // QSA indexer.
  cfg.indexer_budget = static_cast<int>(text_cfg->GetInt("indexer_budget"));
  cfg.indexer_compress_ratio =
      static_cast<int>(text_cfg->GetInt("indexer_compress_ratio"));
  cfg.indexer_head_dim = static_cast<int>(text_cfg->GetInt("indexer_head_dim"));
  cfg.indexer_kv_heads =
      static_cast<int>(text_cfg->GetInt("indexer_kv_heads"));
  cfg.indexer_n_heads = static_cast<int>(text_cfg->GetInt("indexer_n_heads"));

  // PLE.
  cfg.ngram_size = static_cast<int>(text_cfg->GetInt("ngram_size", 3));
  cfg.heads_per_ngram =
      static_cast<int>(text_cfg->GetInt("heads_per_ngram", 8));
  cfg.ple_embed_dim = static_cast<int>(text_cfg->GetInt("ple_embed_dim"));
  cfg.ple_conv_kernel_size =
      static_cast<int>(text_cfg->GetInt("ple_conv_kernel_size", 4));
  const Json* ple_ids = text_cfg->GetArray("ple_layer_ids");
  if (ple_ids) {
    for (const Json& v : ple_ids->array) {
      cfg.ple_layer_ids.push_back(v.AsInt());
    }
  }
  cfg.ngram_vocab_size_base =
      text_cfg->GetInt("ngram_vocab_size_base");
  cfg.make_ngram_vocab_size_divisible_by = static_cast<int>(
      text_cfg->GetInt("make_ngram_vocab_size_divisible_by", 128));
  cfg.split_ngram_parts =
      static_cast<int>(text_cfg->GetInt("split_ngram_parts", 128));

  // Hyper-connection.
  cfg.hc_count = static_cast<int>(text_cfg->GetInt("hc_count", 4));
  cfg.hc_lowrank = static_cast<int>(text_cfg->GetInt("hc_lowrank"));

  // Rope.
  const Json* rope = text_cfg->Find("rope_parameters");
  if (rope && rope->IsObject()) {
    cfg.rope.rope_theta = rope->GetNumber("rope_theta", 10000000.0);
    cfg.rope.partial_rotary_factor =
        rope->GetNumber("partial_rotary_factor", 0.25);
    cfg.rope.mrope_interleaved = rope->GetBool("mrope_interleaved", true);
    cfg.rope.rope_type = rope->GetString("rope_type", "default");
    const Json* section = rope->GetArray("mrope_section");
    if (section) {
      for (const Json& v : section->array) {
        cfg.rope.mrope_section.push_back(v.AsInt());
      }
    }
  }

  // MTP.
  const Json* mtp = text_cfg->Find("mtp");
  if (mtp && mtp->IsObject()) {
    cfg.mtp.num_hidden_layers =
        static_cast<int>(mtp->GetInt("num_hidden_layers", 1));
    cfg.mtp.rope_theta = mtp->GetNumber("rope_theta", 10000000.0);
    cfg.mtp.hybrid = mtp->GetBool("hybrid", true);
    const Json* mtp_lt = mtp->GetArray("layer_types");
    if (mtp_lt) {
      for (const Json& v : mtp_lt->array) {
        if (v.IsString()) cfg.mtp.layer_types.push_back(v.str);
      }
    }
  }

  // Quantization.
  const Json* quant = root.Find("quantization_config");
  if (quant && quant->IsObject()) {
    cfg.quant.quant_algo = quant->GetString("quant_algo");
    const Json* ignore = quant->GetArray("ignore");
    if (ignore) {
      for (const Json& v : ignore->array) {
        if (v.IsString()) cfg.quant.ignore.push_back(v.str);
      }
    }
  }

  // Tokens.
  cfg.bos_token_id = text_cfg->GetInt("bos_token_id");
  cfg.eos_token_id = text_cfg->GetInt("eos_token_id");
  const Json* pad = text_cfg->Find("pad_token_id");
  cfg.pad_token_id = (pad && pad->IsNumber()) ? pad->AsInt() : -1;
  cfg.image_token_id = static_cast<int>(root.GetInt("image_token_id"));
  cfg.video_token_id = static_cast<int>(root.GetInt("video_token_id"));
  cfg.vision_start_token_id =
      static_cast<int>(root.GetInt("vision_start_token_id"));
  cfg.vision_end_token_id =
      static_cast<int>(root.GetInt("vision_end_token_id"));

  // Misc.
  cfg.rms_norm_eps = text_cfg->GetNumber("rms_norm_eps", 1e-6);
  cfg.hidden_act = text_cfg->GetString("hidden_act", "silu");
  cfg.output_gate_type = text_cfg->GetString("output_gate_type", "sigmoid");
  cfg.tie_word_embeddings = text_cfg->GetBool("tie_word_embeddings", false);

  // Invariants.
  if (cfg.hidden_size <= 0 || cfg.vocab_size <= 0) {
    return Status::Fail("invalid hidden_size/vocab_size");
  }
  if (cfg.num_attention_heads <= 0 || cfg.num_key_value_heads <= 0) {
    return Status::Fail("invalid attention head counts");
  }
  if (cfg.num_full_attention_layers() == 0) {
    return Status::Fail("no full_attention layers in layer_types");
  }
  // ple_embed_dim must be a multiple of ngram_heads (row_bytes is a file
  // property, checked when the sidecar is opened).
  const int ngram_heads = (cfg.ngram_size - 1) * cfg.heads_per_ngram;
  if (ngram_heads > 0 && cfg.ple_embed_dim % ngram_heads != 0) {
    return Status::Fail("ple_embed_dim not a multiple of ngram_heads");
  }

  *out = cfg;
  return Status();
}

}  // namespace io
}  // namespace q4t
