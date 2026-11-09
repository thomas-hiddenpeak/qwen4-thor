#include "q4t/server/native_crypto.h"

#include <algorithm>
#include <stdexcept>

#include "q4t/test.h"

namespace {
using namespace q4t::server;
std::string Unhex(std::string_view text) {
  std::string bytes;
  for (size_t i = 0; i < text.size(); i += 2)
    bytes.push_back(static_cast<char>(
        std::stoul(std::string(text.substr(i, 2)), nullptr, 16)));
  return bytes;
}
template <size_t Size>
std::array<uint8_t, Size> Fixed(std::string_view text) {
  const auto bytes = Unhex(text);
  if (bytes.size() != Size) throw std::runtime_error("crypto fixture size");
  std::array<uint8_t, Size> result{};
  std::copy(bytes.begin(), bytes.end(), result.begin());
  return result;
}
}  // namespace

Q4T_TEST(native_crypto_nist_aes256gcm_known_answer) {
  // NIST public test vector, as published in OpenSSL's AES-GCM demo:
  // https://github.com/openssl/openssl/blob/openssl-3.0.13/demos/cipher/aesgcm.c
  // Only numerical vector data is reused; the implementation is independent.
  const auto key = Fixed<32>(
      "eebc1f57487f51921c0465665f8ae6d1658bb26de6f8a069a3520293a572078f");
  const auto nonce = Fixed<12>("99aa3e68ed8173a0eed06684");
  const auto aad = Unhex("4d23c3cec334b49bdb370c437fec78de");
  const auto plain = Unhex("f56e87055bc32d0eeb31b2eacc2bf2a5");
  const auto expected = Unhex("f7264413a84c0e7cd536867eb9f21736");
  const auto expected_tag = Fixed<16>("67ba0510262ae487d737ee6298f77e0c");
  std::string encrypted, decrypted;
  CryptoTag tag{};
  Q4T_CHECK(CryptoAvailable() == CryptoResult::kOk);
  Q4T_CHECK(CryptoSeal(key, nonce, aad, plain, &encrypted, &tag) ==
            CryptoResult::kOk);
  Q4T_CHECK(encrypted == expected && tag == expected_tag);
  Q4T_CHECK(CryptoOpen(key, nonce, aad, expected, expected_tag, &decrypted) ==
            CryptoResult::kOk);
  Q4T_CHECK(decrypted == plain);
  return true;
}

Q4T_TEST(native_crypto_empty_binary_and_utf8_roundtrip) {
  CryptoKey key{};
  CryptoNonce nonce{};
  Q4T_CHECK(CryptoRandom(key) == CryptoResult::kOk);
  const std::string samples[] = {"", std::string("a\0b\xff", 4),
                                 "真实推理：资料与运行记录✅"};
  for (const auto& plain : samples) {
    Q4T_CHECK(CryptoRandom(nonce) == CryptoResult::kOk);
    CryptoTag tag{};
    std::string encrypted, decrypted = "unchanged";
    Q4T_CHECK(CryptoSeal(key, nonce, "client:a", plain, &encrypted, &tag) ==
              CryptoResult::kOk);
    Q4T_CHECK(CryptoOpen(key, nonce, "client:a", encrypted, tag, &decrypted) ==
              CryptoResult::kOk);
    Q4T_CHECK(decrypted == plain && encrypted.size() == plain.size());
  }
  return true;
}

Q4T_TEST(native_crypto_rejects_every_authenticated_component_without_output) {
  CryptoKey key{};
  CryptoNonce nonce{};
  CryptoTag tag{};
  std::string encrypted;
  Q4T_CHECK(CryptoSeal(key, nonce, "aad", "private reasoning", &encrypted,
                       &tag) == CryptoResult::kOk);
  for (int part = 0; part < 6; ++part) {
    auto wrong_key = key;
    auto wrong_nonce = nonce;
    auto wrong_tag = tag;
    auto wrong_cipher = encrypted;
    std::string wrong_aad = "aad", output = "unchanged";
    if (part == 0) wrong_key[0] ^= 1;
    if (part == 1) wrong_nonce[11] ^= 1;
    if (part == 2) wrong_tag[15] ^= 1;
    if (part == 3) wrong_cipher[2] ^= 1;
    if (part == 4) wrong_aad += 'x';
    if (part == 5) wrong_cipher.pop_back();
    Q4T_CHECK(CryptoOpen(wrong_key, wrong_nonce, wrong_aad, wrong_cipher,
                         wrong_tag, &output) == CryptoResult::kAuthentication);
    Q4T_CHECK(output == "unchanged");
  }
  return true;
}

Q4T_TEST(native_crypto_limits_are_transactional) {
  const CryptoKey key{};
  const CryptoNonce nonce{};
  CryptoTag tag{};
  std::string output = "unchanged", plain(kCryptoPlaintextLimit + 1, 'x');
  Q4T_CHECK(CryptoSeal(key, nonce, "", plain, &output, &tag) ==
            CryptoResult::kInvalid);
  Q4T_CHECK(output == "unchanged");
  Q4T_CHECK(CryptoOpen(key, nonce, "", plain, tag, &output) ==
            CryptoResult::kInvalid);
  plain = "input";
  const std::string aad(kCryptoAadLimit + 1, 'a');
  Q4T_CHECK(CryptoSeal(key, nonce, aad, plain, &output, &tag) ==
            CryptoResult::kInvalid);
  Q4T_CHECK(CryptoOpen(key, nonce, aad, plain, tag, &output) ==
            CryptoResult::kInvalid);
  Q4T_CHECK(output == "unchanged");
  return true;
}

Q4T_TEST(native_crypto_random_and_wipe) {
  CryptoKey a{}, b{};
  Q4T_CHECK(CryptoRandom(a) == CryptoResult::kOk &&
            CryptoRandom(b) == CryptoResult::kOk && a != b);
  CryptoWipe(a);
  Q4T_CHECK(
      std::all_of(a.begin(), a.end(), [](uint8_t byte) { return !byte; }));
  std::string secret = "secret";
  CryptoWipe(&secret);
  Q4T_CHECK(secret.empty());
  std::array<uint8_t, 257> too_large;
  too_large.fill(1);
  Q4T_CHECK(CryptoRandom(too_large) == CryptoResult::kInvalid);
  Q4T_CHECK(std::all_of(too_large.begin(), too_large.end(),
                        [](uint8_t byte) { return !byte; }));
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(native_crypto_dependency_failure_is_request_local_and_recoverable) {
  struct Reset {
    ~Reset() { SetCryptoUnavailableForTesting(false); }
  } reset;
  SetCryptoUnavailableForTesting(true);
  std::string output = "unchanged";
  CryptoTag tag{};
  Q4T_CHECK(CryptoAvailable() == CryptoResult::kUnavailable);
  Q4T_CHECK(CryptoSeal({}, {}, "a", "p", &output, &tag) ==
            CryptoResult::kUnavailable);
  Q4T_CHECK(CryptoOpen({}, {}, "a", "p", tag, &output) ==
            CryptoResult::kUnavailable);
  Q4T_CHECK(output == "unchanged");
  SetCryptoUnavailableForTesting(false);
  Q4T_CHECK(CryptoSeal({}, {}, "a", "p", &output, &tag) == CryptoResult::kOk);
  return true;
}
#endif
