// Compare independent planning against the unchanged decoder/processor on
// small actual PNG/JPEG inputs. No model, CUDA dependency, or server.
#include "q4t/vision/media_plan.h"

#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

int main(int argc, char** argv) {
  using namespace q4t::vision;
  if (argc < 2) return 2;
  int cases = 0;
  for (int i = 1; i < argc; ++i) {
    std::ifstream in(argv[i], std::ios::binary);
    if (!in) return 2;
    std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(in)), {});
    MediaHeader header;
    if (!ReadMediaHeader(bytes, &header)) return 1;
    for (int frames : {1, 2, 5}) {
      const auto kind = frames == 1 ? MediaKind::kImage : MediaKind::kVideo;
      ProcessorConfig config;
      if (frames != 1) {
        config.min_pixels = 4096;
        config.max_pixels = 25165824;
      }
      MediaGeometry plan;
      const auto status = PlanMediaGeometry(kind, header.width, header.height,
                                            frames, config, &plan);
      if (!status) {
        std::fprintf(stderr, "plan failed: %s\n",
                     MediaPlanErrorCode(status.error));
        return 1;
      }
      std::string error;
      int gt = 0, gh = 0, gw = 0;
      size_t elements = 0;
      if (frames == 1) {
        ProcessedImage actual;
        if (!ProcessImage(bytes.data(), bytes.size(), config, &actual,
                          &error)) {
          std::fprintf(stderr, "processor failed: %s\n", error.c_str());
          return 1;
        }
        gt = actual.grid_t;
        gh = actual.grid_h;
        gw = actual.grid_w;
        elements = actual.pixel_values.size();
      } else {
        ProcessedVideo actual;
        if (!ProcessVideo(std::vector<const uint8_t*>(frames, bytes.data()),
                          std::vector<size_t>(frames, bytes.size()), config,
                          &actual, &error)) {
          std::fprintf(stderr, "processor failed: %s\n", error.c_str());
          return 1;
        }
        gt = actual.grid_t;
        gh = actual.grid_h;
        gw = actual.grid_w;
        elements = actual.pixel_values.size();
      }
      if (gt != plan.grid_t || gh != plan.grid_h || gw != plan.grid_w ||
          elements != plan.pixel_elements) {
        std::fprintf(stderr, "geometry mismatch: %s frames=%d\n", argv[i],
                     frames);
        return 1;
      }
      ++cases;
      std::printf("PASS %s frames=%d grid=%d,%d,%d elements=%zu\n", argv[i],
                  frames, gt, gh, gw, elements);
    }
  }
  std::printf("%d real processor cases passed\n", cases);
  return 0;
}
