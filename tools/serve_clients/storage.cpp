// Real SSD namespace contracts; host copy callback, no CUDA/model execution.
#include "q4t/server/client_identity.h"
#include "q4t/server/session_cache.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <stdexcept>

int main(int argc, char** argv) {
  using namespace q4t::server;
  int checks = 0;
  const auto require = [&](bool condition, const char* message) {
    ++checks;
    if (!condition) throw std::runtime_error(message);
  };
  try {
    require(argc == 2, "explicit new storage directory required");
    require(std::filesystem::create_directory(argv[1]), "preserve old storage");
    std::filesystem::permissions(argv[1], std::filesystem::perms::owner_all);
    SessionCache sessions, shared;
    require(sessions.Open(argv[1], 1024, 65536).ok(), "session storage open");
    require(shared.Open(argv[1], 1024, 65536).ok(), "shared storage open");
    std::vector<int32_t> tokens(1025, 77);
    std::vector<uint8_t> state(4100, 31);
    const std::vector<SessionRange> ranges{{state.data(), state.size()}};
    const auto copy = [](void* host, void* device, size_t bytes, bool restore) {
      std::memcpy(restore ? device : host, restore ? host : device, bytes);
      return q4t::Status();
    };
    const auto active = [] { return false; };
    const auto a = ClientScopedKey("alpha", std::string(64, 'b'));
    const auto b = ClientScopedKey("beta", std::string(64, 'b'));
    require(sessions.Acquire(a) && sessions.Acquire(b), "independent leases");
    require(!sessions.Acquire(a), "same client lease conflict");
    require(sessions.Save(a, tokens, ranges, copy, active, 123).ok(), "save A");
    sessions.Release(a, true);
    require(sessions.Match(b, tokens) == 0, "B cannot match A session");
    std::fill(state.begin(), state.end(), 42);
    require(sessions.Save(b, tokens, ranges, copy, active, 456).ok(), "save B");
    sessions.Release(b, true);
    for (const auto& key : {a, b}) {
      require(sessions.Acquire(key), "reacquire published session");
      require(sessions.Match(key, tokens) == 1024, "own session hit");
      std::fill(state.begin(), state.end(), 0);
      int32_t draft = -1;
      require(sessions.Restore(key, ranges, copy, active, &draft).ok(),
              "restore");
      require(state == std::vector<uint8_t>(4100, key == a ? 31 : 42) &&
                  draft == (key == a ? 123 : 456),
              "exact isolated payload");
      sessions.Release(key, true);
    }
    const auto as = a + ":snapshot";
    require(shared.AcquireNew(as), "shared new lease");
    require(shared.Save(as, tokens, ranges, copy, active, -1).ok(),
            "shared save");
    shared.Release(as, true);
    std::string selected;
    require(shared.MatchShared(b, tokens, &selected) == 0, "cross client miss");
    require(shared.MatchShared(std::string(64, 'b'), tokens, &selected) == 0,
            "legacy cannot match scoped prefix");
    require(shared.MatchShared(a, tokens, &selected) == 1024 && selected == as,
            "same client shared hit");
    shared.Release(selected, true);
    auto changed = tokens;
    changed[0]++;
    require(shared.MatchShared(a, changed, &selected) == 0,
            "exact prefix check");
    const auto bs = b + ":snapshot";
    require(shared.AcquireNew(bs), "same external scope independent writer");
    require(shared.Save(bs, tokens, ranges, copy, active, -1).ok(), "B save");
    shared.Release(bs, false);
    require(shared.MatchShared(b, tokens, &selected) == 0,
            "failed B unpublished");
    require(shared.MatchShared(a, tokens, &selected) == 1024,
            "A survives B failure");
    shared.Release(selected, true);
    require(sessions.Bytes() == 16384 && shared.Bytes() == 8192,
            "global disk charges include both clients");
    std::printf("PASS client_storage checks=%d gpu=0\n", checks);
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "FAIL client_storage checks=%d error=%s\n", checks,
                 error.what());
    return 1;
  }
}
