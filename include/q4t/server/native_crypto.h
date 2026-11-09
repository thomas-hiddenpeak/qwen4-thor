// Optional, request-local authenticated encryption using system OpenSSL 3.
#pragma once

#include <array>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>

namespace q4t::server {
using CryptoKey = std::array<uint8_t, 32>;
using CryptoNonce = std::array<uint8_t, 12>;
using CryptoTag = std::array<uint8_t, 16>;
enum class CryptoResult {
  kOk,
  kUnavailable,
  kInvalid,
  kAuthentication,
  kFailure
};
inline constexpr size_t kCryptoPlaintextLimit = 4ULL << 20;
inline constexpr size_t kCryptoAadLimit = 64ULL << 10;

// Nonblocking kernel entropy; no timestamp/PRNG fallback. Failure clears out.
CryptoResult CryptoRandom(std::span<uint8_t> out) noexcept;
void CryptoWipe(std::span<uint8_t> out) noexcept;
void CryptoWipe(std::string* text) noexcept;
CryptoResult CryptoAvailable() noexcept;
// Fixed AES-256-GCM, 96-bit nonce and 128-bit tag. The owner must supply a
// fresh nonce for every seal under a given key. Outputs change only on success;
// unauthenticated plaintext never leaves the function, including on exceptions.
CryptoResult CryptoSeal(const CryptoKey& key, const CryptoNonce& nonce,
                        std::string_view aad, std::string_view plaintext,
                        std::string* ciphertext, CryptoTag* tag);
CryptoResult CryptoOpen(const CryptoKey& key, const CryptoNonce& nonce,
                        std::string_view aad, std::string_view ciphertext,
                        const CryptoTag& tag, std::string* plaintext);

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetCryptoUnavailableForTesting(bool unavailable);
#endif
}  // namespace q4t::server
