#include "q4t/server/http_response.h"
#include "q4t/server/serve_fault.h"

#include <sys/socket.h>
#include <poll.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <ctime>

namespace q4t::server::detail {
namespace {
constexpr std::string_view kReplacement = "\xef\xbf\xbd";
}

void SendExceptionResponse(int fd, ResponseState state) noexcept {
  if (state == ResponseState::kTerminal || state == ResponseState::kComplete)
    return;
  constexpr char http[] =
      "HTTP/1.1 500 Internal Server Error\r\n"
      "Content-Type: application/json\r\nConnection: close\r\n\r\n"
      "{\"error\":{\"message\":\"request failed\",\"type\":\"server_error\"}}";
  constexpr char sse[] =
      "data: {\"error\":{\"message\":\"request failed\","
      "\"type\":\"server_error\"}}\r\n\r\ndata: [DONE]\r\n\r\n";
  const WriteOptions terminal{
      std::chrono::steady_clock::now() + std::chrono::milliseconds(250)};
  if (state == ResponseState::kStreaming)
    WriteAll(fd, sse, sizeof(sse) - 1, terminal);
  else
    WriteAll(fd, http, sizeof(http) - 1, terminal);
}

std::string Utf8Stream::Append(std::string_view bytes) {
  std::string input = std::move(pending_);
  pending_.clear();
  input.append(bytes);
  std::string out;
  out.reserve(input.size());
  size_t i = 0;
  while (i < input.size()) {
    const auto lead = static_cast<unsigned char>(input[i]);
    if (lead < 0x80) {
      out += input[i++];
      continue;
    }
    const size_t width = lead >= 0xc2 && lead <= 0xdf ? 2 :
                         lead >= 0xe0 && lead <= 0xef ? 3 :
                         lead >= 0xf0 && lead <= 0xf4 ? 4 : 0;
    if (!width) {
      out += kReplacement;
      ++i;
      continue;
    }
    size_t n = 1;
    for (; n < width && i + n < input.size(); ++n) {
      const auto c = static_cast<unsigned char>(input[i + n]);
      if (c < 0x80 || c > 0xbf ||
          (n == 1 && ((lead == 0xe0 && c < 0xa0) ||
                      (lead == 0xed && c > 0x9f) ||
                      (lead == 0xf0 && c < 0x90) ||
                      (lead == 0xf4 && c > 0x8f)))) break;
    }
    if (n == width) {
      out.append(input, i, width);
    } else if (i + n == input.size()) {
      pending_.assign(input, i, n);
      break;
    } else {
      out += kReplacement;
    }
    i += n;
  }
  return out;
}

std::string Utf8Stream::Finish() {
  if (pending_.empty()) return {};
  pending_.clear();
  return std::string(kReplacement);
}

std::string NormalizeUtf8(std::string_view bytes) {
  Utf8Stream stream;
  std::string out = stream.Append(bytes);
  out += stream.Finish();
  return out;
}

// Escape a string for embedding inside a JSON string literal.
std::string JsonEscape(const std::string& s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

bool WriteAll(int fd, const char* data, size_t len,
              const WriteOptions& options) {
  using Clock = std::chrono::steady_clock;
  const auto deadline = options.control
      ? std::min(options.deadline, options.control->deadline())
      : options.deadline;
  size_t off = 0;
  while (off < len) {
    if (options.control && options.control->Cancelled()) return false;
    if (Clock::now() >= deadline) return false;
    // A disconnected client must fail this request, not terminate the server.
    // Per-call nonblocking leaves the request reader's fd mode unchanged.
    const ssize_t n = ::send(fd, data + off, len - off,
                             MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        ServeFaultPoint("write.backpressure");
        const auto remaining = std::chrono::duration_cast<
            std::chrono::milliseconds>(deadline - Clock::now()).count();
        if (remaining < 0) return false;
        pollfd peer{fd, POLLOUT, 0};
        const int ready = ::poll(
            &peer, 1, static_cast<int>(std::min<int64_t>(remaining + 1, 25)));
        if (ready < 0 && errno != EINTR) return false;
        if (ready > 0 && (peer.revents & (POLLERR | POLLHUP | POLLNVAL)))
          return false;
        continue;
      }
      return false;
    }
    off += static_cast<size_t>(n);
  }
  return true;
}

bool WriteAll(int fd, const std::string& s, const WriteOptions& options) {
  return WriteAll(fd, s.data(), s.size(), options);
}


bool SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type, const WriteOptions& options) {
  std::string resp = "HTTP/1.1 " + std::to_string(code) + " " + status +
                     "\r\nContent-Type: " + content_type +
                     "\r\nContent-Length: " + std::to_string(body.size()) +
                     "\r\nConnection: close\r\n\r\n" +
                     body;
  return WriteAll(fd, resp, options);
}

void SendError(int fd, int code, const std::string& message,
               const WriteOptions& options) {
  const std::string body =
      "{\"error\":{\"message\":\"" + JsonEscape(message) +
      "\",\"type\":\"invalid_request_error\",\"code\":null}}";
  SendSimple(fd, code, "Bad Request", body, "application/json", options);
}

// OpenAI SSE data chunk for a streaming completion.
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index) {
  std::string choice;
  choice += "{\"index\":" + std::to_string(index) + ",\"delta\":{";
  if (!delta_role.empty()) {
    choice += "\"role\":\"" + delta_role + "\",";
  }
  choice += "\"content\":\"" + JsonEscape(content) + "\"}";
  if (!finish_reason.empty()) {
    choice += ",\"finish_reason\":\"" + finish_reason + "\"";
  } else {
    choice += ",\"finish_reason\":null";
  }
  choice += "}";
  std::string obj =
      "{\"id\":\"" + id + "\",\"object\":\"chat.completion.chunk\","
      "\"created\":" +
      std::to_string(std::time(nullptr)) + ",\"model\":\"" + JsonEscape(model) +
      "\",\"choices\":[" + choice + "]}";
  return "data: " + obj + "\r\n\r\n";
}

}  // namespace q4t::server::detail
