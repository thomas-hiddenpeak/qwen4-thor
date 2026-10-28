// Real direct-IO storage, publication, longest-prefix and scope contracts.
#define main SessionStorageContract
#include "../session_cache/storage_contract.cpp"
#undef main
#include "q4t/server/server_options.h"

int main(int argc, char** argv) {
  if (SessionStorageContract(argc, argv)) return 1;
  using namespace q4t;
  using namespace q4t::server;
  int checks = 0;
  auto require = [&](bool ok, const char* message) {
    ++checks;
    if (!ok) throw std::runtime_error(message);
  };
  try {
    ServerOptions options;
    options.model_dir = "read-only-placeholder";
    options.max_len = 208896;
    options.max_seq = 4;
    options.seq_capacities = {208896, 65536, 65536, 65536};
    options.prefix_cache_dir = argv[1];
    require(ValidateServerOptions(options).ok(), "default prefix options");
    for (const auto& values : std::vector<std::vector<int>>{
             {}, {8192, 1024}, {1024, 1024}, {1023}, {131073},
             {1024, 4096, 8192, 32768, 65536}}) {
      auto bad = options;
      bad.prefix_cache_checkpoints = values;
      require(!ValidateServerOptions(bad), "invalid checkpoint bounds");
    }
    const std::string_view args[] = {"--prefix-cache-checkpoints", "1024,8193"};
    require(ParseServerOptions(args, &options).ok() &&
            options.prefix_cache_checkpoints == std::vector<int>({1024, 8193}),
            "custom unaligned checkpoints");
    options.no_mtp = true;
    require(!ValidateServerOptions(options), "plain shared unsupported");
    options.no_mtp = false;
    options.allow_media = true;
    require(!ValidateServerOptions(options), "media shared unsupported");
    SessionCache c;
    require(c.Open(argv[1], 1024, 32768).ok(), "shared limits");
    std::vector<int32_t> prompt(8194, 12345);
    std::vector<uint8_t> data(4100, 37);
    std::vector<SessionRange> ranges{{data.data(), data.size()}};
    const auto copy = [](void* host, void* state, size_t n, bool restore) {
      std::memcpy(restore ? state : host, restore ? host : state, n);
      return Status();
    };
    const auto active = [] { return false; };
    auto save = [&](const std::string& key, int length, bool success) {
      require(c.AcquireNew(key), "new immutable key lease");
      require(c.Save(key, {prompt.data(), size_t(length + 1)}, ranges,
                     copy, active, -1).ok(), "shared save");
      c.Release(key, success);
    };
    save("scope:a", 1024, true);
    save("scope:b", 8192, true);
    save("other:c", 8193, true);
    require(!c.AcquireNew("scope:b"), "published entry cannot be overwritten");
    std::string key;
    require(c.MatchShared("scope", prompt, &key) == 8192 && key == "scope:b",
            "longest exact prefix and scope isolation");
    std::fill(data.begin(), data.end(), 0);
    int32_t d0 = 7;
    require(c.Restore(key, ranges, copy, active, &d0).ok(), "shared restore");
    require(d0 == -1 && data == std::vector<uint8_t>(4100, 37), "payload exact");
    require(!c.AcquireNew(key), "reader prevents replacement");
    c.Release(key, true);
    auto changed = prompt;
    changed[4000]++;
    require(c.MatchShared("scope", changed, &key) == 1024, "shorter fallback");
    c.Release(key, true);
    changed[0]++;
    require(c.MatchShared("scope", changed, &key) == 0, "hash cannot bypass tokens");
    require(c.MatchShared("unknown", prompt, &key) == 0, "unknown scope miss");
    require(c.MatchShared("scope", {prompt.data(), 1024}, &key) == 0,
            "at least one uncached token required");
    require(c.AcquireNew("scope:pending"), "pending lease");
    require(c.Save("scope:pending", prompt, ranges, copy, active, -1).ok(),
            "pending save");
    require(c.MatchShared("scope", prompt, &key) == 8192, "unpublished excluded");
    c.Release(key, true);
    c.Release("scope:pending", false);
    require(c.Bytes() == 24576, "cancel frees shared disk");
    SessionCacheTestPeer::Expire(c, "scope:b");
    require(c.MatchShared("scope", prompt, &key) == 1024, "TTL fallback");
    c.Release(key, true);
    save("scope:d", 8193, true);
    SessionCacheTestPeer::Corrupt(c, "scope:d");
    require(c.MatchShared("scope", prompt, &key) == 8193, "corrupt selected");
    require(!c.Restore(key, ranges, copy, active, &d0), "corrupt rejected");
    c.Release(key, true);
    require(c.MatchShared("scope", prompt, &key) == 1024, "corrupt evicted");
    c.Release(key, true);
    ranges[0].bytes = 32769;
    require(c.AcquireNew("scope:oversize"), "oversize acquire");
    require(!c.Save("scope:oversize", prompt, ranges, copy, active, -1),
            "disk bound before accessing source");
    c.Release("scope:oversize", false);
    std::printf("PASS shared_checks=%d disk_budget=32768\n", checks);
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "FAIL shared_checks=%d error=%s\n", checks, e.what());
    return 1;
  }
}
