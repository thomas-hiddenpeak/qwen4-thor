#include "q4t/model/moe_replay_io.h"

#include <fstream>
#include <limits>

#include "q4t/trace/sha256.h"

namespace q4t::model {
namespace {
constexpr uint64_t kMaxBytes = 16 * 1024 * 1024;
constexpr uint64_t kMagic = 0x3159504c52345451;  // QT4RLPY1, LE.

void Put(std::vector<uint8_t>& bytes, uint64_t value) {
  for (int i = 0; i < 8; ++i) bytes.push_back((value >> (i * 8)) & 255);
}
}  // namespace

Status WriteMoEReplay(const std::string& path, const MoEReplayRecord& record) {
  if (!record.ready()) return Status::Fail("replay file: unprepared record");
  const auto& id = record.source();
  std::vector<uint8_t> bytes;
  Put(bytes, kMagic);
  for (const auto* digest : {&id.model, &id.weights, &id.hidden, &id.binary}) {
    bytes.insert(bytes.end(), digest->begin(), digest->end());
  }
  // Only main is supported; encode explicit role rather than inferring it.
  Put(bytes, 1);
  Put(bytes, id.layer);
  Put(bytes, id.position);
  Put(bytes, id.rows);
  Put(bytes, id.experts);
  Put(bytes, id.top_k);
  Put(bytes, id.tokens.size());
  for (auto token : id.tokens) Put(bytes, token);
  Put(bytes, record.ids().size());
  for (auto expert : record.ids()) Put(bytes, expert);
  if (bytes.size() + 32 > kMaxBytes) {
    return Status::Fail("replay file: too large");
  }
  const auto checksum = trace::Sha256(bytes);
  bytes.insert(bytes.end(), checksum.begin(), checksum.end());
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  output.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
  output.close();
  return output ? Status{} : Status::Fail("replay file: write failed");
}

Status ReadMoEReplay(const std::string& path, MoEReplayRecord* record) {
  if (!record) return Status::Fail("replay file: null output");
  *record = {};
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input) return Status::Fail("replay file: open failed");
  const auto length = input.tellg();
  if (length < 32 || length > static_cast<std::streamoff>(kMaxBytes)) {
    return Status::Fail("replay file: size invalid");
  }
  std::vector<uint8_t> bytes(static_cast<size_t>(length));
  input.seekg(0);
  input.read(reinterpret_cast<char*>(bytes.data()), bytes.size());
  if (!input || input.peek() != std::char_traits<char>::eof()) {
    return Status::Fail("replay file: read failed or grew");
  }
  const size_t end = bytes.size() - 32;
  const auto checksum = trace::Sha256({bytes.data(), end});
  if (!std::equal(checksum.begin(), checksum.end(), bytes.begin() + end)) {
    return Status::Fail("replay file: checksum mismatch");
  }
  size_t cursor = 0;
  bool valid = true;
  const auto get = [&]() -> uint64_t {
    if (cursor > end || end - cursor < 8) {
      valid = false;
      return 0;
    }
    uint64_t value = 0;
    for (int i = 0; i < 8; ++i) value |= uint64_t{bytes[cursor++]} << (8 * i);
    return value;
  };
  if (get() != kMagic) return Status::Fail("replay file: unsupported version");
  MoEReplayIdentity id;
  for (auto* digest : {&id.model, &id.weights, &id.hidden, &id.binary}) {
    if (end - cursor < digest->size()) {
      return Status::Fail("replay file: truncated identity");
    }
    std::copy_n(bytes.begin() + cursor, digest->size(), digest->begin());
    cursor += digest->size();
  }
  if (get() != 1) return Status::Fail("replay file: unsupported role");
  const auto integer = [&]() -> int {
    const uint64_t value = get();
    if (value > static_cast<uint64_t>(std::numeric_limits<int>::max())) {
      valid = false;
      return 0;
    }
    return static_cast<int>(value);
  };
  id.layer = integer();
  id.position = get();
  id.rows = integer();
  id.experts = integer();
  id.top_k = integer();
  const auto array = [&](std::vector<int32_t>& values) {
    const uint64_t count = get();
    if (!valid || count > (end - cursor) / 8) {
      valid = false;
      return;
    }
    values.resize(count);
    for (auto& value : values) value = integer();
  };
  array(id.tokens);
  std::vector<int32_t> ids;
  array(ids);
  if (!valid || cursor != end) {
    return Status::Fail("replay file: truncated or extra payload");
  }
  return record->Prepare(id, id, ids);
}

}  // namespace q4t::model
