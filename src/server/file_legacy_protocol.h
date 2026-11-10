#pragma once

#include <cstdint>

namespace q4t::server::legacy_detail {
inline constexpr uint64_t kMagic = 0x5134544c45473031;
enum class Operation : uint64_t { kConvert = 1, kClean = 2 };
enum class Ready : uint64_t {
  kReady = 1,
  kStorage = 2,
  kSandbox = 3,
  kClean = 4
};
enum class Format : uint64_t {
  kText = 1,
  kSheet = 2,
  kSlides = 3,
  kDrawing = 4
};
enum class Failure : uint64_t {
  kNone,
  kDependency,
  kInvalid,
  kCapacity,
  kStorage
};
struct Request {
  uint64_t magic = kMagic;
  Operation operation = Operation::kConvert;
  uint64_t source_bytes = 0, output_bytes = 0, root_bytes = 0;
  char token[32]{};
};
struct Result {
  uint64_t magic = kMagic;
  Failure failure = Failure::kNone;
  Format format = Format::kText;
  uint64_t bytes = 0;
};
}  // namespace q4t::server::legacy_detail
