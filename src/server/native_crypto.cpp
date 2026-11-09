#include "q4t/server/native_crypto.h"

#include <dlfcn.h>
#include <sys/random.h>

#include <atomic>
#include <cerrno>
#include <stdexcept>

#include <openssl/crypto.h>
#include <openssl/evp.h>

namespace q4t::server {
namespace {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
std::atomic<bool> unavailable{false};
#endif
class Library {
 public:
  Library() {
    // No request-selected module/config, no startup dependency on libcrypto.
    handle_ = dlopen(Q4T_SYSTEM_CRYPTO_LIBRARY,
                     RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
    if (!handle_) throw std::runtime_error("system libcrypto unavailable");
    try {
#define Q4T_CRYPTO_LOAD(name) Load(&name, #name)
      Q4T_CRYPTO_LOAD(OPENSSL_init_crypto);
      Q4T_CRYPTO_LOAD(OSSL_LIB_CTX_new);
      Q4T_CRYPTO_LOAD(OSSL_LIB_CTX_free);
      Q4T_CRYPTO_LOAD(OPENSSL_thread_stop_ex);
      Q4T_CRYPTO_LOAD(EVP_CIPHER_fetch);
      Q4T_CRYPTO_LOAD(EVP_CIPHER_free);
      Q4T_CRYPTO_LOAD(EVP_CIPHER_CTX_new);
      Q4T_CRYPTO_LOAD(EVP_CIPHER_CTX_free);
      Q4T_CRYPTO_LOAD(EVP_CIPHER_CTX_ctrl);
      Q4T_CRYPTO_LOAD(EVP_EncryptInit_ex2);
      Q4T_CRYPTO_LOAD(EVP_EncryptUpdate);
      Q4T_CRYPTO_LOAD(EVP_EncryptFinal_ex);
      Q4T_CRYPTO_LOAD(EVP_DecryptInit_ex2);
      Q4T_CRYPTO_LOAD(EVP_DecryptUpdate);
      Q4T_CRYPTO_LOAD(EVP_DecryptFinal_ex);
#undef Q4T_CRYPTO_LOAD
      // Optional checksum symbols must not disable authenticated encryption.
#define Q4T_MD_LOAD(name) \
  name = reinterpret_cast<decltype(name)>(dlsym(handle_, #name))
      Q4T_MD_LOAD(EVP_MD_fetch);
      Q4T_MD_LOAD(EVP_MD_free);
      Q4T_MD_LOAD(EVP_MD_CTX_new);
      Q4T_MD_LOAD(EVP_MD_CTX_free);
      Q4T_MD_LOAD(EVP_DigestInit_ex2);
      Q4T_MD_LOAD(EVP_DigestUpdate);
      Q4T_MD_LOAD(EVP_DigestFinal_ex);
#undef Q4T_MD_LOAD
      if (!OPENSSL_init_crypto(OPENSSL_INIT_NO_LOAD_CONFIG, nullptr))
        throw std::runtime_error("system libcrypto initialization failed");
    } catch (...) {
      dlclose(handle_);
      throw;
    }
  }
  ~Library() { dlclose(handle_); }
#define Q4T_CRYPTO_FUNCTION(name) decltype(&::name) name = nullptr
  Q4T_CRYPTO_FUNCTION(OPENSSL_init_crypto);
  Q4T_CRYPTO_FUNCTION(OSSL_LIB_CTX_new);
  Q4T_CRYPTO_FUNCTION(OSSL_LIB_CTX_free);
  Q4T_CRYPTO_FUNCTION(OPENSSL_thread_stop_ex);
  Q4T_CRYPTO_FUNCTION(EVP_CIPHER_fetch);
  Q4T_CRYPTO_FUNCTION(EVP_CIPHER_free);
  Q4T_CRYPTO_FUNCTION(EVP_CIPHER_CTX_new);
  Q4T_CRYPTO_FUNCTION(EVP_CIPHER_CTX_free);
  Q4T_CRYPTO_FUNCTION(EVP_CIPHER_CTX_ctrl);
  Q4T_CRYPTO_FUNCTION(EVP_EncryptInit_ex2);
  Q4T_CRYPTO_FUNCTION(EVP_EncryptUpdate);
  Q4T_CRYPTO_FUNCTION(EVP_EncryptFinal_ex);
  Q4T_CRYPTO_FUNCTION(EVP_DecryptInit_ex2);
  Q4T_CRYPTO_FUNCTION(EVP_DecryptUpdate);
  Q4T_CRYPTO_FUNCTION(EVP_DecryptFinal_ex);
  Q4T_CRYPTO_FUNCTION(EVP_MD_fetch);
  Q4T_CRYPTO_FUNCTION(EVP_MD_free);
  Q4T_CRYPTO_FUNCTION(EVP_MD_CTX_new);
  Q4T_CRYPTO_FUNCTION(EVP_MD_CTX_free);
  Q4T_CRYPTO_FUNCTION(EVP_DigestInit_ex2);
  Q4T_CRYPTO_FUNCTION(EVP_DigestUpdate);
  Q4T_CRYPTO_FUNCTION(EVP_DigestFinal_ex);
#undef Q4T_CRYPTO_FUNCTION

 private:
  template <class Function>
  void Load(Function* target, const char* name) {
    *target = reinterpret_cast<Function>(dlsym(handle_, name));
    if (!*target) throw std::runtime_error("system libcrypto ABI unavailable");
  }
  void* handle_ = nullptr;
};
const Library* Api() noexcept {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (unavailable.load()) return nullptr;
#endif
  try {
    // A failed initialization is retried on the next related request only.
    static const Library library;
    return &library;
  } catch (...) {
    return nullptr;
  }
}
class Context {
 public:
  explicit Context(const Library& library) : api(library) {
    // A private provider context does not inherit ambient OpenSSL config or
    // another dependency's default properties. The default provider is built
    // into the system libcrypto; no user-specified provider module is loaded.
    library_context = api.OSSL_LIB_CTX_new();
    if (library_context)
      cipher = api.EVP_CIPHER_fetch(library_context, "AES-256-GCM",
                                    "provider=default");
    if (cipher) context = api.EVP_CIPHER_CTX_new();
  }
  ~Context() {
    api.EVP_CIPHER_CTX_free(context);
    api.EVP_CIPHER_free(cipher);
    if (library_context) {
      api.OPENSSL_thread_stop_ex(library_context);
      api.OSSL_LIB_CTX_free(library_context);
    }
  }
  Context(const Context&) = delete;
  Context& operator=(const Context&) = delete;
  const Library& api;
  OSSL_LIB_CTX* library_context = nullptr;
  EVP_CIPHER* cipher = nullptr;
  EVP_CIPHER_CTX* context = nullptr;
};
struct Scratch {
  explicit Scratch(size_t size) : bytes(size, '\0') {}
  ~Scratch() { CryptoWipe(&bytes); }
  std::string bytes;
  uint8_t* Data() { return reinterpret_cast<uint8_t*>(bytes.data()); }
};
const uint8_t* Bytes(std::string_view text) {
  return reinterpret_cast<const uint8_t*>(text.data());
}
bool Bounds(std::string_view aad, std::string_view text) {
  return aad.size() <= kCryptoAadLimit && text.size() <= kCryptoPlaintextLimit;
}
}  // namespace

struct CryptoMd5::State {
  explicit State(const Library& library) : api(library) {
    library_context = api.OSSL_LIB_CTX_new();
    if (library_context)
      digest = api.EVP_MD_fetch(library_context, "MD5", "provider=default");
    if (digest) context = api.EVP_MD_CTX_new();
    ready = context && api.EVP_DigestInit_ex2(context, digest, nullptr) == 1;
  }
  ~State() {
    api.EVP_MD_CTX_free(context);
    api.EVP_MD_free(digest);
    if (library_context) {
      api.OPENSSL_thread_stop_ex(library_context);
      api.OSSL_LIB_CTX_free(library_context);
    }
  }
  const Library& api;
  OSSL_LIB_CTX* library_context = nullptr;
  EVP_MD* digest = nullptr;
  EVP_MD_CTX* context = nullptr;
  bool ready = false;
};
CryptoMd5::CryptoMd5() = default;
CryptoMd5::~CryptoMd5() = default;
CryptoResult CryptoMd5::Begin() {
  if (state_) return CryptoResult::kInvalid;
  const auto* api = Api();
  if (!api || !api->EVP_MD_fetch || !api->EVP_MD_free || !api->EVP_MD_CTX_new ||
      !api->EVP_MD_CTX_free || !api->EVP_DigestInit_ex2 ||
      !api->EVP_DigestUpdate || !api->EVP_DigestFinal_ex)
    return CryptoResult::kUnavailable;
  state_ = std::make_unique<State>(*api);
  return state_->ready ? CryptoResult::kOk : CryptoResult::kUnavailable;
}
CryptoResult CryptoMd5::Update(std::string_view bytes) {
  if (!state_ || !state_->ready || bytes.size() > kCryptoPlaintextLimit)
    return CryptoResult::kInvalid;
  if (state_->api.EVP_DigestUpdate(state_->context, bytes.data(),
                                   bytes.size()) != 1) {
    state_->ready = false;
    return CryptoResult::kFailure;
  }
  return CryptoResult::kOk;
}
CryptoResult CryptoMd5::Finish(std::string* hex) {
  if (!state_ || !state_->ready || !hex) return CryptoResult::kInvalid;
  state_->ready = false;
  unsigned char bytes[EVP_MAX_MD_SIZE];
  unsigned int count = 0;
  if (state_->api.EVP_DigestFinal_ex(state_->context, bytes, &count) != 1 ||
      count != 16)
    return CryptoResult::kFailure;
  constexpr char digits[] = "0123456789abcdef";
  std::string result;
  result.reserve(32);
  for (unsigned int i = 0; i < count; ++i) {
    result += digits[bytes[i] >> 4];
    result += digits[bytes[i] & 15];
  }
  *hex = std::move(result);
  return CryptoResult::kOk;
}

void CryptoWipe(std::span<uint8_t> out) noexcept {
  // Volatile stores cannot be removed as dead writes by the optimizer.
  volatile uint8_t* bytes = out.data();
  for (size_t i = 0; i < out.size(); ++i) bytes[i] = 0;
}
void CryptoWipe(std::string* text) noexcept {
  if (text) {
    CryptoWipe({reinterpret_cast<uint8_t*>(text->data()), text->size()});
    text->clear();
  }
}
CryptoResult CryptoRandom(std::span<uint8_t> out) noexcept {
  if (out.empty() || out.size() > 256) {
    CryptoWipe(out);
    return CryptoResult::kInvalid;
  }
  size_t offset = 0;
  for (int attempts = 0; attempts < 8 && offset < out.size(); ++attempts) {
    const auto size =
        getrandom(out.data() + offset, out.size() - offset, GRND_NONBLOCK);
    if (size > 0)
      offset += size;
    else if (size < 0 && errno == EINTR)
      continue;
    else
      break;
  }
  if (offset == out.size()) return CryptoResult::kOk;
  CryptoWipe(out);
  return CryptoResult::kUnavailable;
}
CryptoResult CryptoAvailable() noexcept {
  const auto* api = Api();
  if (!api) return CryptoResult::kUnavailable;
  const Context operation(*api);
  return operation.context ? CryptoResult::kOk : CryptoResult::kUnavailable;
}
CryptoResult CryptoSeal(const CryptoKey& key, const CryptoNonce& nonce,
                        std::string_view aad, std::string_view plaintext,
                        std::string* ciphertext, CryptoTag* tag) {
  if (!ciphertext || !tag || !Bounds(aad, plaintext))
    return CryptoResult::kInvalid;
  const auto* api = Api();
  if (!api) return CryptoResult::kUnavailable;
  const Context operation(*api);
  if (!operation.context) return CryptoResult::kUnavailable;
  auto* ctx = operation.context;
  Scratch result(plaintext.size() + 16);
  CryptoTag candidate{};
  int ignored = 0, written = 0, tail = 0;
  if (!api->EVP_EncryptInit_ex2(ctx, operation.cipher, key.data(), nonce.data(),
                                nullptr) ||
      !api->EVP_EncryptUpdate(ctx, nullptr, &ignored, Bytes(aad), aad.size()) ||
      !api->EVP_EncryptUpdate(ctx, result.Data(), &written, Bytes(plaintext),
                              plaintext.size()) ||
      written < 0 || static_cast<size_t>(written) > plaintext.size() ||
      !api->EVP_EncryptFinal_ex(ctx, result.Data() + written, &tail) ||
      written < 0 || tail < 0 ||
      static_cast<size_t>(written + tail) != plaintext.size() ||
      !api->EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, candidate.size(),
                                candidate.data()))
    return CryptoResult::kFailure;
  result.bytes.resize(plaintext.size());
  *ciphertext = std::move(result.bytes);
  *tag = candidate;
  return CryptoResult::kOk;
}
CryptoResult CryptoOpen(const CryptoKey& key, const CryptoNonce& nonce,
                        std::string_view aad, std::string_view ciphertext,
                        const CryptoTag& tag, std::string* plaintext) {
  if (!plaintext || !Bounds(aad, ciphertext)) return CryptoResult::kInvalid;
  const auto* api = Api();
  if (!api) return CryptoResult::kUnavailable;
  const Context operation(*api);
  if (!operation.context) return CryptoResult::kUnavailable;
  auto* ctx = operation.context;
  Scratch result(ciphertext.size() + 16);
  auto expected = tag;
  int ignored = 0, written = 0, tail = 0;
  if (!api->EVP_DecryptInit_ex2(ctx, operation.cipher, key.data(), nonce.data(),
                                nullptr) ||
      !api->EVP_DecryptUpdate(ctx, nullptr, &ignored, Bytes(aad), aad.size()) ||
      !api->EVP_DecryptUpdate(ctx, result.Data(), &written, Bytes(ciphertext),
                              ciphertext.size()) ||
      written < 0 || static_cast<size_t>(written) > ciphertext.size() ||
      !api->EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, expected.size(),
                                expected.data()))
    return CryptoResult::kFailure;
  // Update may have produced plaintext, but it is private until authentication.
  if (api->EVP_DecryptFinal_ex(ctx, result.Data() + written, &tail) <= 0)
    return CryptoResult::kAuthentication;
  if (tail < 0 || static_cast<size_t>(written + tail) != ciphertext.size())
    return CryptoResult::kFailure;
  // Copy only authenticated bytes. The scratch always wipes its full buffer,
  // including small-string storage and exceptional allocation paths.
  plaintext->assign(result.bytes.data(), ciphertext.size());
  return CryptoResult::kOk;
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetCryptoUnavailableForTesting(bool value) { unavailable.store(value); }
#endif
}  // namespace q4t::server
