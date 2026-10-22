// Host-only public-loader adapter used after the HTTP gate.
#include <cstdio>
#include "q4t/mtp/subset_head.h"

int main(int argc, char** argv) {
  if (argc != 3) return 2;
  q4t::mtp::SubsetHeadData data;
  const auto status = q4t::mtp::ReadSubsetHead(argv[1], argv[2], 248320, 2560,
                                             &data);
  if (!status.ok()) {
    std::fprintf(stderr, "%s\n", status.message().c_str());
    return 1;
  }
  std::printf("PASS rows=%zu weights=%zu\n", data.ids.size(), data.weights.size());
  return 0;
}
