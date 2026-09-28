#include "q4t/trace/sha256.h"
#include "q4t/test.h"
#include <string>

Q4T_TEST(router_trace_hash_known_vectors) {
  const auto hash = [](const std::string& s) {
    return q4t::trace::HexDigest(q4t::trace::Sha256(
        {reinterpret_cast<const uint8_t*>(s.data()), s.size()}));
  };
  Q4T_CHECK(hash("") ==
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  Q4T_CHECK(hash("abc") ==
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  Q4T_CHECK(hash("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq") ==
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
  Q4T_CHECK(hash(std::string(1000000, 'a')) ==
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
  return true;
}
