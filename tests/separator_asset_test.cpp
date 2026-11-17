// Explicit actual-checkpoint verification; no weights in public test fixtures.
#include "q4t/audio/separator_config.h"
#include "q4t/test.h"
#include "q4t/trace/sha256.h"

#include <array>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

Q4T_TEST(separator_actual_fixed_checkpoint_and_config_verified_without_cuda) {
  const char* root = std::getenv("Q4T_SEPARATOR_ASSETS");
  Q4T_CHECK(root && *root);
  const auto directory = std::filesystem::path(root);
  const auto path = directory / "model.safetensors";
  using q4t::audio::SeparatorConfig;
  Q4T_CHECK(std::filesystem::file_size(path) == SeparatorConfig::kFileBytes);
  q4t::trace::Digest digest;
  Q4T_CHECK(q4t::trace::HashFile(path, &digest));
  Q4T_CHECK(q4t::trace::HexDigest(digest) == SeparatorConfig::kWeightSha256);
  std::ifstream checkpoint(path, std::ios::binary);
  std::array<unsigned char, 8> prefix{};
  checkpoint.read(reinterpret_cast<char*>(prefix.data()), prefix.size());
  Q4T_CHECK(checkpoint);
  uint64_t length = 0;
  for (size_t i = 0; i < prefix.size(); ++i)
    length |= static_cast<uint64_t>(prefix[i]) << (i * 8);
  Q4T_CHECK(length == SeparatorConfig::kHeaderBytes);
  std::string bytes(length, '\0');
  checkpoint.read(bytes.data(), bytes.size());
  Q4T_CHECK(checkpoint);
  q4t::io::Json header;
  Q4T_CHECK(q4t::io::ParseJson(bytes, &header, false,
                               {.reject_duplicate_keys = true}));
  q4t::audio::SeparatorTensorLayout layout;
  Q4T_CHECK(q4t::audio::ParseSeparatorTensorLayout(
      header, SeparatorConfig::kWeightBytes, &layout));
  std::ifstream file(directory / "config.json", std::ios::binary);
  std::ostringstream text;
  text << file.rdbuf();
  Q4T_CHECK(file && text.str().size() < 8192);
  q4t::io::Json config;
  Q4T_CHECK(q4t::io::ParseJson(text.str(), &config, false,
                               {.reject_duplicate_keys = true}));
  Q4T_CHECK(q4t::audio::ValidateSeparatorConfig(config));
  return true;
}
