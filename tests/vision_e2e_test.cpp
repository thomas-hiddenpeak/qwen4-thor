// End-to-end multimodal test: real image -> processor -> vision tower ->
// image-token expansion -> main-model prefill with feature injection ->
// logits.
//
// This exercises the FULL chain that the serve layer drives:
//   1. Read a real PNG (tools/vision_test_a.png, 256x256).
//   2. vision::ProcessImage -> pixel_values [L, 96] + grid_thw.
//   3. H2D as BF16, vision::VisionForward -> merged features [L/4, 2560].
//   4. Build input_ids with a single <image> placeholder (248056).
//   5. model::ExpandImageTokens -> grid_h/2 * grid_w/2 image tokens.
//   6. model::ModelForward with VisionFeatures -> logits [T, vocab].
//
// Checks (mechanism smoke test, no full-stack CPU reference yet):
//   - the chain runs without error;
//   - logits are finite and non-trivial;
//   - injection changes the logits vs a pure-text baseline (the image token
//     embeddings are actually replaced by the vision features);
//   - two identical runs are bit-identical (determinism).
//
// Skipped when CUDA, the model, the PLE sidecar, or the test image are absent.
#include "q4t/io/weight_loader.h"
#include "q4t/model/model.h"
#include "q4t/test.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/vision.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::model::ExpandImageTokens;
using q4t::model::LoadModel;
using q4t::model::Model;
using q4t::model::ModelConfig;
using q4t::model::ModelForward;
using q4t::model::VisionFeatures;
using q4t::vision::ImageShape;
using q4t::vision::LoadVision;
using q4t::vision::ProcessImage;
using q4t::vision::ProcessorConfig;
using q4t::vision::ProcessedImage;
using q4t::vision::VisionConfig;
using q4t::vision::VisionForward;
using q4t::vision::VisionOutputBytes;
using q4t::vision::VisionTower;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kPleSidecar =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "ple/qwen3.8-flash-next-ple-fp8.bin";
const char* kTestImage =
    "/home/rm01/models/dev/qwen4-thor/tools/vision_test_a.png";

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
}
bool CudaAvailable() {
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}
float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
uint16_t FloatToBf16(float f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const uint16_t*>(&b);
}
std::vector<uint8_t> ReadFile(const char* path) {
  std::vector<uint8_t> data;
  FILE* f = fopen(path, "rb");
  if (!f) return data;
  fseek(f, 0, SEEK_END);
  long sz = ftell(f);
  fseek(f, 0, SEEK_SET);
  if (sz <= 0) {
    fclose(f);
    return data;
  }
  data.resize(static_cast<size_t>(sz));
  if (fread(data.data(), 1, data.size(), f) != static_cast<size_t>(sz))
    data.clear();
  fclose(f);
  return data;
}
double MaxAbsDiff(const std::vector<uint16_t>& a,
                  const std::vector<uint16_t>& b) {
  double m = 0.0;
  for (size_t i = 0; i < a.size(); ++i) {
    m = std::max(m, static_cast<double>(
                        std::fabs(Bf16ToFloat(a[i]) - Bf16ToFloat(b[i]))));
  }
  return m;
}

}  // namespace

