#include "q4t/model/moe_replay_io.h"
#include "q4t/test.h"
#include "q4t/trace/sha256.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>

Q4T_TEST(moe_replay_file_integrity) {
  using namespace q4t::model;
  const char* directory = std::getenv("Q4T_REPLAY_TEST_OUTPUT");
  Q4T_CHECK(directory != nullptr);
  std::filesystem::create_directories(directory);
  const std::string path = std::string(directory) + "/record.bin";
  MoEReplayIdentity id;
  id.model[0] = 1;
  id.weights[0] = 2;
  id.hidden[0] = 3;
  id.binary[0] = 4;
  id.layer = 2;
  id.rows = 1;
  id.experts = 512;
  id.top_k = 2;
  id.tokens = {42};
  MoEReplayRecord record, restored;
  const std::vector<int32_t> ids{511, 0};
  Q4T_CHECK(record.Prepare(id, id, ids).ok());
  Q4T_CHECK(WriteMoEReplay(path, record).ok());
  Q4T_CHECK(ReadMoEReplay(path, &restored).ok());
  Q4T_CHECK(restored.source().binary == id.binary);
  Q4T_CHECK(restored.source().tokens == id.tokens);
  Q4T_CHECK(std::equal(ids.begin(), ids.end(), restored.ids().begin()));
  std::ifstream input(path, std::ios::binary);
  const std::vector<uint8_t> original{std::istreambuf_iterator<char>(input),
                                     std::istreambuf_iterator<char>()};
  const auto write = [&](const std::vector<uint8_t>& bytes) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
  };
  for (size_t cut = 0; cut < original.size(); ++cut) {
    write({original.begin(), original.begin() + cut});
    Q4T_CHECK(!ReadMoEReplay(path, &restored).ok());
    Q4T_CHECK(!restored.ready());
  }
  auto bytes = original;
  bytes.push_back(0);
  write(bytes);
  Q4T_CHECK(!ReadMoEReplay(path, &restored).ok());
  bytes = original;
  bytes[10] ^= 1;
  write(bytes);
  Q4T_CHECK(!ReadMoEReplay(path, &restored).ok());
  // Recompute checksums: parsing must independently reject invalid structure.
  for (size_t offset : {size_t{0}, size_t{136}, size_t{144}, size_t{184}}) {
    bytes = original;
    for (size_t i = 0; i < 8; ++i) bytes[offset + i] = 255;
    auto checksum = q4t::trace::Sha256({bytes.data(), bytes.size() - 32});
    std::copy(checksum.begin(), checksum.end(), bytes.end() - 32);
    write(bytes);
    Q4T_CHECK(!ReadMoEReplay(path, &restored).ok());
  }
  bytes = original;
  bytes.insert(bytes.end() - 32, 8, 0);
  auto checksum = q4t::trace::Sha256({bytes.data(), bytes.size() - 32});
  std::copy(checksum.begin(), checksum.end(), bytes.end() - 32);
  write(bytes);
  Q4T_CHECK(!ReadMoEReplay(path, &restored).ok());
  write(original);
  Q4T_CHECK(ReadMoEReplay(path, &restored).ok());
  return true;
}
