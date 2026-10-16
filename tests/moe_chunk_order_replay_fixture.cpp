// Host adapter for offline replay contracts, using the SAME online selector.
// Text stdin: experts chunks slots; then, for every ORIGINAL chunk, count
// followed by that many expert IDs; then one live resident-slot snapshot per
// selection (slots integers, -1 denotes empty). Emit the selected original
// chunk index immediately so an offline residency replay can advance before
// supplying the next snapshot. This fixture performs no GPU or model I/O.
#include "q4t/model/moe_chunk_order.h"

#include <iostream>
#include <string>
#include <vector>

int main() {
  int experts = 0, chunks = 0, slots = 0;
  if (!(std::cin >> experts >> chunks >> slots) || experts <= 0 ||
      experts > 65536 || chunks < 0 || chunks > 8192 || slots < 0 ||
      slots > experts) return 2;
  q4t::model::MoEChunkOrderSelector selector;
  selector.Init(static_cast<size_t>(chunks), experts);
  for (int chunk = 0; chunk < chunks; ++chunk) {
    int count = 0;
    if (!(std::cin >> count) || count < 0 || count > experts) return 2;
    for (int i = 0; i < count; ++i) {
      int expert = -1;
      if (!(std::cin >> expert) || expert < 0 || expert >= experts) return 2;
      selector.AddExpert(static_cast<size_t>(chunk), expert);
    }
  }
  std::vector<int> resident(slots);
  for (int step = 0; step < chunks; ++step) {
    for (int& expert : resident) {
      if (!(std::cin >> expert) || expert < -1 || expert >= experts) return 2;
    }
    const size_t selected = selector.SelectNext(
        slots, [&](int slot) { return resident[slot]; });
    if (selected >= static_cast<size_t>(chunks)) return 3;
    std::cout << selected << '\n' << std::flush;
  }
  std::string trailing;
  if (std::cin >> trailing) return 2;
  return std::cin.eof() ? 0 : 2;
}
