// Reproduce moe.cu's libstdc++ token ordering, including equal-key ties.
// stdin: little-endian uint32 rows, top_k, then uint16 row-major expert IDs.
// stdout: rows little-endian uint32 token indices. No model execution.
#include <algorithm>
#include <bit>
#include <cstdint>
#include <iostream>
#include <numeric>
#include <vector>

int main() {
  static_assert(std::endian::native == std::endian::little);
  uint32_t rows = 0;
  uint32_t top_k = 0;
  std::cin.read(reinterpret_cast<char*>(&rows), sizeof(rows));
  std::cin.read(reinterpret_cast<char*>(&top_k), sizeof(top_k));
  if (!std::cin || rows == 0 || rows > 8192 || top_k != 10) return 1;
  std::vector<uint16_t> ids(static_cast<size_t>(rows) * top_k);
  std::cin.read(reinterpret_cast<char*>(ids.data()),
                static_cast<std::streamsize>(ids.size() * sizeof(uint16_t)));
  if (!std::cin || std::cin.peek() != std::char_traits<char>::eof()) return 1;
  std::vector<int32_t> keys(ids.begin(), ids.end());
  for (uint32_t t = 0; t < rows; ++t) {
    int32_t* key = keys.data() + static_cast<size_t>(t) * top_k;
    std::sort(key, key + top_k);
    if (key[top_k - 1] >= 512 ||
        std::adjacent_find(key, key + top_k) != key + top_k) {
      return 1;
    }
  }
  std::vector<int32_t> order(rows);
  std::iota(order.begin(), order.end(), 0);
  std::sort(order.begin(), order.end(), [&](int a, int b) {
    const int32_t* ka = keys.data() + static_cast<size_t>(a) * top_k;
    const int32_t* kb = keys.data() + static_cast<size_t>(b) * top_k;
    return std::lexicographical_compare(ka, ka + top_k, kb, kb + top_k);
  });
  std::cout.write(reinterpret_cast<const char*>(order.data()),
                  static_cast<std::streamsize>(order.size() * sizeof(int32_t)));
  return std::cout ? 0 : 1;
}
