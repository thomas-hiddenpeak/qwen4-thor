// Exercise new fixed inputs in production host code; never start CUDA/HTTP.
#include "q4t/server/chat_contract.h"
#include "q4t/server/media_admission.h"
#include "q4t/vision/processor.h"

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace {
void Check(bool ok, const char* why) {
  if (!ok) {
    std::fprintf(stderr, "FAIL %s\n", why);
    std::exit(1);
  }
}
std::string Read(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  Check(static_cast<bool>(input), "fixture read");
  return {std::istreambuf_iterator<char>(input), {}};
}
}  // namespace

int main(int argc, char** argv) {
  Check(argc == 2, "fixture directory required");
  using namespace q4t::server;
  using namespace q4t::vision;
  q4t::io::Json rows;
  Check(q4t::io::ParseJson(Read(std::string(argv[1]) + "/cases.json"), &rows)
            .ok(),
        "fixture JSON");
  size_t cases = 0, decoder_peak = 0;
  for (const auto& row : rows.array) {
    const auto& body = *row.Find("body");
    std::string param, code = "invalid_request", prompt;
    std::vector<VisionItem> items;
    auto status =
        ValidateChatContract(body, "qwen3.8-flash-next", true, &param);
    if (status)
      status = PrepareChatPrompt(body, {}, &prompt, &items, &param, &code);
    if (status) {
      std::vector<int32_t> tokens(100, 9);
      for (const auto& item : items)
        tokens.push_back(item.kind == VisionItem::kImage ? 1 : 2);
      ProcessorConfig image, video;
      video.min_pixels = 4096;
      video.max_pixels = 25165824;
      ChatPlanConfig config;
      config.max_prefill = 2048;
      config.max_len = 8192;
      MediaPlan plan;
      ChatPlanError error;
      // Hypothetical CPU device limit. GPU acceptance separately queries the
      // actual function and checks the discovery value against that result.
      status = PlanChatMedia(items, tokens, 1, 2, image, video, {47104}, config,
                             &plan, &error);
      code = error.code;
      param = error.param;
      if (status) {
        Check(row.GetString("name") == "decoder-quota" && items.size() == 1,
              "only quota fixture may reach decoding");
        ProcessedImage output;
        ProcessorResult result;
        std::string detail;
        const auto& bytes = items[0].frames[0];
        const bool ok = ProcessImage(
            reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size(), image,
            &output, &detail, {.admission = &plan.items[0], .result = &result});
        Check(!ok && output.pixel_values.empty(), "quota published output");
        decoder_peak = result.decoder_peak_bytes;
        Check(decoder_peak > 64*1024*1024 && decoder_peak <= kDecoderOwnedBytes,
              "quota did not bound actual expansion");
        code = result.code;
        param = MediaInputParam(items[0]);
        status = q4t::Status::Fail(code);
      }
    }
    Check(!status && code == row.GetString("code") &&
              param == row.GetString("param"),
          row.GetString("name").c_str());
    ++cases;
    std::printf("PASS %s code=%s param=%s\n", row.GetString("name").c_str(),
                code.c_str(), param.c_str());
  }
  const auto valid = Read(std::string(argv[1]) + "/red.png");
  ProcessedImage recovered;
  std::string error;
  Check(ProcessImage(reinterpret_cast<const uint8_t*>(valid.data()),
                       valid.size(), {}, &recovered, &error) &&
            recovered.pixel_values.size() == 256*1536,
        "valid processing after quota denial failed");
  Check(cases == 11, "fixed case count changed");
  std::printf("PASS cases=11 decoder_peak=%zu logical_quota=%zu recovery=1 "
              "gpu_started=false\n", decoder_peak, kDecoderOwnedBytes);
}
