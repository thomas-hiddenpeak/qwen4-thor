#include "q4t/server/http_request.h"
#include "q4t/server/application_routes.h"

#include <unistd.h>
#include <poll.h>
#include <sys/socket.h>

#include <cerrno>
#include <chrono>
#include <algorithm>

#include <charconv>
#include <string_view>

#include "q4t/server/client_identity.h"
#include "q4t/server/http_framing.h"

namespace q4t::server {
namespace {

using http::IsToken;
using http::Trim;

}  // namespace

bool ReadHttpRequest(int fd, std::string* method, std::string* path,
                     std::string* body, int timeout_ms, HttpBodyLease* lease,
                     int* failure_status, HttpRequestHeaders* headers,
                     const std::function<bool()>& admit,
                     const HttpReceive& receive, HttpRequestState* state,
                     const HttpContinue& send_continue) {
  HttpRequestHeaders local;
  if (!headers) headers = &local;
  *headers = {};
  method->clear();
  path->clear();
  body->clear();
  headers->failure = HttpReceiveFailure::kMalformed;
  if (failure_status) *failure_status = 400;
  if (timeout_ms <= 0) return false;
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
  bool body_phase = false;
  const auto read_deadline = [&] {
    if (failure_status) *failure_status = 408;
    headers->failure = body_phase ? HttpReceiveFailure::kBodyDeadline
                                  : HttpReceiveFailure::kHeaderDeadline;
  };
  const auto read_some = [&](char* data, size_t size) -> ssize_t {
    if (receive) {
      const ssize_t n = receive(data, size, deadline);
      if (n < 0 && errno == ETIMEDOUT) {
        read_deadline();
      } else if (n < 0) {
        headers->failure = HttpReceiveFailure::kSocket;
        headers->system_error = errno;
      } else if (n == 0) {
        headers->failure = body_phase ? HttpReceiveFailure::kBodyEof
                                      : HttpReceiveFailure::kHeaderEof;
      }
      return n;
    }
    for (;;) {
      const auto remaining =
          std::chrono::duration_cast<std::chrono::milliseconds>(
              deadline - std::chrono::steady_clock::now())
              .count();
      if (remaining <= 0) {
        read_deadline();
        return -1;
      }
      pollfd event{fd, POLLIN, 0};
      const int ready = ::poll(&event, 1, static_cast<int>(remaining));
      if (ready < 0 && errno == EINTR) continue;
      if (ready == 0) {
        read_deadline();
        return -1;
      }
      if (ready < 0) {
        headers->failure = HttpReceiveFailure::kSocket;
        headers->system_error = errno;
        return -1;
      }
      const ssize_t n = ::recv(fd, data, size, MSG_DONTWAIT);
      if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        continue;
      if (n < 0) {
        headers->failure = HttpReceiveFailure::kSocket;
        headers->system_error = errno;
      } else if (n == 0) {
        headers->failure = body_phase ? HttpReceiveFailure::kBodyEof
                                      : HttpReceiveFailure::kHeaderEof;
      }
      return n;
    }
  };
  constexpr size_t kMaxHeader = 64 * 1024;
  std::string buf = state ? std::move(state->pending) : std::string();
  if (state) state->pending.clear();
  char tmp[4096];
  size_t header_end = std::string::npos;
  while ((header_end = buf.find("\r\n\r\n")) == std::string::npos) {
    if (buf.size() >= kMaxHeader) {
      headers->failure = HttpReceiveFailure::kHeaderTooLarge;
      return false;
    }
    const ssize_t n = read_some(tmp, std::min(sizeof(tmp), kMaxHeader - buf.size()));
    if (n <= 0) return false;
    buf.append(tmp, static_cast<size_t>(n));
  }
  const size_t head_len = header_end + 4;
  if (head_len > kMaxHeader) {
    headers->failure = HttpReceiveFailure::kHeaderTooLarge;
    return false;
  }
  const std::string_view head(buf.data(), header_end + 2);
  const size_t line_end = head.find("\r\n");
  if (line_end == std::string_view::npos) return false;
  const std::string_view line = head.substr(0, line_end);
  const size_t first = line.find(' ');
  const size_t last = line.rfind(' ');
  if (first == std::string_view::npos || first == 0 || last <= first + 1)
    return false;
  const auto verb = line.substr(0, first);
  const auto target = line.substr(first + 1, last - first - 1);
  const auto version = line.substr(last + 1);
  if (version != "HTTP/1.0" && version != "HTTP/1.1") return false;
  for (unsigned char c : verb)
    if (!IsToken(c)) return false;
  if (target.front() != '/') return false;
  for (unsigned char c : target)
    if (c <= 0x20 || c >= 0x7F || c == '#') return false;

  size_t length = 0;
  bool seen_length = false, seen_host = false;
  bool seen_id = false, seen_type = false;
  bool seen_authorization = false;
  bool seen_client = false, seen_gateway = false;
  bool seen_origin = false, seen_cors_method = false, seen_cors_headers = false;
  bool seen_transfer = false, seen_expect = false;
  std::vector<std::string> connection, declared_trailers;
  size_t pos = line_end + 2;
  while (pos < head.size()) {
    const size_t end = head.find("\r\n", pos);
    if (end == std::string_view::npos) return false;
    const auto field = head.substr(pos, end - pos);
    const size_t colon = field.find(':');
    if (colon == std::string_view::npos || colon == 0) return false;
    std::string name(field.substr(0, colon));
    for (char& c : name) {
      if (!IsToken(static_cast<unsigned char>(c))) return false;
      if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    }
    const auto value = Trim(field.substr(colon + 1));
    for (unsigned char c : value)
      if ((c < 0x20 && c != '\t') || c == 0x7F) return false;
    if (name == "content-length") {
      if (seen_length || value.empty()) return false;
      seen_length = true;
      for (char c : value)
        if (c < '0' || c > '9') return false;
      const auto result =
          std::from_chars(value.data(), value.data() + value.size(), length);
      if (result.ec == std::errc::result_out_of_range ||
          (result.ec == std::errc{} && length > kChatBodyBytes)) {
        if (failure_status) *failure_status = 413;
        headers->failure = HttpReceiveFailure::kBodyTooLarge;
        return false;
      }
      if (result.ec != std::errc{} || result.ptr != value.data() + value.size())
        return false;
    } else if (name == "transfer-encoding") {
      if (seen_transfer || version != "HTTP/1.1") return false;
      seen_transfer = true;
      if (http::Lower(value) != "chunked") return false;
      headers->chunked = true;
    } else if (name == "expect") {
      if (seen_expect) return false;
      seen_expect = true;
      if (version == "HTTP/1.1") {
        if (http::Lower(value) != "100-continue") {
          if (failure_status) *failure_status = 417;
          headers->error_code = "expectation_failed";
          return false;
        }
        headers->expect_continue = true;
      }
    } else if (name == "connection") {
      if (!http::Tokens(value, &connection)) return false;
    } else if (name == "trailer") {
      if (!http::Tokens(value, &declared_trailers)) return false;
    } else if (name == "host") {
      if (seen_host || value.empty()) return false;
      seen_host = true;
      headers->host = value;
    } else if (name == "authorization") {
      if (seen_authorization || value.empty()) {
        headers->error_code = "invalid_authorization_header";
        return false;
      }
      seen_authorization = true;
      headers->authorization = value;
    } else if (name == "x-request-id") {
      if (seen_id || !ValidHttpRequestId(value)) {
        headers->request_id.clear();
        headers->error_code = "invalid_request_id_header";
        headers->error_param = "X-Request-Id";
        return false;
      }
      seen_id = true;
      headers->request_id = value;
    } else if (name == "x-q4t-client-id") {
      if (seen_client || !ValidClientId(value)) {
        headers->error_code = "invalid_client_header";
        return false;
      }
      seen_client = true;
      headers->client_id = value;
    } else if (name == "x-q4t-gateway-token") {
      if (seen_gateway || value.size() != 64) {
        headers->error_code = "invalid_client_header";
        return false;
      }
      for (char c : value) {
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
          headers->error_code = "invalid_client_header";
          return false;
        }
      }
      seen_gateway = true;
      headers->gateway_token = value;
    } else if (name == "origin" || name == "access-control-request-method" ||
               name == "access-control-request-headers") {
      bool& seen = name == "origin" ? seen_origin
                   : name == "access-control-request-method"
                       ? seen_cors_method
                       : seen_cors_headers;
      if (seen || value.empty() || value.size() > 2048) return false;
      seen = true;
      auto& destination = name == "origin" ? headers->origin
                          : name == "access-control-request-method"
                              ? headers->cors_method
                              : headers->cors_headers;
      destination = value;
    } else if (name == "content-type") {
      if (seen_type) {
        headers->error_code = "duplicate_content_type";
        headers->error_param = "Content-Type";
        return false;
      }
      seen_type = true;
      headers->content_type = value;
    }
    pos = end + 2;
  }
  if (version == "HTTP/1.1" && !seen_host) return false;
  if (seen_length && seen_transfer) return false;
  for (const auto& option : connection)
    if (http::Protected(option)) return false;
  for (const auto& trailer : declared_trailers)
    if (http::Protected(trailer) || trailer == "connection" ||
        trailer == "keep-alive" || trailer == "upgrade" ||
        std::find(connection.begin(), connection.end(), trailer) !=
            connection.end())
      return false;
  if (!declared_trailers.empty() && !headers->chunked) return false;
  const auto option = [&](const char* token) {
    return std::find(connection.begin(), connection.end(), token) !=
           connection.end();
  };
  headers->keep_alive =
      !option("close") && (version == "HTTP/1.1" || option("keep-alive"));
  *method = verb;
  *path = target;
  headers->content_length = length;
  if (admit) {
    headers->failure = HttpReceiveFailure::kAdmission;
    if (!admit()) return false;
    headers->failure = HttpReceiveFailure::kMalformed;
  }
  // Admission may canonicalize a configured public path prefix. Use the owned
  // admitted path, never a view into buf across append/reallocation.
  const size_t route_limit =
      IsStoredChatTarget(*path) || *path == "/v1/chat/preflight" ||
              *path == "/v1/completions" || IsResponsesTarget(*path) ||
              IsConversationTarget(*path)
          ? kChatBodyBytes : 4096;
  const auto reserve = [&](size_t bytes) {
    if (bytes > std::min(route_limit, headers->body_limit)) {
      if (failure_status) *failure_status = 413;
      headers->failure = HttpReceiveFailure::kBodyTooLarge;
      headers->error_code = "body_limit_exceeded";
      return false;
    }
    if (lease && !lease->GrowTo(bytes)) {
      if (failure_status) *failure_status = 503;
      headers->failure = HttpReceiveFailure::kBodyCapacity;
      headers->error_code = "body_capacity_exhausted";
      headers->error_param = "body";
      return false;
    }
    return true;
  };
  if (!reserve(length)) return false;
  if (headers->expect_continue && (length || headers->chunked)) {
    bool sent = false;
    if (send_continue) {
      sent = send_continue(deadline);
    } else if (!receive) {
      std::string_view interim = "HTTP/1.1 100 Continue\r\n\r\n";
      while (!interim.empty()) {
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (left <= 0) break;
        pollfd event{fd, POLLOUT, 0};
        const int ready = ::poll(&event, 1, static_cast<int>(left));
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0) break;
        const auto n = ::send(fd, interim.data(), interim.size(),
                              MSG_NOSIGNAL | MSG_DONTWAIT);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n <= 0) break;
        interim.remove_prefix(static_cast<size_t>(n));
      }
      sent = interim.empty();
    }
    if (!sent) {
      headers->failure = HttpReceiveFailure::kSocket;
      headers->error_code = "continue_write_failed";
      return false;
    }
  }
  body_phase = true;
  buf.erase(0, head_len);
  const auto consume = [&](size_t bytes) {
    const size_t buffered = std::min(bytes, buf.size());
    body->append(buf.data(), buffered);
    buf.erase(0, buffered);
    bytes -= buffered;
    while (bytes) {
      const ssize_t n = read_some(tmp, std::min(bytes, sizeof(tmp)));
      if (n <= 0) return false;
      body->append(tmp, static_cast<size_t>(n));
      bytes -= static_cast<size_t>(n);
    }
    return true;
  };
  if (!headers->chunked) {
    if (!consume(length)) return false;
  } else {
    size_t metadata = 0;
    const auto line = [&](std::string* output) {
      size_t end = 0;
      while ((end = buf.find("\r\n")) == buf.npos) {
        if (buf.size() >= 8192) return false;
        const ssize_t n = read_some(tmp, std::min(sizeof(tmp), 8192 - buf.size()));
        if (n <= 0) return false;
        buf.append(tmp, static_cast<size_t>(n));
      }
      if (end + 2 > 8192 || end + 2 > kMaxHeader - metadata) return false;
      metadata += end + 2;
      *output = buf.substr(0, end);
      buf.erase(0, end + 2);
      return true;
    };
    std::string chunk;
    for (;;) {
      size_t bytes = 0;
      if (!line(&chunk) || !http::ChunkSize(chunk, &bytes)) return false;
      if (!bytes) break;
      if (bytes > kChatBodyBytes) {
        if (failure_status) *failure_status = 413;
        headers->failure = HttpReceiveFailure::kBodyTooLarge;
        return false;
      }
      if (!reserve(body->size() + bytes) || !consume(bytes)) return false;
      if (!line(&chunk) || !chunk.empty()) return false;
    }
    for (;;) {
      if (!line(&chunk)) return false;
      if (chunk.empty()) break;
      std::string name;
      std::string_view value;
      if (headers->trailers.size() >= 128 ||
          !http::Field(chunk, &name, &value) || http::Protected(name) ||
          name == "connection" || name == "keep-alive" || name == "upgrade" ||
          std::find(connection.begin(), connection.end(), name) !=
              connection.end()) return false;
      headers->trailers.emplace_back(name, value);
    }
    headers->content_length = body->size();
  }
  if (state) state->pending = std::move(buf);
  headers->failure = HttpReceiveFailure::kNone;
  return true;
}

}  // namespace q4t::server
