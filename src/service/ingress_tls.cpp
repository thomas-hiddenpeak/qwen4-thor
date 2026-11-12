#include "q4t/service/ingress_tls.h"

#include <poll.h>

#include <algorithm>
#include <cerrno>
#include <ctime>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

#include "q4t/service/ingress_address.h"
#include "q4t/service/ingress_transport.h"

#if Q4T_ENABLE_NATIVE_TLS
#include <dlfcn.h>
#include <openssl/err.h>
#include <openssl/pem.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

namespace q4t::service::ingress {
namespace {

// Types come from the build's system headers, not handwritten ABI replicas.
// dlopen uses the configured absolute system path. No plugin search or env
// supplied crypto path; only TLS enablement reaches this code.
#define Q4T_TLS_SYMBOLS(X)                                                     \
  X(OPENSSL_init_ssl)                                                          \
  X(TLS_server_method) X(SSL_CTX_new) X(SSL_CTX_free) X(SSL_CTX_ctrl) X(       \
      SSL_CTX_set_options) X(SSL_CTX_set_cipher_list) X(SSL_CTX_set_verify)    \
      X(SSL_CTX_set_verify_depth) X(SSL_CTX_use_certificate)                   \
          X(SSL_CTX_use_PrivateKey) X(SSL_CTX_check_private_key) X(            \
              SSL_CTX_get_cert_store) X(SSL_CTX_set_alpn_select_cb) X(SSL_new) \
              X(SSL_free) X(SSL_set_fd) X(SSL_accept) X(SSL_shutdown) X(       \
                  SSL_read_ex) X(SSL_write_ex) X(SSL_peek_ex) X(SSL_get_error) \
                  X(SSL_get_servername) X(SSL_CTX_callback_ctrl) X(            \
                      ERR_clear_error) X(BIO_new_mem_buf) X(BIO_free)          \
                      X(PEM_read_bio_X509_AUX) X(PEM_read_bio_PrivateKey) X(   \
                          EVP_PKEY_free) X(PEM_read_bio_X509_CRL)              \
                          X(X509_CRL_free) X(X509_CRL_get0_lastUpdate) X(      \
                              X509_CRL_get0_nextUpdate) X(X509_CRL_verify)     \
                              X(X509_STORE_add_crl) X(X509_STORE_set_flags) X( \
                                  ASN1_TIME_to_tm) X(X509_free) X(X509_up_ref) \
                                  X(X509_get0_notBefore) X(X509_get0_notAfter) \
                                      X(X509_cmp_current_time)                 \
                                          X(X509_check_host)                   \
                                              X(X509_check_ip_asc) X(          \
                                                  X509_check_purpose)          \
                                                  X(X509_get_pubkey) X(        \
                                                      X509_verify)             \
                                                      X(X509_check_issued) X(  \
                                                          X509_STORE_add_cert) \
                                                          X(X509_digest)       \
                                                              X(EVP_sha256)

struct Api {
  void* handle = nullptr;
#define Q4T_TLS_DECLARE(name) decltype(&::name) name = nullptr;
  Q4T_TLS_SYMBOLS(Q4T_TLS_DECLARE)
#undef Q4T_TLS_DECLARE
  Api() {
    handle =
        ::dlopen(Q4T_SYSTEM_SSL_LIBRARY, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
    if (!handle) throw std::runtime_error("tls_library_unavailable");
    try {
#define Q4T_TLS_LOAD(name)                                         \
  name = reinterpret_cast<decltype(name)>(::dlsym(handle, #name)); \
  if (!name) throw std::runtime_error("tls_library_incompatible");
      Q4T_TLS_SYMBOLS(Q4T_TLS_LOAD)
#undef Q4T_TLS_LOAD
      if (OPENSSL_init_ssl(OPENSSL_INIT_NO_LOAD_CONFIG, nullptr) != 1)
        throw std::runtime_error("tls_library_initialization_failed");
    } catch (...) {
      ::dlclose(handle);
      handle = nullptr;
      throw;
    }
  }
  ~Api() {
    if (handle) ::dlclose(handle);
  }
};
#undef Q4T_TLS_SYMBOLS

bool WaitTls(int fd, int error, TlsDeadline deadline) {
  const short events = error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT;
  if (error != SSL_ERROR_WANT_READ && error != SSL_ERROR_WANT_WRITE) {
    errno = EPROTO;
    return false;
  }
  while (!stopped.load(std::memory_order_relaxed)) {
    const auto remaining =
        std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now())
            .count();
    if (remaining <= 0) {
      errno = ETIMEDOUT;
      return false;
    }
    pollfd event{fd, events, 0};
    const int ready = ::poll(&event, 1, std::min<int64_t>(remaining, 100));
    if (ready < 0 && errno == EINTR) continue;
    if (ready < 0 || (event.revents & (POLLERR | POLLNVAL))) return false;
    if (event.revents & (events | POLLHUP)) return true;
  }
  errno = ECANCELED;
  return false;
}

void RequireTls(bool ok, const char* error) {
  if (!ok) throw std::runtime_error(error);
}

int NoPassword(char*, int, int, void*) { return 0; }

int Alpn(SSL*, const unsigned char** output, unsigned char* output_size,
         const unsigned char* input, unsigned int size, void*) {
  for (unsigned int i = 0; i < size;) {
    const unsigned n = input[i++];
    if (n > size - i) return SSL_TLSEXT_ERR_ALERT_FATAL;
    if (std::string_view(reinterpret_cast<const char*>(input + i), n) ==
        "http/1.1") {
      *output = input + i;
      *output_size = n;
      return SSL_TLSEXT_ERR_OK;
    }
    i += n;
  }
  return SSL_TLSEXT_ERR_ALERT_FATAL;
}

// Reject trailing garbage and excessive chains. CA/key bytes stay in memory;
// OpenSSL never opens files from the ingress event loop or request threads.
template <class F>
void Certificates(Api& api, const std::string& pem, F consume) {
  size_t offset = 0, count = 0;
  while (offset < pem.size()) {
    while (offset < pem.size() && (pem[offset] == '\n' || pem[offset] == '\r' ||
                                   pem[offset] == ' ' || pem[offset] == '\t'))
      ++offset;
    if (offset == pem.size()) break;
    constexpr std::string_view begin = "-----BEGIN CERTIFICATE-----";
    constexpr std::string_view end = "-----END CERTIFICATE-----";
    RequireTls(std::string_view(pem).substr(offset).starts_with(begin),
               "tls_certificate_invalid");
    const auto finish = pem.find(end, offset + begin.size());
    RequireTls(finish != pem.npos && ++count <= 16, "tls_certificate_invalid");
    const auto length = finish + end.size() - offset;
    BIO* bio = api.BIO_new_mem_buf(pem.data() + offset, length);
    RequireTls(bio != nullptr, "tls_allocation_failed");
    X509* cert = api.PEM_read_bio_X509_AUX(bio, nullptr, NoPassword, nullptr);
    api.BIO_free(bio);
    RequireTls(cert != nullptr, "tls_certificate_invalid");
    try {
      consume(cert, count);
    } catch (...) {
      api.X509_free(cert);
      throw;
    }
    api.X509_free(cert);
    offset = finish + end.size();
  }
  RequireTls(count != 0, "tls_certificate_invalid");
}

bool Valid(Api& api, const X509* cert) {
  return api.X509_cmp_current_time(api.X509_get0_notBefore(cert)) < 0 &&
         api.X509_cmp_current_time(api.X509_get0_notAfter(cert)) > 0;
}
}  // namespace

struct TlsContext::Impl {
  std::shared_ptr<Api> api = std::make_shared<Api>();
  SSL_CTX* context = nullptr;
  // SSL_CTX owns its own reference; these copies support expiry checks and
  // chain verification without rereading the credential files.
  std::vector<X509*> certificates;
  std::string name;
  std::string fingerprint;
  int64_t expires_at = std::numeric_limits<int64_t>::max();
  bool checks_revocation = false;
  ~Impl() {
    for (auto* certificate : certificates) api->X509_free(certificate);
    if (context) api->SSL_CTX_free(context);
  }
};

namespace {
int ServerName(SSL* ssl, int* alert, void* argument) {
  const auto& impl = *static_cast<TlsContext::Impl*>(argument);
  const char* name =
      impl.api->SSL_get_servername(ssl, TLSEXT_NAMETYPE_host_name);
  // IP-literal clients do not send SNI. The HTTP Host check is still required.
  if (!name && Address::Parse(impl.name).family != AF_UNSPEC)
    return SSL_TLSEXT_ERR_OK;
  std::string normalized = name ? name : "";
  for (char& c : normalized)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  if (normalized == impl.name) return SSL_TLSEXT_ERR_OK;
  *alert = SSL_AD_UNRECOGNIZED_NAME;
  return SSL_TLSEXT_ERR_ALERT_FATAL;
}
}  // namespace

TlsContext::TlsContext(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
TlsContext::~TlsContext() = default;

std::shared_ptr<TlsContext> TlsContext::Create(const TlsMaterial& material,
                                               const std::string& name,
                                               bool client_certificate,
                                               std::string* error) {
  try {
    auto impl = std::make_unique<Impl>();
    auto& api = *impl->api;
    impl->name = name;
    api.ERR_clear_error();
    impl->context = api.SSL_CTX_new(api.TLS_server_method());
    RequireTls(impl->context, "tls_context_failed");
    auto* ctx = impl->context;
    const auto expiry = [&](const ASN1_TIME* until) {
      std::tm time{};
      RequireTls(until && api.ASN1_TIME_to_tm(until, &time) == 1,
                 "tls_certificate_time_invalid");
      impl->expires_at = std::min<int64_t>(impl->expires_at, ::timegm(&time));
    };
    RequireTls(api.SSL_CTX_ctrl(ctx, SSL_CTRL_SET_MIN_PROTO_VERSION,
                                TLS1_2_VERSION, nullptr) == 1 &&
                   api.SSL_CTX_ctrl(ctx, SSL_CTRL_SET_MAX_PROTO_VERSION,
                                    TLS1_3_VERSION, nullptr) == 1 &&
                   api.SSL_CTX_set_cipher_list(
                       ctx, "ECDHE+AESGCM:ECDHE+CHACHA20") == 1,
               "tls_policy_failed");
    api.SSL_CTX_set_options(ctx, SSL_OP_NO_COMPRESSION | SSL_OP_NO_TICKET |
                                     SSL_OP_NO_RENEGOTIATION);
    api.SSL_CTX_set_alpn_select_cb(ctx, Alpn, nullptr);
    api.SSL_CTX_callback_ctrl(ctx, SSL_CTRL_SET_TLSEXT_SERVERNAME_CB,
                              reinterpret_cast<void (*)()>(ServerName));
    api.SSL_CTX_ctrl(ctx, SSL_CTRL_SET_TLSEXT_SERVERNAME_ARG, 0, impl.get());
    Certificates(api, material.certificate, [&](X509* cert, size_t index) {
      RequireTls(Valid(api, cert), "tls_certificate_expired_or_not_yet_valid");
      expiry(api.X509_get0_notAfter(cert));
      if (index == 1) {
        RequireTls(
            api.X509_check_purpose(cert, X509_PURPOSE_SSL_SERVER, 0) == 1,
            "tls_certificate_purpose");
        RequireTls(api.X509_check_host(cert, name.c_str(), name.size(),
                                       X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS,
                                       nullptr) == 1 ||
                       api.X509_check_ip_asc(cert, name.c_str(), 0) == 1,
                   "tls_certificate_name_mismatch");
        RequireTls(api.SSL_CTX_use_certificate(ctx, cert) == 1,
                   "tls_certificate_invalid");
        unsigned char digest[32];
        unsigned length = 0;
        RequireTls(
            api.X509_digest(cert, api.EVP_sha256(), digest, &length) == 1 &&
                length == sizeof(digest),
            "tls_certificate_digest");
        constexpr char hex[] = "0123456789abcdef";
        for (unsigned char b : digest) {
          impl->fingerprint += hex[b >> 4];
          impl->fingerprint += hex[b & 15];
        }
      } else {
        auto* child = impl->certificates.back();
        EVP_PKEY* pubkey = api.X509_get_pubkey(cert);
        const bool valid = pubkey && api.X509_verify(child, pubkey) == 1 &&
                           api.X509_check_issued(cert, child) == X509_V_OK;
        if (pubkey) api.EVP_PKEY_free(pubkey);
        RequireTls(valid, "tls_certificate_chain_invalid");
        RequireTls(api.SSL_CTX_ctrl(ctx, SSL_CTRL_CHAIN_CERT, 1, cert) == 1,
                   "tls_certificate_chain_invalid");
      }
      // Keep a separate parsed reference without relying on opaque struct ABI.
      RequireTls(api.X509_up_ref(cert) == 1, "tls_allocation_failed");
      impl->certificates.push_back(cert);
    });
    BIO* bio = api.BIO_new_mem_buf(material.key.data(), material.key.size());
    RequireTls(bio != nullptr, "tls_allocation_failed");
    EVP_PKEY* key =
        api.PEM_read_bio_PrivateKey(bio, nullptr, NoPassword, nullptr);
    api.BIO_free(bio);
    const bool matched = key && api.SSL_CTX_use_PrivateKey(ctx, key) == 1 &&
                         api.SSL_CTX_check_private_key(ctx) == 1;
    if (key) api.EVP_PKEY_free(key);
    RequireTls(matched, "tls_private_key_mismatch_or_invalid");
    if (client_certificate) {
      X509_CRL* crl = nullptr;
      bool crl_verified = false;
      if (!material.crl.empty()) {
        BIO* source =
            api.BIO_new_mem_buf(material.crl.data(), material.crl.size());
        RequireTls(source != nullptr, "tls_allocation_failed");
        crl = api.PEM_read_bio_X509_CRL(source, nullptr, NoPassword, nullptr);
        api.BIO_free(source);
      }
      auto release_crl = [&](X509_CRL* value) {
        if (value) api.X509_CRL_free(value);
      };
      std::unique_ptr<X509_CRL, decltype(release_crl)> owned_crl(crl,
                                                                 release_crl);
      if (!material.crl.empty()) {
        RequireTls(
            crl && api.X509_CRL_get0_nextUpdate(crl) &&
                api.X509_cmp_current_time(api.X509_CRL_get0_lastUpdate(crl)) <
                    0 &&
                api.X509_cmp_current_time(api.X509_CRL_get0_nextUpdate(crl)) >
                    0,
            "tls_client_crl_invalid_or_stale");
        expiry(api.X509_CRL_get0_nextUpdate(crl));
      }
      Certificates(api, material.ca, [&](X509* cert, size_t) {
        RequireTls(
            Valid(api, cert) &&
                api.X509_check_purpose(cert, X509_PURPOSE_SSL_CLIENT, 1) == 1 &&
                api.X509_STORE_add_cert(api.SSL_CTX_get_cert_store(ctx),
                                        cert) == 1,
            "tls_client_ca_invalid");
        expiry(api.X509_get0_notAfter(cert));
        if (crl) {
          EVP_PKEY* public_key = api.X509_get_pubkey(cert);
          if (public_key) {
            crl_verified |= api.X509_CRL_verify(crl, public_key) == 1;
            api.EVP_PKEY_free(public_key);
          }
        }
      });
      if (crl) {
        RequireTls(crl_verified &&
                       api.X509_STORE_add_crl(api.SSL_CTX_get_cert_store(ctx),
                                              crl) == 1 &&
                       api.X509_STORE_set_flags(api.SSL_CTX_get_cert_store(ctx),
                                                X509_V_FLAG_CRL_CHECK) == 1,
                   "tls_client_crl_untrusted");
        impl->checks_revocation = true;
      }
      api.SSL_CTX_set_verify(
          ctx, SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT, nullptr);
      api.SSL_CTX_set_verify_depth(ctx, 8);
    } else {
      RequireTls(material.ca.empty() && material.crl.empty(),
                 "tls_unexpected_client_ca");
      api.SSL_CTX_set_verify(ctx, SSL_VERIFY_NONE, nullptr);
    }
    return std::shared_ptr<TlsContext>(new TlsContext(std::move(impl)));
  } catch (const std::exception& e) {
    // Only stable literals thrown here; never expose OpenSSL PEM/key errors.
    *error = e.what();
    return {};
  }
}

bool TlsContext::ValidNow() const {
  return std::time(nullptr) < impl_->expires_at &&
         std::all_of(
             impl_->certificates.begin(), impl_->certificates.end(),
             [&](const X509* cert) { return Valid(*impl_->api, cert); });
}
const std::string& TlsContext::Fingerprint() const {
  return impl_->fingerprint;
}
int64_t TlsContext::ExpiresAt() const { return impl_->expires_at; }
bool TlsContext::ChecksRevocation() const { return impl_->checks_revocation; }

struct TlsStream::Impl {
  std::shared_ptr<TlsContext> context;
  SSL* ssl = nullptr;
  int fd = -1;
  short poll_events = POLLIN;
  ~Impl() {
    if (ssl) context->impl_->api->SSL_free(ssl);
  }
};
TlsStream::TlsStream(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
TlsStream::~TlsStream() {
  if (!impl_->ssl) return;
  auto& api = *impl_->context->impl_->api;
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::milliseconds(100);
  for (;;) {
    api.ERR_clear_error();
    const int result = api.SSL_shutdown(impl_->ssl);
    // One-way close_notify. Never wait for the client's acknowledgement.
    if (result >= 0) break;
    const int error = api.SSL_get_error(impl_->ssl, result);
    if (error != SSL_ERROR_WANT_WRITE || !WaitTls(impl_->fd, error, deadline))
      break;
  }
}

std::unique_ptr<TlsStream> TlsContext::Stream(int fd) {
  if (!ValidNow()) return {};
  auto stream = std::make_unique<TlsStream::Impl>();
  stream->context = shared_from_this();
  stream->fd = fd;
  stream->ssl = impl_->api->SSL_new(impl_->context);
  if (!stream->ssl || impl_->api->SSL_set_fd(stream->ssl, fd) != 1) return {};
  return std::unique_ptr<TlsStream>(new TlsStream(std::move(stream)));
}

bool TlsStream::Accept(TlsDeadline deadline) {
  auto& api = *impl_->context->impl_->api;
  for (;;) {
    if (std::chrono::steady_clock::now() >= deadline) {
      errno = ETIMEDOUT;
      return false;
    }
    api.ERR_clear_error();
    const int result = api.SSL_accept(impl_->ssl);
    if (result == 1) return true;
    const int error = api.SSL_get_error(impl_->ssl, result);
    if (!WaitTls(impl_->fd, error, deadline)) return false;
  }
}

ssize_t TlsStream::Read(void* buffer, size_t size, TlsDeadline deadline) {
  impl_->poll_events = POLLIN;
  auto& api = *impl_->context->impl_->api;
  for (;;) {
    if (std::chrono::steady_clock::now() >= deadline) {
      errno = ETIMEDOUT;
      return -1;
    }
    size_t count = 0;
    api.ERR_clear_error();
    const int result = api.SSL_read_ex(impl_->ssl, buffer, size, &count);
    if (result == 1) return count;
    const int error = api.SSL_get_error(impl_->ssl, result);
    if (error == SSL_ERROR_ZERO_RETURN) return 0;
    if (!WaitTls(impl_->fd, error, deadline)) return -1;
  }
}

bool TlsStream::Write(std::string_view bytes, TlsDeadline deadline,
                      bool* started) {
  auto& api = *impl_->context->impl_->api;
  while (!bytes.empty()) {
    if (std::chrono::steady_clock::now() >= deadline) {
      errno = ETIMEDOUT;
      return false;
    }
    const size_t length = std::min<size_t>(bytes.size(), 16384);
    size_t count = 0;
    api.ERR_clear_error();
    const int result =
        api.SSL_write_ex(impl_->ssl, bytes.data(), length, &count);
    if (result == 1) {
      if (started) *started = true;
      bytes.remove_prefix(count);
    } else {
      const int error = api.SSL_get_error(impl_->ssl, result);
      if (!WaitTls(impl_->fd, error, deadline)) return false;
    }
  }
  return true;
}

bool TlsStream::Cancelled() {
  auto& api = *impl_->context->impl_->api;
  unsigned char byte;
  size_t count = 0;
  api.ERR_clear_error();
  const int result = api.SSL_peek_ex(impl_->ssl, &byte, 1, &count);
  if (result == 1) {
    impl_->poll_events = 0;  // Pipeline data belongs to the next request.
    return false;
  }
  const int error = api.SSL_get_error(impl_->ssl, result);
  impl_->poll_events = error == SSL_ERROR_WANT_WRITE ? POLLOUT : POLLIN;
  return error != SSL_ERROR_WANT_READ && error != SSL_ERROR_WANT_WRITE;
}
short TlsStream::PollEvents() const { return impl_->poll_events; }
}  // namespace q4t::service::ingress

#else
namespace q4t::service::ingress {
struct TlsContext::Impl {};
struct TlsStream::Impl {};
TlsContext::TlsContext(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
TlsContext::~TlsContext() = default;
TlsStream::TlsStream(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
TlsStream::~TlsStream() = default;
std::shared_ptr<TlsContext> TlsContext::Create(const TlsMaterial&,
                                               const std::string&, bool,
                                               std::string* error) {
  *error = "tls_build_disabled";
  return {};
}
bool TlsContext::ValidNow() const { return false; }
const std::string& TlsContext::Fingerprint() const {
  static const std::string empty;
  return empty;
}
int64_t TlsContext::ExpiresAt() const { return 0; }
bool TlsContext::ChecksRevocation() const { return false; }
std::unique_ptr<TlsStream> TlsContext::Stream(int) { return {}; }
bool TlsStream::Accept(TlsDeadline) { return false; }
ssize_t TlsStream::Read(void*, size_t, TlsDeadline) { return -1; }
bool TlsStream::Write(std::string_view, TlsDeadline, bool*) { return false; }
bool TlsStream::Cancelled() { return true; }
short TlsStream::PollEvents() const { return POLLIN; }
}  // namespace q4t::service::ingress
#endif
