// Byte-level oracle is independent of the manifest and the model's math.
#define main OriginalContract
#include "base_storage_contract.cpp"
#undef main

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  if (OriginalContract(argc, argv)) return 1;
  using namespace q4t;
  using namespace q4t::server;
  int checks = 0;
  auto require = [&](bool value, const char* why) {
    ++checks;
    if (!value) throw std::runtime_error(why);
  };
  try {
    SessionCache cache;
    require(cache.Open(argv[1], 8192, SessionCache::kDiskBudget, true).ok(),
            "incremental open");
    const size_t block = SessionCache::kStagingBytes;
    std::vector<std::vector<uint8_t>> data{
        std::vector<uint8_t>(2 * block + 71), std::vector<uint8_t>(5001),
        std::vector<uint8_t>(block + 31)};
    for (size_t r = 0; r < data.size(); ++r)
      for (size_t i = 0; i < data[r].size(); ++i)
        data[r][i] = i * 29 + i / 7 + r * 31;
    std::vector<int32_t> prefix(8193, 248001);
    auto ranges = [&] {
      std::vector<SessionRange> result;
      for (auto& d : data) result.push_back({d.data(), d.size()});
      return result;
    };
    const auto copy = [](void* host, void* state, size_t bytes, bool restore) {
      std::memcpy(restore ? state : host, restore ? host : state, bytes);
      return Status();
    };
    const auto no_cancel = [] { return false; };
    int32_t d0 = 200001;
    auto save = [&] {
      SessionSaveTiming timing("cpu-incremental", "private");
      const auto s = cache.Save("A", prefix, ranges(), copy, no_cancel, d0,
                                  &timing);
      timing.Report(s.ok());
      require(s.ok(), "save");
      require(timing.incremental,
              "independent path");
      require(timing.scanned_bytes == data[0].size() + data[1].size() +
                                         data[2].size(), "all bytes scanned");
      require(timing.disk_bytes + timing.reused_bytes >= timing.scanned_bytes,
              "written plus reused closure");
      require(timing.file_span <= 8192 * block &&
                  cache.Bytes() >= timing.allocated_bytes,
              "bounded sparse span and actual disk accounting");
      return timing;
    };
    auto restore = [&] {
      cache.Release("A", true);
      require(cache.Acquire("A"), "new lease");
      require(cache.Match("A", prefix) == int(prefix.size()) - 1,
              "new prefix published");
      const auto expected = data;
      for (auto& d : data) std::fill(d.begin(), d.end(), 0xa5);
      int32_t got_d0 = -1, pending = -1;
      require(cache.Restore("A", ranges(), copy, no_cancel, &got_d0,
                            &pending).ok(), "restore updated manifest");
      require(data == expected && got_d0 == d0 && pending == prefix.back(),
              "independent byte and scalar oracle");
    };
    require(cache.Acquire("A"), "initial lease");
    require(!cache.Acquire("A"), "same key excluded");
    auto first = save();
    require(first.reused_bytes == 0, "cold full write");
    restore();
    auto same = save();
    require(same.disk_bytes == 0 && same.reused_bytes == first.disk_bytes,
            "identical complete snapshot zero writes");
    restore();
    // Grow across a chunk boundary in the first range; later range indexes
    // shift in the manifest while their physical slots must remain correct.
    data[0].resize(3 * block + 701, 0x57);
    data[1][17] ^= 0x7f;  // Fixed recurrent-like range.
    data[2].back() ^= 1;  // Shifted draft's mutable tail.
    prefix.back() = 17;
    prefix.resize(prefix.size() + 27, 234567);
    ++d0;
    auto grown = save();
    require(grown.reused_bytes == 3 * block, "only exact full blocks reused");
    restore();
    for (int turn = 0; turn < 3; ++turn) {
      data[0].resize(data[0].size() + 37, 0x39 + turn);
      ++prefix.back();
      prefix.push_back(81234 + turn);
      ++d0;
      auto next = save();
      require(next.reused_bytes >= 4 * block, "bounded repeated growth");
      restore();
    }
    // New lease without Restore must not inherit reuse eligibility.
    cache.Release("A", true);
    require(cache.Acquire("A"), "lease without restore");
    require(save().reused_bytes == 0, "no prior lease eligibility leak");
    restore();
    prefix[3] ^= 1;
    require(save().reused_bytes == 0, "different history rebuilds");
    restore();
    data[0].resize(block + 7);
    require(save().reused_bytes == 0, "shrinking range rebuilds");
    restore();
    data[0][0] ^= 1;
    fault = 3;
    require(!cache.Save("A", prefix, ranges(), copy, no_cancel, d0),
            "incremental short write");
    fault = 0;
    require(cache.Bytes() == 0 && !cache.Match("A", prefix),
            "partial update cannot publish or leak disk charge");
    save();
    restore();
    int copies = 0;
    auto fail_copy = [&](void* h, void* s, size_t n, bool r) {
      return ++copies == 2 ? Status::Fail("copy injection") : copy(h, s, n, r);
    };
    data[0][0] ^= 1;
    require(!cache.Save("A", prefix, ranges(), fail_copy, no_cancel, d0),
            "failure after a real changed write");
    require(cache.Bytes() == 0, "copy failure closes changed file");
    save();
    restore();
    int polls = 0;
    require(!cache.Save("A", prefix, ranges(), copy,
                        [&] { return ++polls == 2; }, d0), "cancel scan");
    require(cache.Bytes() == 0, "cancel removes old manifest");
    save();
    cache.Release("A", false);
    require(cache.Bytes() == 0, "failed HTTP remains unpublished");
    require(cache.Acquire("A"), "fault recovery lease");
    save();
    restore();
    SessionCacheTestPeer::Corrupt(cache, "A");
    require(!cache.Restore("A", ranges(), copy, no_cancel, nullptr),
            "reused file still checksum checked");
    require(cache.Bytes() == 0, "bad reused file evicted");
    auto too_many = std::vector<SessionRange>(1025, {data[1].data(), 1});
    require(!cache.Save("A", prefix, too_many, copy, no_cancel, d0),
            "manifest RAM cap");
    cache.Release("A", false);
    std::printf("PASS incremental_checks=%d staging_bytes=%zu\n", checks,
                SessionCache::kStagingBytes);
    return 0;
  } catch (const std::exception& e) {
    fault = 0;
    std::fprintf(stderr, "FAIL incremental_checks=%d error=%s\n", checks,
                  e.what());
    return 1;
  }
}
