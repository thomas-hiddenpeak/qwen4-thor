// Reuse the frozen text/sequence lifecycle and layer-2 control records.
// Additional observers link only into this independent executable.
#define main Q4tRealCaptureOriginalMain
#include "../moe_replay_real/capture.cpp"
#undef main
#include <cublasLt.h>
#include <set>

namespace q4t::diagnostic::upstream {
using namespace real_capture;
void BlasCheck(cublasStatus_t status, const char* operation) {
  Check(status == CUBLAS_STATUS_SUCCESS, operation);
}
template <typename T>
T Layout(cublasLtMatrixLayout_t layout,
         cublasLtMatrixLayoutAttribute_t attribute) {
  T value{};
  size_t written = 0;
  BlasCheck(cublasLtMatrixLayoutGetAttribute(layout, attribute, &value,
                                             sizeof(value), &written),
            "read cuBLAS layout");
  Check(written == sizeof(value), "cuBLAS layout attribute width");
  return value;
}
template <typename T>
T Descriptor(cublasLtMatmulDesc_t descriptor,
             cublasLtMatmulDescAttributes_t attribute) {
  T value{};
  size_t written = 0;
  BlasCheck(cublasLtMatmulDescGetAttribute(descriptor, attribute, &value,
                                           sizeof(value), &written),
            "read cuBLAS operation");
  Check(written == sizeof(value), "cuBLAS descriptor attribute width");
  return value;
}
void CheckLayout(cublasLtMatrixLayout_t layout, uint64_t rows, uint64_t columns,
                 int64_t leading) {
  Check(Layout<uint64_t>(layout, CUBLASLT_MATRIX_LAYOUT_ROWS) == rows &&
            Layout<uint64_t>(layout, CUBLASLT_MATRIX_LAYOUT_COLS) == columns &&
            Layout<int64_t>(layout, CUBLASLT_MATRIX_LAYOUT_LD) == leading &&
            Layout<cudaDataType_t>(layout, CUBLASLT_MATRIX_LAYOUT_TYPE) ==
                CUDA_R_16BF &&
            Layout<cublasLtOrder_t>(layout, CUBLASLT_MATRIX_LAYOUT_ORDER) ==
                CUBLASLT_ORDER_COL &&
            Layout<int32_t>(layout, CUBLASLT_MATRIX_LAYOUT_BATCH_COUNT) == 1,
        "HC cuBLAS matrix layout differs from production BF16 projection");
}
void CheckDescriptor(cublasLtMatmulDesc_t descriptor) {
  Check(Descriptor<cublasComputeType_t>(descriptor,
                                        CUBLASLT_MATMUL_DESC_COMPUTE_TYPE) ==
                CUBLAS_COMPUTE_32F &&
            Descriptor<cudaDataType_t>(
                descriptor, CUBLASLT_MATMUL_DESC_SCALE_TYPE) == CUDA_R_32F &&
            Descriptor<cublasLtPointerMode_t>(
                descriptor, CUBLASLT_MATMUL_DESC_POINTER_MODE) ==
                CUBLASLT_POINTER_MODE_HOST &&
            Descriptor<cublasOperation_t>(
                descriptor, CUBLASLT_MATMUL_DESC_TRANSA) == CUBLAS_OP_T &&
            Descriptor<cublasOperation_t>(
                descriptor, CUBLASLT_MATMUL_DESC_TRANSB) == CUBLAS_OP_N &&
            Descriptor<cublasLtEpilogue_t>(descriptor,
                                           CUBLASLT_MATMUL_DESC_EPILOGUE) ==
                CUBLASLT_EPILOGUE_DEFAULT,
        "HC cuBLAS compute/scalar/transposition contract changed");
}

struct Projection {
  std::string name;
  int n, k;
};
const DecoderLayer* active = nullptr;
fs::path root;
std::map<const void*, Projection> projections;
std::set<std::string> saved;
std::string prefix;
size_t events = 0;

void Emit(const std::string& name, const void* pointer, size_t count, bool fp32,
          cudaStream_t stream) {
  const auto path = root / (prefix + name);
  Check(saved.insert(path.string()).second, "duplicate upstream boundary");
  fs::create_directories(root);
  if (fp32) {
    std::vector<float> values;
    Read(values, pointer, count, stream);
    Save(path, values);
  } else {
    std::vector<uint16_t> values;
    Read(values, pointer, count, stream);
    Save(path, values);
  }
  std::ofstream manifest(root / "events.jsonl", std::ios::app);
  manifest << "{\"index\":" << events++ << ",\"file\":\""
           << path.filename().string() << "\",\"sha256\":\"" << FileHash(path)
           << "\",\"elements\":" << count << ",\"dtype\":\""
           << (fp32 ? "f32" : "bf16") << "\",\"layer\":" << active->layer_id
           << ",\"position\":" << context.position()
           << ",\"rows\":" << context.rows() << ",\"prefix_sha256\":\""
           << trace::HexDigest(Hash(context.tokens())) << "\"}\n";
  manifest.close();
  Check(bool(manifest), "upstream event write");
}
void Register(const DecoderLayer& layer) {
  projections.clear();
  auto add = [&](const void* pointer, const std::string& name, int n, int k) {
    Check(
        pointer && projections.emplace(pointer, Projection{name, n, k}).second,
        "invalid projection identity");
  };
  for (const auto& [w, name] :
       {std::pair{&layer.attn_hc, "attn_hc"}, {&layer.mlp_hc, "mlp_hc"}}) {
    add(w->mix_down, std::string(name) + ".down", w->lowrank, w->hc_dim());
    add(w->mix_up, std::string(name) + ".up", w->hc_dim(), w->lowrank);
    add(w->block_inject, std::string(name) + ".inject", w->hc_count,
        w->hc_dim());
  }
  const auto& w = layer.linear;
  const int qkv = 2 * w.nkh * w.kd + w.nv * w.vd;
  add(w.in_proj_qkv, "gdn.qkv", qkv, w.hidden_size);
  add(w.in_proj_z, "gdn.z", w.nv * w.vd, w.hidden_size);
  add(w.in_proj_a, "gdn.a", w.nv, w.hidden_size);
  add(w.in_proj_b, "gdn.beta", w.nv, w.hidden_size);
  add(w.out_proj, "gdn.out", w.hidden_size, w.nv * w.vd);
  add(layer.mlp.gate, "moe.router", layer.mlp.E, layer.hs);
  if (layer.has_ple) {
    add(layer.ple.key_proj, "ple.key", layer.hc_dim, layer.ple.ple_embed_dim);
    add(layer.ple.value_proj, "ple.value", layer.hs, layer.ple.ple_embed_dim);
  }
}
const Projection* Find(const void* weight) {
  if (!active) return nullptr;
  const auto found = projections.find(weight);
  return found == projections.end() ? nullptr : &found->second;
}
void Observe(const Projection& p, const uint16_t* x, const uint16_t* w,
             const uint16_t* y, int m, int n, int k, float alpha,
             const char* dispatch, cudaStream_t stream) {
  Check(m == context.rows() && n == p.n && k == p.k && alpha == 1.0f,
        "projection shape/scale mismatch");
  Check(x != y, "projection capture requires nonaliasing input/output");
  Emit(p.name + ".x", x, static_cast<size_t>(m) * k, false, stream);
  Emit(p.name + ".y", y, static_cast<size_t>(m) * n, false, stream);
  // Save each actual weight once. Subsequent invocations use the same model
  // allocation; the model is read-only throughout this driver.
  const std::string weight_name =
      "layer" + std::to_string(active->layer_id) + "." + p.name + ".weight";
  if (!fs::exists(root / weight_name)) {
    std::vector<uint16_t> weights;
    Read(weights, w, static_cast<size_t>(n) * k, stream);
    Save(root / weight_name, weights);
  }
  std::ofstream metadata(root / (prefix + p.name + ".json"));
  metadata << "{\"m\":" << m << ",\"n\":" << n << ",\"k\":" << k
           << ",\"alpha\":1,\"beta\":0,\"dispatch\":\"" << dispatch
           << "\",\"weight\":\"" << weight_name << "\",\"weight_sha256\":\""
           << FileHash(root / weight_name) << "\"}\n";
  metadata.close();
  Check(bool(metadata), "projection metadata write");
}
#define DECODER_ABI                                                        \
  "_ZN3q4t5model19DecoderLayerForwardERKNS0_12DecoderLayerEPKtS5_PtPKiS8_" \
  "iPvmP11CUstream_stPfS6_iiS8_S8_iS6_PKNS0_11RaggedBatchEiPNS_"           \
  "5trace15RouterCollectorEiiSC_S6_S6_"
#define DECODER_ARGS                                                       \
  const DecoderLayer &layer, const uint16_t *input, const uint16_t *ple,   \
      uint16_t *output, const int *positions, const int *rope, int rows,   \
      void *workspace, size_t bytes, cudaStream_t stream, float *ssm_ckpt, \
      uint16_t *conv_ckpt, int num_ckpt, int seq, const int *seq_ids,      \
      const int *rope_ids, int tokens_per_seq, uint16_t *ple_ckpt,         \
      const RaggedBatch *ragged, int max_position,                         \
      trace::RouterCollector *trace, int layer_id, int stride,             \
      float *ssm_capture, uint16_t *qkv_capture, uint16_t *gated_capture
Status RealDecoder(DECODER_ARGS) asm("__real_" DECODER_ABI);
Status WrapDecoder(DECODER_ARGS) asm("__wrap_" DECODER_ABI);
Status WrapDecoder(DECODER_ARGS) {
  Check(!active, "nested decoder capture");
  const bool enabled =
      capture_enabled && context.position() > 0 && layer_id < 3;
  try {
    if (enabled) {
      Check(layer_id >= 0 && layer.layer_id == layer_id && !seq_ids &&
                seq == 0 && !ragged && rows == context.rows() &&
                (rows == 1 || rows == 4),
            "unsupported upstream invocation");
      active = &layer;
      Register(layer);
      prefix = "p" + std::to_string(context.position()) + "-t" +
               std::to_string(rows) + "-l" + std::to_string(layer_id) + ".";
      Emit("trunk.in", input, static_cast<size_t>(rows) * layer.hc_dim, false,
           stream);
      Emit("ssm.before", layer.ssm_state, 48 * 128 * 128, true, stream);
      Emit("conv.before", layer.conv_state, 10240 * 3, false, stream);
      if (layer.has_ple) {
        Emit("ple.embeddings", ple,
             static_cast<size_t>(rows) * layer.ple.ple_embed_dim, false,
             stream);
        Emit("ple.conv.before", layer.ple_conv_state, 10240 * 9, false, stream);
      }
    }
    auto status = RealDecoder(layer, input, ple, output, positions, rope, rows,
                              workspace, bytes, stream, ssm_ckpt, conv_ckpt,
                              num_ckpt, seq, seq_ids, rope_ids, tokens_per_seq,
                              ple_ckpt, ragged, max_position, trace, layer_id,
                              stride, ssm_capture, qkv_capture, gated_capture);
    if (enabled && status.ok()) {
      Emit("trunk.out", output, static_cast<size_t>(rows) * layer.hc_dim, false,
           stream);
      Emit("ssm.after", layer.ssm_state, 48 * 128 * 128, true, stream);
      Emit("conv.after", layer.conv_state, 10240 * 3, false, stream);
      if (layer.has_ple)
        Emit("ple.conv.after", layer.ple_conv_state, 10240 * 9, false, stream);
    }
    active = nullptr;
    return status;
  } catch (const std::exception& error) {
    cudaStreamSynchronize(stream);
    active = nullptr;
    return Status::Fail(error.what());
  }
}
#define GEV_ABI "_ZN3q4t5model7Bf16GevEPKtS2_PtiifP11CUstream_st"
bool RealGev(const uint16_t*, const uint16_t*, uint16_t*, int, int, float,
             cudaStream_t) asm("__real_" GEV_ABI);
bool WrapGev(const uint16_t*, const uint16_t*, uint16_t*, int, int, float,
             cudaStream_t) asm("__wrap_" GEV_ABI);
bool WrapGev(const uint16_t* x, const uint16_t* w, uint16_t* y, int n, int k,
             float alpha, cudaStream_t stream) {
  const bool success = RealGev(x, w, y, n, k, alpha, stream);
  if (const auto* p = Find(w); p && success)
    Observe(*p, x, w, y, 1, n, k, alpha, "gemv", stream);
  return success;
}
#define SMALL_ABI "_ZN3q4t5model14Bf16SmallMGemmEPKtS2_PtiiifP11CUstream_st"
bool RealSmall(const uint16_t*, const uint16_t*, uint16_t*, int, int, int,
               float, cudaStream_t) asm("__real_" SMALL_ABI);
bool WrapSmall(const uint16_t*, const uint16_t*, uint16_t*, int, int, int,
               float, cudaStream_t) asm("__wrap_" SMALL_ABI);
bool WrapSmall(const uint16_t* x, const uint16_t* w, uint16_t* y, int m, int n,
               int k, float alpha, cudaStream_t stream) {
  const bool success = RealSmall(x, w, y, m, n, k, alpha, stream);
  if (const auto* p = Find(w); p && success)
    Observe(*p, x, w, y, m, n, k, alpha, "small_m", stream);
  return success;
}
}  // namespace q4t::diagnostic::upstream

