#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>
#include "q4t/vision/processor.h"
int main(int argc, char** argv) {
  if (argc != 3) return 2;
  std::ifstream in(argv[1], std::ios::binary);
  const std::string png(std::istreambuf_iterator<char>(in), {});
  std::vector<const uint8_t*> frames(4, reinterpret_cast<const uint8_t*>(png.data()));
  std::vector<size_t> sizes(4, png.size());
  q4t::vision::ProcessorConfig config;
  config.min_pixels = 4096;
  config.max_pixels = 25165824;
  q4t::vision::ProcessedVideo video;
  std::string error;
  if (!q4t::vision::ProcessVideo(frames, sizes, config, &video, &error)) {
    std::cerr << error << '\n'; return 3;
  }
  std::ofstream out(argv[2], std::ios::binary);
  out.write(reinterpret_cast<const char*>(video.pixel_values.data()),
            video.pixel_values.size() * sizeof(float));
  std::cout << "{\"grid\":[" << video.grid_t << ',' << video.grid_h << ','
            << video.grid_w << "],\"values\":" << video.pixel_values.size() << "}\n";
  return out.good() ? 0 : 4;
}
