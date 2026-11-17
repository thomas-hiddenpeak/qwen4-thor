// Explicit checkpoint qualification; no model assets in the public registry.
#include "q4t/audio/aligner_config.h"
#include "q4t/test.h"

#include <array>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

Q4T_TEST(aligner_actual_checkpoint_matches_metadata_and_all_tensor_ranges) {
  const char* root = std::getenv("Q4T_ALIGNER_ASSETS");
  Q4T_CHECK(root && *root);
  const auto directory = std::filesystem::path(root);
  using Validator = q4t::Status (*)(const q4t::io::Json&);
  const std::array<std::pair<const char*, Validator>, 3> files{
      {{"config.json", q4t::audio::ValidateAlignerConfig},
       {"preprocessor_config.json",
        q4t::audio::ValidateAlignerPreprocessorConfig},
       {"generation_config.json",
        q4t::audio::ValidateAlignerGenerationConfig}}};
  for (const auto& [name, validate] : files) {
    std::ifstream file(directory / name, std::ios::binary);
    std::ostringstream bytes;
    bytes << file.rdbuf();
    Q4T_CHECK(file && bytes.str().size() < 65536);
    q4t::io::Json config;
    Q4T_CHECK(q4t::io::ParseJson(bytes.str(), &config, false,
                                 {.reject_duplicate_keys = true}));
    Q4T_CHECK(validate(config));
  }
  const auto path = directory / "model.safetensors";
  Q4T_CHECK(std::filesystem::file_size(path) == 1835544544);
  std::ifstream checkpoint(path, std::ios::binary);
  std::array<unsigned char, 8> prefix{};
  checkpoint.read(reinterpret_cast<char*>(prefix.data()), prefix.size());
  Q4T_CHECK(checkpoint);
  uint64_t length = 0;
  for (size_t i = 0; i < prefix.size(); ++i)
    length |= static_cast<uint64_t>(prefix[i]) << (i * 8);
  Q4T_CHECK(length == 86744);
  std::string bytes(length, '\0');
  checkpoint.read(bytes.data(), bytes.size());
  Q4T_CHECK(checkpoint);
  q4t::io::Json header;
  Q4T_CHECK(q4t::io::ParseJson(bytes, &header, false,
                               {.reject_duplicate_keys = true}));
  Q4T_CHECK(q4t::audio::ValidateAlignerTensorHeader(
      header, std::filesystem::file_size(path) - 8 - length));
  return true;
}
