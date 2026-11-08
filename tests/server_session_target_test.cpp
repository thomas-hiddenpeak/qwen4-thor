// CPU-only state layout and real SSD storage. No model load or CUDA calls.
#include "q4t/server/session_state.h"
#include "q4t/test.h"

#include <sys/stat.h>

#include <algorithm>
#include <cstring>
#include <filesystem>
#include <stdexcept>

namespace {
using namespace q4t;
using namespace q4t::server;

struct Fixture {
  model::Model model;
  std::vector<float> ssm = std::vector<float>(24, 1.25f);
  std::vector<uint16_t> conv = std::vector<uint16_t>(40, 3);
  std::vector<uint16_t> ple = std::vector<uint16_t>(24, 5);
  std::vector<uint16_t> kv = std::vector<uint16_t>(128, 7);
  std::vector<uint16_t> raw = std::vector<uint16_t>(64, 9);
  std::vector<uint16_t> comp = std::vector<uint16_t>(64, 11);
  std::vector<int> rope = std::vector<int>(96, 13);

  explicit Fixture(bool ragged = true) {
    model.cfg.max_seq = 2;
    model.cfg.max_len = 16;
    model.cfg.hc = 1;
    model.cfg.hs = 3;
    if (ragged) model.cfg.seq_capacities = {16, 12};
    model.layers.resize(2);
    auto& linear = model.layers[0];
    linear.linear.nkh = 1;
    linear.linear.nv = 2;
    linear.linear.kd = 2;
    linear.linear.vd = 3;
    linear.linear.conv_k = 3;
    linear.ssm_state = ssm.data();
    linear.conv_state = conv.data();
    linear.has_ple = true;
    linear.hc_dim = 3;
    linear.ple.conv_kernel = 3;
    linear.ple.conv_dilation = 2;
    linear.ple_conv_state = ple.data();
    auto& full = model.layers[1];
    full.is_full_attention = true;
    full.full.nkv = 1;
    full.full.hd = 2;
    full.full.idx_head_dim = 2;
    full.full.idx_compress = 4;
    full.seq_capacities = model.cfg.seq_capacities;
    full.max_len = 16;
    full.kv_cache = kv.data();
    full.idx_raw = raw.data();
    full.idx_comp = comp.data();
    model.d_rope_pos = rope.data();
  }
};

struct Directory {
  std::string path;
  Directory() {
    const char* root = std::getenv("Q4T_SESSION_TEST_DIRECTORY");
    if (!root) throw std::runtime_error("explicit test artifact root required");
    std::string pattern = std::string(root) + "/case-XXXXXX";
    if (!mkdtemp(pattern.data())) throw std::runtime_error("test directory");
    path = pattern;
  }
  ~Directory() { std::filesystem::remove(path); }
};
const auto Copy = [](void* host, void* state, size_t bytes, bool restore) {
  std::memcpy(restore ? state : host, restore ? host : state, bytes);
  return Status();
};
const auto Active = [] { return false; };
}  // namespace

