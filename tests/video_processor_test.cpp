// Differential test: C++ video processor vs the real transformers 5.16.1
// Qwen3VLVideoProcessor (torchvision backend) ground truth.
//
// Reference (tools/video_processor_ref.py) runs the real processor on
// deterministic synthetic frame sequences (saved as lossless PNGs so the C++
// side decodes the EXACT same uint8 pixels) and records pixel_values_videos +
// video_grid_thw to tools/video_processor_ref.json.
//
// NOTE on resize backend: the reference resizes with torchvision
// `tvF.resize(..., BICUBIC, antialias=True)`; the C++ reuses the Pillow 12.3.0
// fixed-point BICUBIC (shared with the image processor). Measured difference
// between the two backends on the 225x321->224x320 case: max_abs ~0.016
// (≈2/255 in pixel space, ×2 after the /0.5 normalize), l2_rel ~0.001. The
// tolerance is max_abs_diff <= 0.02 / l2_rel <= 0.005 (a ~2-5x margin over the
// measured backend difference, still 100x below a real layout bug). Identity
// cases (32-multiple frames) are bit-exact.
//
// Cases cover: even frames (2/4/8), odd frames (5 -> pad last frame), and a
// larger resolution (448x640). Skipped when the reference files are absent.
#include "q4t/test.h"
#include "q4t/vision/processor.h"
#include "q4t/io/json.h"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

using q4t::vision::ProcessVideo;
using q4t::vision::ProcessorConfig;
using q4t::vision::ProcessedVideo;

const char* kDir = "/home/rm01/models/dev/qwen4-thor/tools";

bool FileExists(const char* path) {
  int fd = open(path, O_RDONLY);
  if (fd < 0) return false;
  close(fd);
  return true;
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
  if (fread(data.data(), 1, data.size(), f) != data.size()) data.clear();
  fclose(f);
  return data;
}

// Video budget from video_preprocessor_config.json (NOT the image budget).
ProcessorConfig VideoCfg() {
  ProcessorConfig cfg;
  cfg.min_pixels = 4096;
  cfg.max_pixels = 25165824;
  return cfg;
}

// Run one case: decode the case's frame PNGs, process, compare to ground truth.
// `case_name` is the JSON key, used to locate the frame PNGs.
bool CheckCase(const q4t::io::Json& c, const std::string& case_name) {
  const int F = static_cast<int>(c.GetInt("num_frames"));
  // grid_thw is an array [t, h, w].
  const q4t::io::Json* grid = c.GetArray("grid_thw");
  if (!grid || grid->array.size() != 3) {
    std::printf("  [case] bad grid_thw\n");
    return false;
  }
  const int want_t = static_cast<int>(grid->array[0].AsInt());
  const int want_h = static_cast<int>(grid->array[1].AsInt());
  const int want_w = static_cast<int>(grid->array[2].AsInt());
  // pixel_values is [L, patch_dim]: a nested array, one row per patch.
  const q4t::io::Json* pv = c.GetArray("pixel_values");
  if (!pv) {
    std::printf("  [case] missing pixel_values\n");
    return false;
  }
  std::vector<float> gt_pv;
  for (const auto& row : pv->array) {
    if (!row.IsArray()) {
      std::printf("  [case] pixel_values row is not an array\n");
      return false;
    }
    for (const auto& v : row.array)
      gt_pv.push_back(static_cast<float>(v.AsDouble()));
  }

  // Decode the frame PNGs (saved losslessly by the reference).
  std::vector<const uint8_t*> frames;
  std::vector<size_t> frame_bytes;
  std::vector<std::vector<uint8_t>> frame_data;
  for (int f = 0; f < F; ++f) {
    // The reference names frames video_frames_<case>_<i>.png (lossless PNG).
    const std::string path =
        std::string(kDir) + "/video_frames_" + case_name + "_" +
        std::to_string(f) + ".png";
    std::vector<uint8_t> d = ReadFile(path.c_str());
    if (d.empty()) {
      std::printf("  [case] missing frame %d (%s)\n", f, path.c_str());
      return false;
    }
    frame_data.push_back(std::move(d));
  }
  for (auto& d : frame_data) {
    frames.push_back(d.data());
    frame_bytes.push_back(d.size());
  }

  ProcessedVideo out;
  std::string err;
  if (!ProcessVideo(frames, frame_bytes, VideoCfg(), &out, &err)) {
    std::printf("  [case] ProcessVideo failed: %s\n", err.c_str());
    return false;
  }

  if (out.grid_t != want_t || out.grid_h != want_h || out.grid_w != want_w) {
    std::printf("  [case] grid mismatch: got [%d,%d,%d] want [%d,%d,%d]\n",
                out.grid_t, out.grid_h, out.grid_w, want_t, want_h, want_w);
    return false;
  }
  if (out.pixel_values.size() != gt_pv.size()) {
    std::printf("  [case] size mismatch: got %zu want %zu\n",
                out.pixel_values.size(), gt_pv.size());
    return false;
  }
  double max_abs = 0.0, sum_sq = 0.0, ref_sq = 0.0;
  for (size_t i = 0; i < out.pixel_values.size(); ++i) {
    double d = std::fabs(static_cast<double>(out.pixel_values[i]) -
                         static_cast<double>(gt_pv[i]));
    max_abs = std::max(max_abs, d);
    sum_sq += d * d;
    ref_sq += static_cast<double>(gt_pv[i]) * gt_pv[i];
  }
  double l2_rel = ref_sq > 0 ? std::sqrt(sum_sq / ref_sq) : 0.0;
  std::printf("  [case] F=%d grid=[%d,%d,%d] L=%d max_abs_diff=%.6f "
              "l2_rel=%.6f\n",
              F, out.grid_t, out.grid_h, out.grid_w, out.L(), max_abs, l2_rel);

  // Tolerance: identity-resize cases (32-multiple frames) are bit-exact
  // (max_abs == 0). The BICUBIC case (225x321 -> 224x320) differs only by the
  // torchvision-vs-Pillow backend: measured max_abs ~0.016 (≈2/255 in pixel
  // space, ×2 after /0.5 normalize), l2_rel ~0.001. Allow 0.02 max and 0.005
  // l2_rel — a ~2-5x margin over the measured backend difference, yet still
  // 100x below a real layout bug (which gives max_abs ~2.0) and well under
  // the BF16 precision band (~0.03).
  if (max_abs > 0.02) {
    std::printf("  [case] FAIL: max_abs_diff=%.6f > 0.02\n", max_abs);
    return false;
  }
  if (l2_rel > 0.005) {
    std::printf("  [case] FAIL: l2_rel=%.6f > 0.005\n", l2_rel);
    return false;
  }
  return true;
}

}  // namespace

Q4T_TEST(video_processor) {
  const std::string json_path = std::string(kDir) + "/video_processor_ref.json";
  if (!FileExists(json_path.c_str())) {
    std::printf("  [video_processor] skipped: reference absent\n");
    return true;
  }
  std::vector<uint8_t> raw = ReadFile(json_path.c_str());
  std::string content(raw.begin(), raw.end());
  q4t::io::Json root;
  q4t::Status s = q4t::io::ParseJson(content, &root);
  if (!s.ok()) {
    std::printf("  [video_processor] parse failed: %s\n", s.message().c_str());
    return false;
  }
  bool all_ok = true;
  int n = 0;
  for (const auto& kv : root.object) {
    all_ok = CheckCase(kv.second, kv.first) && all_ok;
    ++n;
  }
  std::printf("  [video_processor] %d cases\n", n);
  return all_ok && n > 0;
}