Q4T_TEST(vision_e2e) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex) || !FileExists(kPleSidecar) ||
      !FileExists(kTestImage)) {
    std::printf("  (skipped: model, PLE sidecar, or test image not found)\n");
    return true;
  }

  // --- Load the main model (2 layers, including the PLE layer).
  const int num_layers = [] {
    const char* e = std::getenv("Q4T_MODEL_LAYERS");
    return e ? std::atoi(e) : 2;
  }();
  ModelConfig cfg;
  cfg.model_dir = kModelDir;
  cfg.index_path = kIndex;
  cfg.num_layers = num_layers;
  cfg.max_prefill = 512;
  cfg.ple_sidecar = kPleSidecar;

  Model m;
  Status s = LoadModel(cfg, &m, nullptr);
  if (!s.ok()) {
    std::printf("  model load failed: %s\n", s.message().c_str());
    return false;
  }

  // --- Load the vision tower.
  WeightIndex* index = nullptr;
  s = WeightIndex::Open(kIndex, &index);
  if (!s.ok()) {
    std::printf("  index open failed: %s\n", s.message().c_str());
    m.Free();
    return false;
  }
  WeightLoader* loader = nullptr;
  s = WeightLoader::Create(kModelDir, *index, 8, &loader);
  if (!s.ok()) {
    std::printf("  loader create failed: %s\n", s.message().c_str());
    m.Free();
    delete index;
    return false;
  }
  VisionConfig vcfg;
  VisionTower tower;
  std::string err;
  if (!LoadVision(*loader, vcfg, &tower, &err)) {
    std::printf("  LoadVision failed: %s\n", err.c_str());
    m.Free();
    delete loader;
    delete index;
    return false;
  }
  delete loader;
  delete index;

  // --- 1. Process the real image (CPU).
  std::vector<uint8_t> img = ReadFile(kTestImage);
  ProcessorConfig pcfg;
  ProcessedImage pi;
  if (!ProcessImage(img.data(), img.size(), pcfg, &pi, &err)) {
    std::printf("  ProcessImage failed: %s\n", err.c_str());
    tower.Free();
    m.Free();
    return false;
  }
  const int L = pi.L();
  const int patch_dim = pi.patch_dim();
  const int merged = (pi.grid_h / pcfg.merge_size) *
                     (pi.grid_w / pcfg.merge_size) * pi.grid_t;
  std::printf("  image grid=[%d,%d,%d] L=%d merged=%d\n", pi.grid_t,
              pi.grid_h, pi.grid_w, L, merged);
  Q4T_CHECK(L > 0);
  Q4T_CHECK(merged > 0);

  // --- 2. H2D pixel_values as BF16 + run the vision tower.
  std::vector<uint16_t> pixels_bf16(static_cast<size_t>(L) * patch_dim);
  for (size_t i = 0; i < pixels_bf16.size(); ++i)
    pixels_bf16[i] = FloatToBf16(pi.pixel_values[i]);
  uint16_t* d_pixels = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_pixels),
                 pixels_bf16.size() * sizeof(uint16_t)) != cudaSuccess) {
    std::printf("  cudaMalloc pixels failed\n");
    tower.Free();
    m.Free();
    return false;
  }
  cudaMemcpy(d_pixels, pixels_bf16.data(), pixels_bf16.size() * sizeof(uint16_t),
             cudaMemcpyHostToDevice);

  ImageShape shape;
  shape.h = pi.grid_h;
  shape.w = pi.grid_w;
  shape.t = pi.grid_t;
  std::vector<ImageShape> shapes = {shape};
  if (!tower.Allocate(shapes, nullptr)) {
    std::printf("  tower Allocate failed\n");
    cudaFree(d_pixels);
    tower.Free();
    m.Free();
    return false;
  }
  const size_t out_bytes = VisionOutputBytes(vcfg, shapes);
  uint16_t* d_feats = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_feats), out_bytes) !=
      cudaSuccess) {
    std::printf("  cudaMalloc feats failed\n");
    cudaFree(d_pixels);
    tower.Free();
    m.Free();
    return false;
  }
  if (!VisionForward(tower, d_pixels, shapes, d_feats, &err, nullptr)) {
    std::printf("  VisionForward failed: %s\n", err.c_str());
    cudaFree(d_pixels);
    cudaFree(d_feats);
    tower.Free();
    m.Free();
    return false;
  }
  cudaFree(d_pixels);
  cudaDeviceSynchronize();

  // --- 3. Build input_ids with a single <image> placeholder.
  // "Describe" (a few real tokens) + <image> + " this image."
  // The tokenizer encodes <image> as a single 248056 (added token).
  const int img_id = cfg.image_token_id;
  const int32_t ids[] = {25, 1203, img_id, 321, 14556};
  const int T_in = static_cast<int>(std::size(ids));

  // Expand the placeholder to `merged` image tokens.
  std::vector<int> counts = {merged};
  std::vector<int32_t> ids_expanded;
  if (!ExpandImageTokens(ids, T_in, img_id, counts, &ids_expanded)) {
    std::printf("  ExpandImageTokens failed\n");
    cudaFree(d_feats);
    tower.Free();
    m.Free();
    return false;
  }
  const int T = static_cast<int>(ids_expanded.size());
  Q4T_CHECK(T == T_in - 1 + merged);  // 1 placeholder -> merged tokens
  std::printf("  input_ids: %d -> %d (expanded %d image tokens)\n", T_in, T,
              merged);

  // --- 4. Prefill with vision features injected.
  uint16_t* d_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(T) * cfg.vocab * 2) != cudaSuccess) {
    std::printf("  cudaMalloc logits failed\n");
    cudaFree(d_feats);
    tower.Free();
    m.Free();
    return false;
  }
  std::vector<uint16_t> logits(static_cast<size_t>(T) * cfg.vocab);

  // 4a. Baseline: same ids but NO vision (image tokens keep their embedding).
  std::vector<uint16_t> base;
  {
    s = ModelForward(m, ids_expanded.data(), T, d_logits, nullptr, nullptr);
    Q4T_CHECK(s.ok());
    cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
               cudaMemcpyDeviceToHost);
    base = logits;
  }

  // 4b. Injected: vision features replace the image token embeddings.
  VisionFeatures vf;
  vf.device = d_feats;
  vf.num_tokens = merged;
  std::vector<uint16_t> inj1;
  s = ModelForward(m, ids_expanded.data(), T, d_logits, nullptr, &vf);
  Q4T_CHECK(s.ok());
  cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
             cudaMemcpyDeviceToHost);
  inj1 = logits;

  // 4c. Finite + non-trivial on the injected run.
  double max_abs = 0.0;
  bool all_finite = true;
  for (uint16_t b : inj1) {
    const float f = Bf16ToFloat(b);
    if (!std::isfinite(f)) all_finite = false;
    max_abs = std::max(max_abs, static_cast<double>(std::fabs(f)));
  }
  Q4T_CHECK(all_finite);
  Q4T_CHECK(max_abs > 1e-3);

  // 4d. Injection must change the logits (features replaced the embeddings).
  const double diff = MaxAbsDiff(base, inj1);
  std::printf("  baseline vs injected max_abs_diff = %.6f\n", diff);
  Q4T_CHECK(diff > 1e-3);

  // 4e. Determinism: a second identical injected run is bit-identical.
  std::vector<uint16_t> inj2;
  s = ModelForward(m, ids_expanded.data(), T, d_logits, nullptr, &vf);
  Q4T_CHECK(s.ok());
  cudaMemcpy(logits.data(), d_logits, logits.size() * 2,
             cudaMemcpyDeviceToHost);
  inj2 = logits;
  Q4T_CHECK(MaxAbsDiff(inj1, inj2) == 0.0);

  cudaFree(d_feats);
  cudaFree(d_logits);
  tower.Free();
  m.Free();
  return true;
}
