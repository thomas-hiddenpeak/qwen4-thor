// Executes production host code; JSONL in/out, no CUDA or model loading.
#include <array>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/model/vision_positions.h"
#include "q4t/vision/media_plan.h"
#include "stb/stb_image.h"

int main() {
  using namespace q4t;
  std::string line;
  while (std::getline(std::cin, line)) {
    io::Json input;
    if (!io::ParseJson(line, &input)) return 2;
    if (input.GetString("kind") == "positions") {
      std::vector<int32_t> ids;
      std::vector<std::array<int, 3>> grids;
      for (const auto& id : input.GetArray("ids")->array)
        ids.push_back(id.AsInt());
      for (const auto& grid : input.GetArray("grids")->array) {
        if (grid.array.size() != 3) return 2;
        grids.push_back({static_cast<int>(grid.array[0].AsInt()),
                         static_cast<int>(grid.array[1].AsInt()),
                         static_cast<int>(grid.array[2].AsInt())});
      }
      std::vector<int> positions;
      int delta = 0;
      const auto status = model::BuildVisionRopePositions(
          ids, 2, 10, 11, ids.size(), grids, &positions, &delta);
      std::cout << "{\"ok\":" << (status ? "true" : "false")
                << ",\"delta\":" << delta << ",\"positions\":[";
      for (size_t i = 0; i < positions.size(); ++i)
        std::cout << (i ? "," : "") << positions[i];
      std::cout << "]}\n";
    } else if (input.GetString("kind") == "geometry") {
      const int frames = input.GetInt("frames", 1);
      vision::ProcessorConfig cfg;
      if (frames != 1) {
        cfg.min_pixels = 4096;
        cfg.max_pixels = 25165824;
      }
      vision::MediaGeometry geometry;
      const auto status = vision::PlanMediaGeometry(
          frames == 1 ? vision::MediaKind::kImage : vision::MediaKind::kVideo,
          input.GetInt("width"), input.GetInt("height"), frames, cfg,
          &geometry);
      std::cout << "{\"ok\":" << (status ? "true" : "false")
                << ",\"height\":" << geometry.height
                << ",\"width\":" << geometry.width << ",\"grid\":["
                << geometry.grid_t << "," << geometry.grid_h << ","
                << geometry.grid_w << "]}\n";
    } else if (input.GetString("kind") == "decode") {
      int width = 0, height = 0, channels = 0;
      auto* pixels = stbi_load(input.GetString("file").c_str(), &width, &height,
                               &channels, 3);
      if (!pixels) return 1;
      std::ofstream output(input.GetString("output"), std::ios::binary);
      output.write(reinterpret_cast<const char*>(pixels),
                   static_cast<size_t>(width) * height * 3);
      stbi_image_free(pixels);
      if (!output) return 2;
      std::cout << "{\"width\":" << width << ",\"height\":" << height << "}\n";
    } else if (input.GetString("kind") == "pixels") {
      std::vector<std::vector<uint8_t>> storage;
      for (const auto& file : input.GetArray("files")->array) {
        std::ifstream in(file.str, std::ios::binary);
        if (!in) return 2;
        storage.emplace_back(std::istreambuf_iterator<char>(in),
                             std::istreambuf_iterator<char>());
      }
      vision::ProcessorConfig cfg;
      std::vector<float> pixels;
      std::string error;
      int gt = 0, gh = 0, gw = 0;
      if (storage.size() == 1) {
        vision::ProcessedImage result;
        if (!vision::ProcessImage(storage[0].data(), storage[0].size(), cfg,
                                  &result, &error))
          return 1;
        pixels = std::move(result.pixel_values);
        gt = result.grid_t;
        gh = result.grid_h;
        gw = result.grid_w;
      } else {
        cfg.min_pixels = 4096;
        cfg.max_pixels = 25165824;
        std::vector<const uint8_t*> data;
        std::vector<size_t> sizes;
        for (const auto& frame : storage) {
          data.push_back(frame.data());
          sizes.push_back(frame.size());
        }
        vision::ProcessedVideo result;
        if (!vision::ProcessVideo(data, sizes, cfg, &result, &error)) return 1;
        pixels = std::move(result.pixel_values);
        gt = result.grid_t;
        gh = result.grid_h;
        gw = result.grid_w;
      }
      std::ofstream out(input.GetString("output"), std::ios::binary);
      out.write(reinterpret_cast<const char*>(pixels.data()),
                pixels.size() * sizeof(float));
      if (!out) return 2;
      std::cout << "{\"ok\":true,\"grid\":[" << gt << "," << gh << "," << gw
                << "]}\n";
    } else {
      return 2;
    }
  }
}
