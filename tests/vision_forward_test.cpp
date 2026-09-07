// End-to-end vision tower test: CUDA forward vs numpy CPU reference.
//
// Loads the synthetic test image (seed 1234, 4x4 patches) and the expected
// output from tools/vision_ref.json, runs the CUDA vision tower on the same
// input, and compares the outputs.  Skipped when CUDA, the model, or the
// reference file are absent.
#include "q4t/io/weight_loader.h"
#include "q4t/test.h"
#include "q4t/vision/vision.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::Status;
using q4t::io::WeightIndex;
using q4t::io::WeightLoader;
using q4t::vision::ImageShape;
using q4t::vision::LoadVision;
using q4t::vision::VisionConfig;
using q4t::vision::VisionForward;
using q4t::vision::VisionOutputBytes;
using q4t::vision::VisionTower;

const char* kModelDir =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream";
const char* kIndex =
    "/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/"
    "model.safetensors.index.json";
const char* kRefJson =
    "/home/rm01/models/dev/qwen4-thor/tools/vision_ref.json";

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

// Minimal JSON parser for the vision_ref.json structure:
//   {"grid_thw": [[t,h,w]], "pixel_values": [[...]], "output": [[...]],
//    "output_shape": [rows, cols]}
// Returns false on parse failure.
bool ParseRefJson(const std::string& path, std::vector<int>* grid_thw,
                  std::vector<float>* pixel_values, std::vector<float>* output,
                  int* out_rows, int* out_cols) {
  FILE* f = fopen(path.c_str(), "rb");
  if (!f) return false;
  fseek(f, 0, SEEK_END);
  long sz = ftell(f);
  fseek(f, 0, SEEK_SET);
  std::string content;
  content.resize(static_cast<size_t>(sz));
  if (fread(&content[0], 1, content.size(), f) != content.size()) {
    fclose(f);
    return false;
  }
  fclose(f);

  auto find_key = [&](const std::string& key) -> size_t {
    return content.find("\"" + key + "\"");
  };

  // grid_thw: [[t, h, w]]
  {
    size_t k = find_key("grid_thw");
    if (k == std::string::npos) return false;
    size_t b1 = content.find('[', k);
    size_t b2 = content.find('[', b1 + 1);
    size_t e2 = content.find(']', b2);
    std::string inner = content.substr(b2 + 1, e2 - b2 - 1);
    int vals[3] = {0};
    size_t p = 0;
    for (int i = 0; i < 3 && p < inner.size(); ++i) {
      while (p < inner.size() && (inner[p] == ' ' || inner[p] == '\t' ||
                                  inner[p] == '\n' || inner[p] == ','))
        ++p;
      char* end = nullptr;
      vals[i] = static_cast<int>(strtol(inner.c_str() + p, &end, 10));
      p = static_cast<size_t>(end - inner.c_str());
    }
    grid_thw->assign(vals, vals + 3);
  }

  // pixel_values: [[...], ...]
  {
    size_t k = find_key("pixel_values");
    if (k == std::string::npos) return false;
    size_t b1 = content.find('[', k);
    // Find the matching closing bracket.
    int depth = 0;
    size_t e1 = b1;
    for (size_t i = b1; i < content.size(); ++i) {
      if (content[i] == '[') ++depth;
      if (content[i] == ']') --depth;
      if (depth == 0) {
        e1 = i;
        break;
      }
    }
    std::string block = content.substr(b1, e1 - b1 + 1);
    // Extract all floats.
    for (size_t i = 0; i < block.size(); ++i) {
      if (block[i] == '[' || block[i] == ']' || block[i] == ',' ||
          block[i] == ' ' || block[i] == '\n' || block[i] == '\t')
        continue;
      char* ptr = nullptr;
      float v = strtof(block.c_str() + i, &ptr);
      if (ptr != block.c_str() + i) {
        pixel_values->push_back(v);
        i = static_cast<size_t>(ptr - block.c_str()) - 1;
      }
    }
  }

  // output: [[...], ...]
  {
    size_t k = find_key("output");
    if (k == std::string::npos) return false;
    size_t b1 = content.find('[', k);
    int depth = 0;
    size_t e1 = b1;
    for (size_t i = b1; i < content.size(); ++i) {
      if (content[i] == '[') ++depth;
      if (content[i] == ']') --depth;
      if (depth == 0) {
        e1 = i;
        break;
      }
    }
    std::string block = content.substr(b1, e1 - b1 + 1);
    for (size_t i = 0; i < block.size(); ++i) {
      if (block[i] == '[' || block[i] == ']' || block[i] == ',' ||
          block[i] == ' ' || block[i] == '\n' || block[i] == '\t')
        continue;
      char* ptr = nullptr;
      float v = strtof(block.c_str() + i, &ptr);
      if (ptr != block.c_str() + i) {
        output->push_back(v);
        i = static_cast<size_t>(ptr - block.c_str()) - 1;
      }
    }
  }

  // output_shape: [rows, cols]
  {
    size_t k = find_key("output_shape");
    if (k == std::string::npos) return false;
    size_t b1 = content.find('[', k);
    size_t e1 = content.find(']', b1);
    std::string inner = content.substr(b1 + 1, e1 - b1 - 1);
    size_t p = 0;
    while (p < inner.size() && (inner[p] == ' ' || inner[p] == '\t' ||
                                inner[p] == '\n'))
      ++p;
    char* end = nullptr;
    *out_rows = static_cast<int>(strtol(inner.c_str() + p, &end, 10));
    p = static_cast<size_t>(end - inner.c_str());
    while (p < inner.size() && (inner[p] == ' ' || inner[p] == ',' ||
                                inner[p] == '\t' || inner[p] == '\n'))
      ++p;
    *out_cols = static_cast<int>(strtol(inner.c_str() + p, &end, 10));
  }

  return true;
}

}  // namespace

