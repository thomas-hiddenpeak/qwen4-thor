#include "q4t/model/embedding_config.h"

#include <initializer_list>
#include <utility>

namespace q4t::model {

Status ValidateEmbeddingConfig(const io::Json& config) {
  using C = EmbeddingConfig;
  const auto fail = [](const std::string& key) {
    return Status::Fail("Qwen3 Embedding configuration differs: " + key);
  };
  if (!config.IsObject()) return fail("object");
  for (const auto& [name, value] :
       std::initializer_list<std::pair<const char*, int>>{
           {"hidden_size", C::kHidden}, {"intermediate_size", C::kIntermediate},
           {"num_hidden_layers", C::kLayers},
           {"num_attention_heads", C::kQueryHeads},
           {"num_key_value_heads", C::kKvHeads}, {"head_dim", C::kHeadDim},
           {"vocab_size", C::kVocab}, {"bos_token_id", C::kEos},
           {"eos_token_id", C::kEos}, {"max_position_embeddings", C::kContext},
           {"max_window_layers", C::kLayers}}) {
    const auto* found = config.Find(name);
    if (!found || found->AsInt(-1) != value) return fail(name);
  }
  for (const auto& [name, value] :
       std::initializer_list<std::pair<const char*, const char*>>{
           {"model_type", "qwen3"}, {"hidden_act", "silu"},
           {"torch_dtype", "bfloat16"}})
    if (config.GetString(name) != value) return fail(name);
  const auto* architectures = config.GetArray("architectures");
  if (!architectures || architectures->array.size() != 1 ||
      !architectures->array[0].IsString() ||
      architectures->array[0].str != "Qwen3ForCausalLM")
    return fail("architectures");
  for (const auto& [name, expected] :
       std::initializer_list<std::pair<const char*, bool>>{
           {"attention_bias", false}, {"use_sliding_window", false},
           {"tie_word_embeddings", true}, {"use_cache", true}}) {
    const auto* value = config.Find(name);
    if (!value || !value->IsBool() || value->boolean != expected)
      return fail(name);
  }
  for (const char* name : {"rope_scaling", "sliding_window"}) {
    const auto* value = config.Find(name);
    if (!value || !value->IsNull()) return fail(name);
  }
  if (config.GetNumber("rms_norm_eps", -1) != 1e-6)
    return fail("rms_norm_eps");
  if (config.GetNumber("rope_theta", -1) != C::kRopeTheta)
    return fail("rope_theta");
  if (config.GetNumber("attention_dropout", -1) != 0)
    return fail("attention_dropout");
  return {};
}

std::vector<EmbeddingTensorContract> EmbeddingTensorContracts() {
  using C = EmbeddingConfig;
  std::vector<EmbeddingTensorContract> result;
  const auto add = [&](std::string name, std::vector<int64_t> shape) {
    size_t bytes = sizeof(uint16_t);
    for (const auto extent : shape) bytes *= static_cast<size_t>(extent);
    result.push_back({std::move(name), std::move(shape), bytes});
  };
  // Official AutoModel safetensors stores bare model names, no "model.".
  add("embed_tokens.weight", {C::kVocab, C::kHidden});
  add("norm.weight", {C::kHidden});
  for (int i = 0; i < C::kLayers; ++i) {
    const auto prefix = "layers." + std::to_string(i) + ".";
    for (const char* norm : {"input_layernorm.weight",
                             "post_attention_layernorm.weight"})
      add(prefix + norm, {C::kHidden});
    for (const char* norm : {"q_norm.weight", "k_norm.weight"})
      add(prefix + "self_attn." + norm, {C::kHeadDim});
    add(prefix + "self_attn.q_proj.weight",
        {C::kQueryHeads * C::kHeadDim, C::kHidden});
    for (const char* kind : {"k_proj.weight", "v_proj.weight"})
      add(prefix + "self_attn." + kind,
          {C::kKvHeads * C::kHeadDim, C::kHidden});
    add(prefix + "self_attn.o_proj.weight",
        {C::kHidden, C::kQueryHeads * C::kHeadDim});
    for (const char* kind : {"gate_proj.weight", "up_proj.weight"})
      add(prefix + "mlp." + kind, {C::kIntermediate, C::kHidden});
    add(prefix + "mlp.down_proj.weight", {C::kHidden, C::kIntermediate});
  }
  return result;
}

size_t EmbeddingWeightBytes() {
  size_t bytes = 0;
  for (const auto& tensor : EmbeddingTensorContracts()) bytes += tensor.bytes;
  return bytes;
}

}  // namespace q4t::model
