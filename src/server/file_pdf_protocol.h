#pragma once

#include <cstdint>

namespace q4t::server::pdf_detail {
inline constexpr uint64_t kMagic = 0x5134545044463031;
enum class Operation : uint64_t { kInfo = 1, kText = 2, kPage = 3 };
enum class Ready : uint64_t {
  kReady = 1,
  kDependency = 2,
  kSandbox = 3,
  kInput = 4,
};
struct Request {
  uint64_t magic = kMagic;
  Operation operation = Operation::kInfo;
  uint64_t bytes = 0, page = 0, edge = 0;
};
}  // namespace q4t::server::pdf_detail
