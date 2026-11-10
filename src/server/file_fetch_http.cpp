#include "file_fetch_internal.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <optional>
#include <set>
#include <stdexcept>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/lifetime.h"

namespace q4t::server::file_detail {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using service::ingress::Address;
using Deadline = Clock::time_point;

// Reuse system OpenSSL. Only the independent fetch child loads this optional
// library; no external HTTP program/service, proxy environment or user module.
#define Q4T_FETCH_TLS(X)                                                   \
  X(OPENSSL_init_ssl)                                                      \
  X(TLS_client_method)                                                     \
  X(SSL_CTX_new) X(SSL_CTX_free) X(SSL_CTX_ctrl) X(SSL_CTX_set_options)    \
      X(SSL_CTX_set_verify) X(SSL_CTX_set_default_verify_paths)            \
          X(SSL_CTX_load_verify_locations) X(SSL_new) X(SSL_free)          \
              X(SSL_set_fd) X(SSL_ctrl) X(SSL_set1_host) X(SSL_get0_param) \
                  X(X509_VERIFY_PARAM_set1_ip_asc) X(SSL_set_hostflags)    \
                      X(SSL_set_alpn_protos) X(SSL_connect)                \
                          X(SSL_get_verify_result) X(SSL_read_ex)          \
                              X(SSL_write_ex) X(SSL_get_error)             \
                                  X(ERR_clear_error)
struct TlsApi {
  void* handle = nullptr;
#define Q4T_FETCH_DECLARE(name) decltype(&::name) name = nullptr;
  Q4T_FETCH_TLS(Q4T_FETCH_DECLARE)
#undef Q4T_FETCH_DECLARE
  TlsApi() {
    handle = dlopen(Q4T_SYSTEM_FETCH_SSL_LIBRARY,
                    RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
    if (!handle) throw std::runtime_error("file_tls_dependency_unavailable");
    try {
#define Q4T_FETCH_LOAD(name)                                     \
  name = reinterpret_cast<decltype(name)>(dlsym(handle, #name)); \
  if (!name) throw std::runtime_error("file_tls_dependency_incompatible");
      Q4T_FETCH_TLS(Q4T_FETCH_LOAD)
#undef Q4T_FETCH_LOAD
      if (OPENSSL_init_ssl(OPENSSL_INIT_NO_LOAD_CONFIG, nullptr) != 1)
        throw std::runtime_error("file_tls_initialization_failed");
    } catch (...) {
      dlclose(handle);
      handle = nullptr;
      throw;
    }
  }
  ~TlsApi() {
    if (handle) dlclose(handle);
  }
};
#undef Q4T_FETCH_TLS

bool Wait(int fd, short events, Deadline deadline) {
  for (;;) {
    const auto left =
        std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now())
            .count();
    if (left <= 0) {
      errno = ETIMEDOUT;
      return false;
    }
    pollfd event{fd, events, 0};
    const int result = poll(&event, 1, std::min<int64_t>(left, 100));
    if (result < 0 && errno == EINTR) continue;
    if (result < 0 || (event.revents & POLLNVAL)) return false;
    if (event.revents & (events | POLLERR | POLLHUP)) return true;
  }
}
struct Stream {
  int fd = -1;
  std::unique_ptr<TlsApi> api;
  SSL_CTX* context = nullptr;
  SSL* tls = nullptr;
  ~Stream() {
    // No blocking shutdown/close-notify; this one-shot connection is never
    // pooled or reused and owns no application write after the GET.
    if (api) {
      api->SSL_free(tls);
      api->SSL_CTX_free(context);
    }
    if (fd >= 0) close(fd);
  }
  bool TlsWait(int result, Deadline deadline) {
    const auto error = api->SSL_get_error(tls, result);
    if (error == SSL_ERROR_WANT_READ) return Wait(fd, POLLIN, deadline);
    if (error == SSL_ERROR_WANT_WRITE) return Wait(fd, POLLOUT, deadline);
    return false;
  }
  bool Secure(const Url& url, const FileFetchOptions& options,
              Deadline deadline) {
    api = std::make_unique<TlsApi>();
    context = api->SSL_CTX_new(api->TLS_client_method());
    if (!context || api->SSL_CTX_ctrl(context, SSL_CTRL_SET_MIN_PROTO_VERSION,
                                      TLS1_2_VERSION, nullptr) != 1)
      return false;
    api->SSL_CTX_set_options(context, SSL_OP_NO_COMPRESSION);
    api->SSL_CTX_set_verify(context, SSL_VERIFY_PEER, nullptr);
    const int trusted = options.ca_file.empty()
                            ? api->SSL_CTX_set_default_verify_paths(context)
                            : api->SSL_CTX_load_verify_locations(
                                  context, options.ca_file.c_str(), nullptr);
    if (trusted != 1) return false;
    tls = api->SSL_new(context);
    if (!tls || api->SSL_set_fd(tls, fd) != 1) return false;
    api->SSL_set_hostflags(tls, X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS);
    if (Address::Parse(url.host).family == AF_UNSPEC) {
      if (api->SSL_set1_host(tls, url.host.c_str()) != 1 ||
          api->SSL_ctrl(tls, SSL_CTRL_SET_TLSEXT_HOSTNAME,
                        TLSEXT_NAMETYPE_host_name,
                        const_cast<char*>(url.host.c_str())) != 1)
        return false;
    } else if (api->X509_VERIFY_PARAM_set1_ip_asc(api->SSL_get0_param(tls),
                                                  url.host.c_str()) != 1)
      return false;
    const unsigned char protocols[] = {8,   'h', 't', 't', 'p',
                                       '/', '1', '.', '1'};
    if (api->SSL_set_alpn_protos(tls, protocols, sizeof(protocols)) != 0)
      return false;
    while (Clock::now() < deadline) {
      api->ERR_clear_error();
      const int result = api->SSL_connect(tls);
      if (result == 1) return api->SSL_get_verify_result(tls) == X509_V_OK;
      if (!TlsWait(result, deadline)) return false;
    }
    return false;
  }
  ssize_t Read(void* out, size_t size, Deadline deadline) {
    for (;;) {
      if (Clock::now() >= deadline) {
        errno = ETIMEDOUT;
        return -1;
      }
      if (tls) {
        size_t bytes = 0;
        api->ERR_clear_error();
        const int result = api->SSL_read_ex(tls, out, size, &bytes);
        if (result == 1) return bytes;
        if (api->SSL_get_error(tls, result) == SSL_ERROR_ZERO_RETURN) return 0;
        if (!TlsWait(result, deadline)) return -1;
      } else {
        const auto bytes = recv(fd, out, size, MSG_DONTWAIT);
        if (bytes >= 0) return bytes;
        if (errno == EINTR) continue;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return -1;
        if (!Wait(fd, POLLIN, deadline)) return -1;
      }
    }
  }
  bool Write(std::string_view input, Deadline deadline) {
    while (!input.empty()) {
      if (Clock::now() >= deadline) return false;
      if (tls) {
        size_t bytes = 0;
        api->ERR_clear_error();
        const int result =
            api->SSL_write_ex(tls, input.data(), input.size(), &bytes);
        if (result == 1) {
          input.remove_prefix(bytes);
          continue;
        }
        if (!TlsWait(result, deadline)) return false;
      } else {
        const auto bytes =
            send(fd, input.data(), input.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
        if (bytes > 0) {
          input.remove_prefix(bytes);
          continue;
        }
        if (bytes < 0 && errno == EINTR) continue;
        if (bytes == 0 || (errno != EAGAIN && errno != EWOULDBLOCK))
          return false;
        if (!Wait(fd, POLLOUT, deadline)) return false;
      }
    }
    return true;
  }
};
ObjectReply Failure(const char* code = "file_fetch_transport_failed") {
  return Error(502, code, "Remote file acquisition failed");
}
ObjectReply Connect(const Url& url, const FileFetchOptions& options,
                    Deadline deadline, Stream* stream) {
  if (!AllowedUrl(url, options))
    return Error(403, "file_fetch_target_denied",
                 "File target is not permitted");
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_protocol = IPPROTO_TCP;
  hints.ai_flags = AI_NUMERICSERV;
  addrinfo* addresses = nullptr;
  const auto port = std::to_string(url.port);
  if (getaddrinfo(url.host.c_str(), port.c_str(), &hints, &addresses))
    return Failure("file_fetch_dns_failed");
  ScopeExit release([&] { freeaddrinfo(addresses); });
  size_t count = 0;
  for (auto* address = addresses; address; address = address->ai_next) {
    sockaddr_storage storage{};
    if (++count > 16 || address->ai_addrlen > sizeof(storage))
      return Failure("file_fetch_dns_capacity");
    std::memcpy(&storage, address->ai_addr, address->ai_addrlen);
    if (!AllowedAddress(Address::Peer(storage, address->ai_addrlen), options))
      return Error(403, "file_fetch_target_denied",
                   "File target is not permitted");
  }
  // Connect only these validated numeric sockaddrs. There is no second DNS
  // lookup at connect/TLS time; every redirect resolves and validates afresh.
  for (auto* address = addresses; address; address = address->ai_next) {
    if (Clock::now() >= deadline) break;
    const int fd =
        socket(address->ai_family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
               IPPROTO_TCP);
    if (fd < 0) continue;
    bool connected = connect(fd, address->ai_addr, address->ai_addrlen) == 0;
    if (!connected && errno == EINPROGRESS && Wait(fd, POLLOUT, deadline)) {
      int error = 0;
      socklen_t size = sizeof(error);
      connected =
          getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) == 0 && !error;
    }
    if (connected) {
      stream->fd = fd;
      break;
    }
    close(fd);
  }
  if (stream->fd < 0) return Failure();
  if (url.tls && !stream->Secure(url, options, deadline))
    return Failure("file_fetch_tls_failed");
  return {};
}

class Reader {
 public:
  Reader(Stream& stream, Deadline deadline)
      : stream_(stream), deadline_(deadline) {}
  bool Line(std::string* line, size_t limit = 8192) {
    line->clear();
    for (;;) {
      if (begin_ == end_ && !Fill()) return false;
      const char c = buffer_[begin_++];
      if (c == '\n') {
        if (line->empty() || line->back() != '\r') return false;
        line->pop_back();
        return true;
      }
      if (line->size() == limit) return false;
      line->push_back(c);
    }
  }
  bool Exact(size_t bytes, std::string* output) {
    while (bytes) {
      if (begin_ == end_ && !Fill()) return false;
      const size_t count = std::min(bytes, end_ - begin_);
      output->append(buffer_.data() + begin_, count);
      begin_ += count;
      bytes -= count;
    }
    return true;
  }
  bool ToEnd(uint64_t limit, std::string* output) {
    for (;;) {
      if (end_ - begin_ > limit - output->size()) {
        capacity = true;
        return false;
      }
      output->append(buffer_.data() + begin_, end_ - begin_);
      begin_ = end_;
      if (!Fill()) return eof_;
    }
  }
  bool capacity = false;

 private:
  bool Fill() {
    const auto count = stream_.Read(buffer_.data(), buffer_.size(), deadline_);
    eof_ = count == 0;
    begin_ = end_ = 0;
    if (count > 0) end_ = count;
    return count > 0;
  }
  Stream& stream_;
  Deadline deadline_;
  std::array<char, 16384> buffer_{};
  size_t begin_ = 0, end_ = 0;
  bool eof_ = false;
};
struct Headers {
  int status = 0;
  bool chunked = false;
  std::optional<uint64_t> bytes;
  std::string location, mime;
};
bool ReadHeaders(Reader& reader, Headers* headers) {
  std::string line;
  if (!reader.Line(&line) || line.size() < 12 ||
      (line.substr(0, 9) != "HTTP/1.1 " && line.substr(0, 9) != "HTTP/1.0 ") ||
      (line.size() > 12 && line[12] != ' '))
    return false;
  auto parsed =
      std::from_chars(line.data() + 9, line.data() + 12, headers->status);
  if (parsed.ec != std::errc{} || parsed.ptr != line.data() + 12 ||
      headers->status < 100 || headers->status > 599)
    return false;
  for (unsigned char c : line)
    if (c < 32 || c == 127) return false;
  size_t total = line.size();
  std::set<std::string> singleton;
  std::vector<std::string> connection;
  while (reader.Line(&line)) {
    total += line.size() + 2;
    if (total > 32768) return false;
    if (line.empty()) {
      if (headers->chunked && headers->bytes) return false;
      for (const auto& name : connection)
        if (http::Protected(name) || name == "location") return false;
      return true;
    }
    std::string name;
    std::string_view value;
    if (!http::Field(line, &name, &value)) return false;
    if (name == "content-length" || name == "transfer-encoding" ||
        name == "content-type" || name == "content-encoding" ||
        name == "location") {
      if (!singleton.insert(name).second) return false;
    }
    if (name == "content-length") {
      uint64_t bytes = 0;
      const auto number =
          std::from_chars(value.data(), value.data() + value.size(), bytes);
      if (value.empty() || number.ec != std::errc{} ||
          number.ptr != value.data() + value.size())
        return false;
      headers->bytes = bytes;
    } else if (name == "transfer-encoding") {
      if (http::Lower(value) != "chunked") return false;
      headers->chunked = true;
    } else if (name == "content-encoding") {
      if (http::Lower(value) != "identity") return false;
    } else if (name == "content-type") {
      headers->mime = http::Lower(http::Trim(value.substr(0, value.find(';'))));
      if (headers->mime.size() > 255) return false;
    } else if (name == "location") {
      headers->location = value;
    } else if (name == "connection") {
      if (!http::Tokens(value, &connection)) return false;
    } else if (name == "content-range") {
      // A complete file is required; no Range request is ever made.
      return false;
    }
  }
  return false;
}
ObjectReply Body(Reader& reader, const Headers& headers, uint64_t limit,
                 std::string* bytes) {
  auto capacity = [] {
    return Error(413, "file_input_capacity_exceeded",
                 "Remote file exceeds byte budget");
  };
  if (headers.bytes) {
    if (*headers.bytes > limit) return capacity();
    bytes->reserve(*headers.bytes);
    if (!reader.Exact(*headers.bytes, bytes))
      return Failure("file_fetch_truncated");
  } else if (headers.chunked) {
    std::string line;
    size_t control = 0;
    for (;;) {
      size_t chunk = 0;
      if (!reader.Line(&line) || !http::ChunkSize(line, &chunk))
        return Failure("file_fetch_framing_invalid");
      control += line.size() + 4;
      if (control > (2U << 20)) return Failure("file_fetch_framing_capacity");
      if (!chunk) break;
      if (chunk > limit - bytes->size()) return capacity();
      if (!reader.Exact(chunk, bytes) || !reader.Line(&line, 2) ||
          !line.empty())
        return Failure("file_fetch_truncated");
    }
    size_t trailer = 0;
    for (;;) {
      if (!reader.Line(&line)) return Failure("file_fetch_truncated");
      trailer += line.size() + 2;
      if (trailer > 32768) return Failure("file_fetch_framing_capacity");
      if (line.empty()) break;
      std::string name;
      std::string_view value;
      if (!http::Field(line, &name, &value) || http::Protected(name) ||
          name == "location")
        return Failure("file_fetch_framing_invalid");
    }
  } else if (!reader.ToEnd(limit, bytes)) {
    return reader.capacity ? capacity() : Failure("file_fetch_truncated");
  }
  return {};
}
}  // namespace

ObjectReply Fetch(const Url& original, const FileFetchOptions& options,
                  uint64_t max_bytes, Deadline deadline,
                  FileFetchReceipt* result) {
  *result = {};
  FileFetchReceipt next;
  Url url = original;
  std::set<std::string> visited;
  for (int hop = 0; hop <= options.max_redirects; ++hop) {
    if (!visited.insert(url.Text()).second)
      return Failure("file_fetch_redirect_loop");
    next.urls.push_back(url.Text());
    Stream stream;
    auto reply = Connect(url, options, deadline, &stream);
    if (!reply.ok()) return reply;
    const std::string request =
        "GET " + url.target + " HTTP/1.1\r\nHost: " + url.authority +
        "\r\nUser-Agent: q4t-file-input/1\r\nAccept: */*\r\n"
        "Accept-Encoding: identity\r\nConnection: close\r\n\r\n";
    if (!stream.Write(request, deadline)) return Failure();
    Reader reader(stream, deadline);
    Headers headers;
    int information = 0;
    for (;;) {
      if (!ReadHeaders(reader, &headers))
        return Failure("file_fetch_framing_invalid");
      if (headers.status >= 200) break;
      if (headers.status == 101 || ++information > 4 || headers.bytes ||
          headers.chunked)
        return Failure("file_fetch_framing_invalid");
      headers = {};
    }
    if (headers.status == 301 || headers.status == 302 ||
        headers.status == 303 || headers.status == 307 ||
        headers.status == 308) {
      Url redirected;
      if (hop == options.max_redirects)
        return Failure("file_fetch_redirect_limit");
      if (!Redirect(url, headers.location, &redirected))
        return Failure("file_fetch_redirect_invalid");
      url = std::move(redirected);
      continue;
    }
    if (headers.status != 200) {
      auto failed =
          Error(400, "file_fetch_http_error", "Remote file is not available");
      failed.value = Object({{"upstream_status", Number(headers.status)}});
      return failed;
    }
    reply = Body(reader, headers, max_bytes, &next.content.bytes);
    if (!reply.ok()) return reply;
    next.content.mime_type = std::move(headers.mime);
    // URL basename is display metadata, never a path opened by a decoder.
    const auto path = url.target.substr(0, url.target.find('?'));
    next.content.filename = path.substr(path.rfind('/') + 1);
    if (next.content.filename.size() > 1024) next.content.filename.clear();
    *result = std::move(next);
    return {};
  }
  return Failure("file_fetch_redirect_limit");
}
}  // namespace q4t::server::file_detail