Q4T_TEST(vision_forward) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  if (!FileExists(kIndex)) {
    std::printf("  (skipped: model index not found)\n");
    return true;
  }
  if (!FileExists(kRefJson)) {
    std::printf("  (skipped: vision reference not found)\n");
    return true;
  }

  // 1. Parse the numpy reference.
  std::vector<int> grid_thw;
  std::vector<float> ref_pixels, ref_output;
  int out_rows = 0, out_cols = 0;
  if (!ParseRefJson(kRefJson, &grid_thw, &ref_pixels, &ref_output, &out_rows,
                    &out_cols)) {
    std::printf("  failed to parse %s\n", kRefJson);
    return false;
  }
  const int t = grid_thw[0], h = grid_thw[1], w = grid_thw[2];
  const int L = t * h * w;
  const int in_dim = 3 * 2 * 16 * 16;  // C*T*P*P
  std::printf("  grid=%dx%dx%d L=%d in_dim=%d out=[%d,%d]\n", t, h, w, L,
              in_dim, out_rows, out_cols);

  // 2. Open the weight loader.
  WeightIndex* index = nullptr;
  Status s = WeightIndex::Open(kIndex, &index);
  if (!s.ok()) {
    std::printf("  index open failed: %s\n", s.message().c_str());
    return false;
  }
  WeightLoader* loader = nullptr;
  s = WeightLoader::Create(kModelDir, *index, 8, &loader);
  if (!s.ok()) {
    std::printf("  loader create failed: %s\n", s.message().c_str());
    delete index;
    return false;
  }

  // 3. Load vision weights.
  VisionConfig cfg;
  VisionTower tower;
  std::string err;
  if (!LoadVision(*loader, cfg, &tower, &err)) {
    std::printf("  LoadVision failed: %s\n", err.c_str());
    delete loader;
    delete index;
    return false;
  }
  std::printf("  vision weights loaded\n");

  // 4. Prepare input (float32 -> BF16, H2D).
  std::vector<uint16_t> pixels_bf16(ref_pixels.size());
  for (size_t i = 0; i < ref_pixels.size(); ++i)
    pixels_bf16[i] = FloatToBf16(ref_pixels[i]);
  uint16_t* d_pixels = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_pixels),
                 ref_pixels.size() * sizeof(uint16_t)) != cudaSuccess) {
    std::printf("  cudaMalloc pixels failed\n");
    tower.Free();
    delete loader;
    delete index;
    return false;
  }
  cudaMemcpy(d_pixels, pixels_bf16.data(),
             ref_pixels.size() * sizeof(uint16_t), cudaMemcpyHostToDevice);

  // 5. Allocate workspace + run forward.
  ImageShape shape;
  shape.h = h;
  shape.w = w;
  shape.t = t;
  std::vector<ImageShape> shapes = {shape};
  if (!tower.Allocate(shapes, 0)) {
    std::printf("  Allocate failed\n");
    cudaFree(d_pixels);
    tower.Free();
    delete loader;
    delete index;
    return false;
  }
  const size_t out_bytes = VisionOutputBytes(cfg, shapes);
  uint16_t* d_out = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_out), out_bytes) !=
      cudaSuccess) {
    std::printf("  cudaMalloc out failed\n");
    cudaFree(d_pixels);
    tower.Free();
    delete loader;
    delete index;
    return false;
  }
  if (!VisionForward(tower, d_pixels, shapes, d_out, &err, 0)) {
    std::printf("  VisionForward failed: %s\n", err.c_str());
    cudaFree(d_pixels);
    cudaFree(d_out);
    tower.Free();
    delete loader;
    delete index;
    return false;
  }
  cudaDeviceSynchronize();

  // 6. Download + compare.
  const size_t out_n = out_rows * out_cols;
  std::vector<uint16_t> out_bf16(out_n);
  cudaMemcpy(out_bf16.data(), d_out, out_n * sizeof(uint16_t),
             cudaMemcpyDeviceToHost);

  double max_abs_diff = 0.0;
  double max_ref_abs = 0.0;
  double l2_num = 0.0, l2_den = 0.0;
  for (size_t i = 0; i < out_n; ++i) {
    const float cuda_v = Bf16ToFloat(out_bf16[i]);
    const float ref_v = ref_output[i];
    const double d = std::abs(static_cast<double>(cuda_v) -
                              static_cast<double>(ref_v));
    max_abs_diff = std::max(max_abs_diff, d);
    max_ref_abs = std::max(max_ref_abs, std::abs(static_cast<double>(ref_v)));
    l2_num += d * d;
    l2_den += static_cast<double>(ref_v) * static_cast<double>(ref_v);
  }
  const double l2_rel = std::sqrt(l2_num) / (std::sqrt(l2_den) + 1e-6);
  std::printf("  output[0,:4] cuda = [%.6f, %.6f, %.6f, %.6f]\n",
              Bf16ToFloat(out_bf16[0]), Bf16ToFloat(out_bf16[1]),
              Bf16ToFloat(out_bf16[2]), Bf16ToFloat(out_bf16[3]));
  std::printf("  output[0,:4] ref  = [%.6f, %.6f, %.6f, %.6f]\n",
              ref_output[0], ref_output[1], ref_output[2], ref_output[3]);
  std::printf("  max_abs_diff = %.6f  max_ref_abs = %.6f  l2_rel = %.6f\n",
              max_abs_diff, max_ref_abs, l2_rel);

  cudaFree(d_pixels);
  cudaFree(d_out);
  tower.Free();
  delete loader;
  delete index;

  // BF16 GEMM vs FP32 numpy: expect small relative error.
  Q4T_CHECK(l2_rel < 0.05);
  Q4T_CHECK(max_abs_diff < 0.05);
  return true;
}