Q4T_TEST(target_session_ranges_preserve_effective_state_and_slot_offsets) {
  for (const bool ragged : {true, false}) {
    Fixture f(ragged);
    for (const int prefix : {3, 5, 8}) {
      std::vector<SessionRange> ranges;
      Q4T_CHECK(TargetSessionStateRanges(f.model, 1, prefix, &ranges));
      // Independent dimensions: linear 2*2*3 FP32, conv10*2 BF16,
      // PLE3*2*2 BF16, attention row4 BF16, index row2 BF16, 3 RoPE rows.
      const std::vector<SessionRange> expected{
          {f.ssm.data() + 12, 48},
          {f.conv.data() + 20, 40},
          {f.ple.data() + 12, 24},
          {f.kv.data() + 64, static_cast<size_t>(prefix) * 8},
          {f.raw.data() + 32, static_cast<size_t>(prefix) * 4}};
      Q4T_CHECK(ranges.size() == (prefix < 4 ? 8 : 9));
      size_t at = 0;
      for (const auto& value : expected) {
        Q4T_CHECK(ranges[at].data == value.data &&
                  ranges[at].bytes == value.bytes);
        ++at;
      }
      if (prefix >= 4) {
        Q4T_CHECK(ranges[at].data == f.comp.data() + 32 &&
                  ranges[at].bytes == static_cast<size_t>(prefix / 4) * 4);
        ++at;
      }
      for (int row = 0; row < 3; ++row, ++at) {
        Q4T_CHECK(ranges[at].data == f.rope.data() + (3 + row) * 16);
        Q4T_CHECK(ranges[at].bytes == static_cast<size_t>(prefix) * 4);
      }
    }
    std::vector<SessionRange> ranges;
    Q4T_CHECK(!TargetSessionStateRanges(f.model, -1, 5, &ranges));
    Q4T_CHECK(!TargetSessionStateRanges(f.model, 2, 5, &ranges));
    Q4T_CHECK(!TargetSessionStateRanges(f.model, 1, 0, &ranges));
    Q4T_CHECK(!TargetSessionStateRanges(f.model, 1, f.model.cfg.Capacity(1),
                                        &ranges));
    f.model.layers[0].ple_conv_state = nullptr;
    Q4T_CHECK(!TargetSessionStateRanges(f.model, 1, 5, &ranges));
  }
  return true;
}

Q4T_TEST(target_session_layout_is_not_a_shifted_draft_snapshot) {
  Fixture f;
  mtp::MtpModel draft;
  draft.max_seq = draft.cfg.max_seq = 2;
  draft.cfg.max_len = 16;
  draft.cfg.hc = 1;
  draft.cfg.hs = 3;
  draft.cfg.seq_capacities = {16, 12};
  draft.full_attn = f.model.layers[1].full;
  draft.kv_cache = f.kv.data();
  draft.idx_raw = f.raw.data();
  draft.idx_comp = f.comp.data();
  uint16_t g[3]{}, h[3]{};
  std::vector<SessionRange> target, mtp;
  Q4T_CHECK(TargetSessionStateRanges(f.model, 1, 5, &target));
  Q4T_CHECK(SessionStateRanges(f.model, draft, 1, 5, g, h, &mtp));
  Q4T_CHECK(mtp.size() == target.size() + 5);
  for (size_t i = 0; i < target.size(); ++i)
    Q4T_CHECK(target[i].data == mtp[i].data && target[i].bytes == mtp[i].bytes);
  Q4T_CHECK(mtp[mtp.size() - 2].data == g && mtp.back().data == h);
  Q4T_CHECK(mtp.back().bytes == 6);
  return true;
}

