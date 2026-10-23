#include "q4t/trace/sha256.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstring>
#include <fstream>

#if defined(__aarch64__) && defined(__linux__) && \
    __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
#include <arm_neon.h>
#include <asm/hwcap.h>
#include <sys/auxv.h>
#define Q4T_SHA2_ARM 1
#endif

namespace q4t::trace {
namespace {
constexpr uint32_t kRound[] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
// Keep crypto instructions in a separately targeted function. The generic
// caller can run on CPUs without SHA2; Linux HWCAP gates every hardware path.
bool HasSha2() {
#if defined(Q4T_SHA2_ARM)
  static const bool available = (getauxval(AT_HWCAP) & HWCAP_SHA2) != 0;
  return available;
#else
  return false;
#endif
}

#if defined(Q4T_SHA2_ARM)
__attribute__((target("+crypto"), noinline)) void TransformSha2(
    uint32_t* state, const uint8_t* input, size_t blocks) {
  uint32x4_t abcd = vld1q_u32(state);
  uint32x4_t efgh = vld1q_u32(state + 4);
  for (size_t block = 0; block < blocks; ++block, input += 64) {
    const uint32x4_t saved_abcd = abcd;
    const uint32x4_t saved_efgh = efgh;
    uint32x4_t words[4];
    for (int i = 0; i < 4; ++i) {
      words[i] = vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(input + i * 16)));
    }
    for (int group = 0; group < 16; ++group) {
      const int i = group % 4;
      const uint32x4_t wk = vaddq_u32(words[i], vld1q_u32(kRound + group * 4));
      const uint32x4_t old_abcd = abcd;
      abcd = vsha256hq_u32(abcd, efgh, wk);
      efgh = vsha256h2q_u32(efgh, old_abcd, wk);
      if (group < 12) {
        words[i] =
            vsha256su1q_u32(vsha256su0q_u32(words[i], words[(i + 1) % 4]),
                            words[(i + 2) % 4], words[(i + 3) % 4]);
      }
    }
    abcd = vaddq_u32(abcd, saved_abcd);
    efgh = vaddq_u32(efgh, saved_efgh);
  }
  vst1q_u32(state, abcd);
  vst1q_u32(state + 4, efgh);
}
#endif

class Hash {
 public:
  explicit Hash(bool portable = false) : use_sha2_(!portable && HasSha2()) {}

  void Add(std::span<const uint8_t> bytes) {
    length_ += bytes.size();
    if (bytes.empty()) return;
    if (used_ != 0) {
      const size_t count = std::min(bytes.size(), block_.size() - used_);
      std::memcpy(block_.data() + used_, bytes.data(), count);
      used_ += count;
      bytes = bytes.subspan(count);
      if (used_ == block_.size()) {
        TransformBlocks(block_.data(), 1);
        used_ = 0;
      }
    }
    const size_t blocks = bytes.size() / block_.size();
    if (blocks != 0) {
      TransformBlocks(bytes.data(), blocks);
      bytes = bytes.subspan(blocks * block_.size());
    }
    if (!bytes.empty()) {
      std::memcpy(block_.data() + used_, bytes.data(), bytes.size());
      used_ += bytes.size();
    }
  }
  Digest Finish() {
    const uint64_t bits = length_ * 8;
    const uint8_t marker = 128, zero = 0;
    Add({&marker, 1});
    while (used_ != 56) Add({&zero, 1});
    for (int shift = 56; shift >= 0; shift -= 8) {
      const uint8_t byte = bits >> shift;
      Add({&byte, 1});
    }
    Digest result;
    for (int i = 0; i < 32; ++i)
      result[i] = state_[i / 4] >> (24 - 8 * (i % 4));
    return result;
  }

 private:
  void TransformBlocks(const uint8_t* input, size_t blocks) {
#if defined(Q4T_SHA2_ARM)
    if (use_sha2_) {
      TransformSha2(state_.data(), input, blocks);
      return;
    }
#endif
    for (size_t i = 0; i < blocks; ++i) TransformPortable(input + i * 64);
  }

  void TransformPortable(const uint8_t* block) {
    uint32_t w[64];
    for (int i = 0; i < 16; ++i) {
      w[i] = 0;
      for (int j = 0; j < 4; ++j) w[i] = (w[i] << 8) | block[4 * i + j];
    }
    for (int i = 16; i < 64; ++i) {
      const auto x = w[i - 15], y = w[i - 2];
      w[i] = w[i - 16] + (std::rotr(x, 7) ^ std::rotr(x, 18) ^ (x >> 3)) +
             w[i - 7] + (std::rotr(y, 17) ^ std::rotr(y, 19) ^ (y >> 10));
    }
    auto [a, b, c, d, e, f, g, h] = state_;
    for (int i = 0; i < 64; ++i) {
      const uint32_t t1 =
          h + (std::rotr(e, 6) ^ std::rotr(e, 11) ^ std::rotr(e, 25)) +
          ((e & f) ^ (~e & g)) + kRound[i] + w[i];
      const uint32_t t2 =
          (std::rotr(a, 2) ^ std::rotr(a, 13) ^ std::rotr(a, 22)) +
          ((a & b) ^ (a & c) ^ (b & c));
      h = g;
      g = f;
      f = e;
      e = d + t1;
      d = c;
      c = b;
      b = a;
      a = t1 + t2;
    }
    const uint32_t final[] = {a, b, c, d, e, f, g, h};
    for (int i = 0; i < 8; ++i) state_[i] += final[i];
  }
  std::array<uint32_t, 8> state_{0x6a09e667, 0xbb67ae85, 0x3c6ef372,
                                 0xa54ff53a, 0x510e527f, 0x9b05688c,
                                 0x1f83d9ab, 0x5be0cd19};
  std::array<uint8_t, 64> block_{};
  [[maybe_unused]] bool use_sha2_;
  size_t used_ = 0;
  uint64_t length_ = 0;
};
}  // namespace
Digest Sha256(std::span<const uint8_t> bytes) {
  Hash hash;
  hash.Add(bytes);
  return hash.Finish();
}
static Status HashFileImpl(const std::string& path, Digest* digest,
                           bool portable) {
  std::ifstream file(path, std::ios::binary);
  if (!file) return Status::Fail("trace: cannot hash " + path);
  Hash hash(portable);
  std::array<uint8_t, 65536> buffer;
  while (file.read(reinterpret_cast<char*>(buffer.data()), buffer.size()) ||
         file.gcount() > 0)
    hash.Add({buffer.data(), static_cast<size_t>(file.gcount())});
  if (!file.eof()) return Status::Fail("trace: hash read failure");
  *digest = hash.Finish();
  return Status();
}
Status HashFile(const std::string& path, Digest* digest) {
  return HashFileImpl(path, digest, false);
}
#if defined(Q4T_SHA256_TESTING)
bool Sha256HardwareForTest() { return HasSha2(); }
Digest Sha256ChunksForTest(std::span<const uint8_t> bytes, size_t chunk,
                           bool portable) {
  Hash hash(portable);
  if (chunk == 0) chunk = bytes.size();
  while (!bytes.empty()) {
    const size_t count = std::min(chunk, bytes.size());
    hash.Add(bytes.first(count));
    bytes = bytes.subspan(count);
  }
  return hash.Finish();
}
Status HashFilePortableForTest(const std::string& path, Digest* digest) {
  return HashFileImpl(path, digest, true);
}
#endif
std::string HexDigest(const Digest& digest) {
  std::string out;
  for (auto b : digest) {
    out += "0123456789abcdef"[b >> 4];
    out += "0123456789abcdef"[b & 15];
  }
  return out;
}
}  // namespace q4t::trace