extern "C" cublasStatus_t CUBLASWINAPI __real_cublasLtMatmul(
    cublasLtHandle_t, cublasLtMatmulDesc_t, const void*, const void*,
    cublasLtMatrixLayout_t, const void*, cublasLtMatrixLayout_t, const void*,
    const void*, cublasLtMatrixLayout_t, void*, cublasLtMatrixLayout_t,
    const cublasLtMatmulAlgo_t*, void*, size_t, cudaStream_t);
extern "C" cublasStatus_t CUBLASWINAPI __wrap_cublasLtMatmul(
    cublasLtHandle_t handle, cublasLtMatmulDesc_t descriptor, const void* alpha,
    const void* weight, cublasLtMatrixLayout_t wl, const void* input,
    cublasLtMatrixLayout_t il, const void* beta, const void* prior,
    cublasLtMatrixLayout_t pl, void* output, cublasLtMatrixLayout_t ol,
    const cublasLtMatmulAlgo_t* algorithm, void* workspace, size_t bytes,
    cudaStream_t stream) {
  using namespace q4t::diagnostic::upstream;
  const auto* projection = Find(weight);
  if (projection) {
    Check(alpha && beta && prior == output && algorithm,
          "cuBLAS pointer contract");
    Check(*static_cast<const float*>(alpha) == 1 &&
              *static_cast<const float*>(beta) == 0,
          "cuBLAS scale");
    CheckDescriptor(descriptor);
    CheckLayout(wl, projection->k, projection->n, projection->k);
    CheckLayout(il, projection->k, context.rows(), projection->k);
    CheckLayout(pl, projection->n, context.rows(), projection->n);
    CheckLayout(ol, projection->n, context.rows(), projection->n);
  }
  const auto status = __real_cublasLtMatmul(
      handle, descriptor, alpha, weight, wl, input, il, beta, prior, pl, output,
      ol, algorithm, workspace, bytes, stream);
  if (projection && status == CUBLAS_STATUS_SUCCESS)
    Observe(*projection, static_cast<const uint16_t*>(input),
            static_cast<const uint16_t*>(weight),
            static_cast<const uint16_t*>(output), context.rows(), projection->n,
            projection->k, 1.0f, "cublasLt", stream);
  return status;
}
int main(int argc, char** argv) {
  if (argc != 7) return Q4tRealCaptureOriginalMain(argc, argv);
  q4t::diagnostic::upstream::root = std::filesystem::path(argv[3]) / "upstream";
  return Q4tRealCaptureOriginalMain(argc, argv);
}