Q4T_TEST(target_session_storage_checks_layout_before_copy_and_publication) {
  Directory directory;
  Fixture f;
  SessionCache cache;
  Q4T_CHECK(cache.Open(directory.path, 1, 1 << 20));
  std::vector<SessionRange> ranges;
  Q4T_CHECK(TargetSessionStateRanges(f.model, 1, 5, &ranges));
  const std::vector<int32_t> snapshot{10, 11, 12, 13, 14, -1};
  const std::vector<int32_t> prompt{10, 11, 12, 13, 14, 15, 16};
  Q4T_CHECK(cache.Acquire("client:session"));
  Q4T_CHECK(!cache.Acquire("client:session"));
  Q4T_CHECK(cache.Save("client:session", snapshot, ranges, Copy, Active, -1,
                       nullptr, SessionLayout::kTarget));
  Q4T_CHECK(cache.Match("client:session", prompt, SessionLayout::kTarget) == 0);
  cache.Release("client:session", true);
  Q4T_CHECK(cache.Acquire("client:session"));
  Q4T_CHECK(cache.Match("client:session", prompt) == 0);
  Q4T_CHECK(cache.Match("client:session", prompt, SessionLayout::kTarget) == 5);
  size_t copies = 0;
  const auto tracked = [&](void* host, void* state, size_t bytes,
                           bool restore) {
    ++copies;
    return Copy(host, state, bytes, restore);
  };
  Q4T_CHECK(!cache.Restore("client:session", ranges, tracked, Active, nullptr));
  Q4T_CHECK(copies == 0);
  for (const auto& r : ranges) std::memset(r.data, 0, r.bytes);
  int32_t pending = 123;
  Q4T_CHECK(cache.Restore("client:session", ranges, tracked, Active, nullptr,
                          &pending, SessionLayout::kTarget));
  Q4T_CHECK(pending == -1 && copies == ranges.size());
  // Restored state is byte-exact, and slot0 and the unused suffix are intact.
  Q4T_CHECK(f.ssm == std::vector<float>(24, 1.25f));
  Q4T_CHECK(f.conv == std::vector<uint16_t>(40, 3));
  Q4T_CHECK(f.ple == std::vector<uint16_t>(24, 5));
  Q4T_CHECK(f.kv == std::vector<uint16_t>(128, 7));
  Q4T_CHECK(f.raw == std::vector<uint16_t>(64, 9));
  Q4T_CHECK(f.comp == std::vector<uint16_t>(64, 11));
  Q4T_CHECK(f.rope == std::vector<int>(96, 13));
  cache.Release("client:session", true);
  Q4T_CHECK(cache.Acquire("cancelled"));
  Q4T_CHECK(cache.Save("cancelled", snapshot, ranges, Copy, Active, -1, nullptr,
                       SessionLayout::kTarget));
  cache.Release("cancelled", false);
  Q4T_CHECK(cache.Acquire("cancelled"));
  Q4T_CHECK(cache.Match("cancelled", prompt, SessionLayout::kTarget) == 0);
  cache.Release("cancelled", false);
  return true;
}

Q4T_TEST(target_session_shared_selection_and_incremental_layout_switch) {
  Directory directory;
  SessionCache cache;
  Q4T_CHECK(cache.Open(directory.path, 1, 128 << 20, true));
  std::vector<uint8_t> bytes(4096, 41);
  const std::vector<SessionRange> ranges{{bytes.data(), bytes.size()}};
  const std::vector<int32_t> prefix{10, 11, 12, 13};
  for (const auto& [key, layout] :
       {std::pair{"scope:mtp", SessionLayout::kMtp},
        std::pair{"scope:target-v1:x", SessionLayout::kTarget},
        std::pair{"other:target-v1:x", SessionLayout::kTarget}}) {
    Q4T_CHECK(cache.AcquireNew(key));
    Q4T_CHECK(
        cache.Save(key, prefix, ranges, Copy, Active, 20, nullptr, layout));
    cache.Release(key, true);
  }
  std::string key;
  Q4T_CHECK(cache.MatchShared("scope", prefix, &key, SessionLayout::kTarget) ==
            3);
  Q4T_CHECK(key == "scope:target-v1:x");
  Q4T_CHECK(cache.Restore(key, ranges, Copy, Active, nullptr, nullptr,
                          SessionLayout::kTarget));
  // Same sized ranges cannot make a different layout eligible for reuse.
  std::fill(bytes.begin(), bytes.end(), 71);
  Q4T_CHECK(cache.Save(key, prefix, ranges, Copy, Active, 29));
  cache.Release(key, true);
  Q4T_CHECK(cache.Acquire(key));
  Q4T_CHECK(cache.Match(key, prefix, SessionLayout::kTarget) == 0);
  Q4T_CHECK(cache.Match(key, prefix) == 3);
  std::fill(bytes.begin(), bytes.end(), 0);
  int32_t d0 = -1;
  Q4T_CHECK(cache.Restore(key, ranges, Copy, Active, &d0));
  Q4T_CHECK(d0 == 29 && bytes == std::vector<uint8_t>(4096, 71));
  cache.Release(key, true);
  Q4T_CHECK(cache.MatchShared("scope", prefix, &key, SessionLayout::kTarget) ==
            0);
  Q4T_CHECK(cache.MatchShared("scope", prefix, &key) == 3);
  cache.Release(key, true);
  return true;
}
