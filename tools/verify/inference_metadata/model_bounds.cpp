// Exercise real entry validation without loading weights or issuing GPU work.
#include <iostream>
#include <string_view>

#include "q4t/model/model.h"

int main() {
  q4t::model::Model model;
  model.cfg.max_seq = 4;
  model.cfg.max_prefill = 2;
  model.cfg.max_len = 8;
  int passed = 0;
  auto check = [&](int rows, const int* positions, const int* slots,
                   std::string_view expected) {
    const auto status = q4t::model::ModelDecodeBatchMulti(
        model, nullptr, positions, slots, nullptr, rows, nullptr, nullptr);
    if (status.ok() || status.message() != expected) {
      std::cerr << "B=" << rows << ": " << status.message() << '\n';
      return false;
    }
    ++passed;
    return true;
  };
  if (!check(0, nullptr, nullptr, "ModelDecodeBatchMulti: B must be > 0") ||
      !check(5, nullptr, nullptr,
             "ModelDecodeBatchMulti: B exceeds max_seq") ||
      !check(3, nullptr, nullptr,
             "ModelDecodeBatchMulti: B exceeds max_prefill") ||
      !check(4, nullptr, nullptr,
             "ModelDecodeBatchMulti: B exceeds max_prefill")) {
    return 1;
  }
  const int positions[] = {0, 7};
  const int slots[] = {0, 3};
  if (!check(1, positions, slots,
             "ModelDecodeBatchMulti: upload owner missing") ||
      !check(2, positions, slots,
             "ModelDecodeBatchMulti: upload owner missing")) {
    return 1;
  }
  std::cout << "PASS " << passed << " entry checks; no model/GPU work\n";
}
