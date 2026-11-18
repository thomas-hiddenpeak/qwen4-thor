#include "q4t/trace/sha256.h"

#include <algorithm>
#include <array>
#include <vector>

#include "q4t/test.h"

Q4T_TEST(sha256_stream_snapshots_preserve_future_bytes_and_known_digests) {
  q4t::trace::Sha256Stream hash;
  Q4T_CHECK(q4t::trace::HexDigest(hash.Snapshot()) ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  const std::string abc = "abc";
  for (size_t i = 0; i < abc.size(); ++i) {
    Q4T_CHECK(hash.Add({reinterpret_cast<const uint8_t*>(abc.data() + i), 1}));
    const auto first = hash.Snapshot();
    Q4T_CHECK(first == hash.Snapshot());
    Q4T_CHECK(hash.bytes() == i + 1);
  }
  Q4T_CHECK(q4t::trace::HexDigest(hash.Snapshot()) ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  return true;
}

Q4T_TEST(sha256_stream_capacity_rejection_is_atomic) {
  q4t::trace::Sha256Stream hash(3), empty(0);
  const std::array<uint8_t, 4> data{'a', 'b', 'c', 'd'};
  Q4T_CHECK(hash.Add(std::span(data).first(2)));
  const auto before = hash.Snapshot();
  Q4T_CHECK(!hash.Add(std::span(data).subspan(2)));
  Q4T_CHECK(hash.Snapshot() == before && hash.bytes() == 2);
  Q4T_CHECK(hash.Add(std::span(data).subspan(2, 1)));
  Q4T_CHECK(hash.Snapshot() == q4t::trace::Sha256(std::span(data).first(3)));
  Q4T_CHECK(!empty.Add(data) && empty.bytes() == 0);
  Q4T_CHECK(empty.Add({}) && empty.Snapshot() == q4t::trace::Sha256({}));
  return true;
}

Q4T_TEST(sha256_stream_packet_boundaries_match_complete_payload) {
  std::vector<uint8_t> data(65537);
  for (size_t i = 0; i < data.size(); ++i) data[i] = (i * 37 + 13) % 256;
  const auto expected = q4t::trace::Sha256(data);
  for (size_t packet : {1, 17, 63, 64, 65, 4096, 65536}) {
    q4t::trace::Sha256Stream hash(data.size());
    for (size_t i = 0; i < data.size(); i += packet)
      Q4T_CHECK(hash.Add(
          std::span(data).subspan(i, std::min(packet, data.size() - i))));
    Q4T_CHECK(hash.bytes() == data.size() && hash.Snapshot() == expected);
  }
  return true;
}
