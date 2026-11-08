#include "q4t/server/http_response.h"
#include "q4t/server/http_request.h"
#include "q4t/server/serve_fault.h"

#include <sys/socket.h>
#include <sys/random.h>
#include <poll.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <ctime>
#include <stdexcept>

namespace q4t::server::detail {
namespace {
constexpr std::string_view kReplacement = "\xef\xbf\xbd";
thread_local HttpResponseContext* current_response = nullptr;

std::string RequestIdHeader(int fd) {
  const auto* context = HttpResponseContext::Current(fd);
  return context ? "X-Request-Id: " + context->TraceId() + "\r\n" : "";
}
}  // namespace

void SendExceptionResponse(int fd, ResponseState state,
                           ExceptionKind kind) noexcept {
  // Fixed storage also works while all C++ heap allocations keep failing.
  // If the transport itself throws, abandon this response without retrying.
  try {
    auto* context = HttpResponseContext::Current(fd);
    const bool memory = kind == ExceptionKind::kHostMemory;
    const char* code = memory ? "host_memory_exhausted" : "internal_error";
    const char* type = memory ? "service_unavailable_error" : "server_error";
    if (context) {
      context->ObserveError(memory ? 503 : 500, "request failed", code);
      context->StopHeartbeats();
      state = *context->State();
      std::fprintf(stderr, "[q4t][http] trace_id=%s error=%s\n",
                   context->TraceId().c_str(), code);
    }
    if (state == ResponseState::kHeadersStarted ||
        state == ResponseState::kTerminal || state == ResponseState::kComplete)
      return;
    char body[256];
    const int body_size = std::snprintf(
        body, sizeof(body),
        "{\"error\":{\"message\":\"request failed\",\"type\":\"%s\","
        "\"code\":\"%s\",\"param\":null}}",
        type, code);
    if (body_size <= 0 || static_cast<size_t>(body_size) >= sizeof(body))
      return;
    char response[1024];
    int size = 0;
    if (state == ResponseState::kStreaming && context &&
        context->ResponsesSequence()) {
      // This path must not call the allocation-using snapshot renderer.
      auto& sequence = *context->ResponsesSequence();
      size = std::snprintf(
          response, sizeof(response),
          "event: error\r\ndata: {\"type\":\"error\",\"code\":\"%s\","
          "\"message\":\"request failed\",\"param\":null,"
          "\"sequence_number\":%lld}\r\n\r\n", code,
          static_cast<long long>(sequence));
      if (sequence < INT64_MAX) ++sequence;
    } else {
      size = state == ResponseState::kStreaming
            ? std::snprintf(response, sizeof(response),
                            "data: %s\r\n\r\ndata: [DONE]\r\n\r\n", body)
            : std::snprintf(response, sizeof(response),
                            "HTTP/1.1 %s\r\n%s%s%s"
                            "Content-Type: application/json\r\n"
                            "Connection: close\r\n\r\n%s",
                            memory ? "503 Service Unavailable"
                                   : "500 Internal Server Error",
                            context ? "X-Request-Id: " : "",
                            context ? context->TraceId().c_str() : "",
                            context ? "\r\n" : "", body);
    }
    if (size <= 0 || static_cast<size_t>(size) >= sizeof(response)) return;
    const WriteOptions terminal{std::chrono::steady_clock::now() +
                                std::chrono::milliseconds(250)};
    if (context) {
      context->BeforeTerminal();
      *context->State() = ResponseState::kTerminal;
    }
    WriteAll(fd, response, static_cast<size_t>(size), terminal);
  } catch (...) {
    // No allocation, second error response, or exception may escape fallback.
  }
}

std::string Utf8Stream::Append(std::string_view bytes,
                               std::vector<Utf8Span>* spans) {
  const size_t base = input_bytes_ - pending_.size();
  input_bytes_ += bytes.size();
  std::string input = std::move(pending_);
  pending_.clear();
  input.append(bytes);
  std::string out;
  out.reserve(input.size());
  const auto record = [&](size_t begin, size_t end, size_t output_begin) {
    if (spans)
      spans->push_back({base + begin, base + end, output_bytes_ + output_begin,
                        output_bytes_ + out.size()});
  };
  size_t i = 0;
  while (i < input.size()) {
    const auto output_begin = out.size();
    const auto lead = static_cast<unsigned char>(input[i]);
    if (lead < 0x80) {
      out += input[i];
      record(i, i + 1, output_begin);
      ++i;
      continue;
    }
    const size_t width = lead >= 0xc2 && lead <= 0xdf   ? 2
                         : lead >= 0xe0 && lead <= 0xef ? 3
                         : lead >= 0xf0 && lead <= 0xf4 ? 4
                                                        : 0;
    if (!width) {
      out += kReplacement;
      record(i, i + 1, output_begin);
      ++i;
      continue;
    }
    size_t n = 1;
    for (; n < width && i + n < input.size(); ++n) {
      const auto c = static_cast<unsigned char>(input[i + n]);
      if (c < 0x80 || c > 0xbf ||
          (n == 1 &&
           ((lead == 0xe0 && c < 0xa0) || (lead == 0xed && c > 0x9f) ||
            (lead == 0xf0 && c < 0x90) || (lead == 0xf4 && c > 0x8f))))
        break;
    }
    if (n == width) {
      out.append(input, i, width);
    } else if (i + n == input.size()) {
      pending_.assign(input, i, n);
      break;
    } else {
      out += kReplacement;
    }
    record(i, i + n, output_begin);
    i += n;
  }
  output_bytes_ += out.size();
  return out;
}

std::string Utf8Stream::Finish(std::vector<Utf8Span>* spans) {
  if (pending_.empty()) return {};
  if (spans)
    spans->push_back({input_bytes_ - pending_.size(), input_bytes_,
                      output_bytes_, output_bytes_ + kReplacement.size()});
  pending_.clear();
  output_bytes_ += kReplacement.size();
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
      case '"':
        out += "\\\"";
        break;
      case '\\':
        out += "\\\\";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      case '\b':
        out += "\\b";
        break;
      case '\f':
        out += "\\f";
        break;
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

namespace {
HttpWriteFailure CancelWriteReason(const RequestControl& control) {
  const std::string_view reason(control.Reason());
  if (reason == "deadline_exceeded") return HttpWriteFailure::kRequestDeadline;
  if (reason == "explicit_cancel") return HttpWriteFailure::kExplicitCancel;
  if (reason == "client_disconnected") return HttpWriteFailure::kPeerClosed;
  if (reason == "server_shutdown") return HttpWriteFailure::kShutdown;
  return HttpWriteFailure::kRequestFailure;
}

bool WriteAllImpl(int fd, const char* data, size_t len,
                  const WriteOptions& options, ResponseState* header_state,
                  HttpWriteObservation* observation = nullptr) {
  HttpWriteObservation local;
  if (!observation) observation = &local;
  *observation = {};
  const auto fail = [&](HttpWriteFailure reason, int system_error = 0,
                        short events = 0) {
    observation->reason = reason;
    observation->system_error = system_error;
    observation->poll_events = events;
    return false;
  };
  using Clock = std::chrono::steady_clock;
  const auto deadline =
      options.control ? std::min(options.deadline, options.control->deadline())
                      : options.deadline;
  const auto timeout_reason =
      options.control && options.control->deadline() <= options.deadline
          ? HttpWriteFailure::kRequestDeadline
          : HttpWriteFailure::kWriteDeadline;
  size_t off = 0;
  while (off < len) {
    if (options.control && options.control->Cancelled())
      return fail(CancelWriteReason(*options.control));
    if (Clock::now() >= deadline) return fail(timeout_reason);
    // A disconnected client must fail this request, not terminate the server.
    // Per-call nonblocking leaves the request reader's fd mode unchanged.
    size_t remaining_bytes = len - off;
#ifdef Q4T_SERVE_FAULT_INJECTION
    const char* fault = std::getenv("Q4T_SERVE_FAULT");
    if (header_state && fault &&
        std::strcmp(fault, "handler.partial_header") == 0)
      remaining_bytes = std::min(remaining_bytes, size_t{8});
#endif
    const ssize_t n =
        ::send(fd, data + off, remaining_bytes, MSG_NOSIGNAL | MSG_DONTWAIT);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        ServeFaultPoint("write.backpressure");
        const auto remaining =
            std::chrono::duration_cast<std::chrono::milliseconds>(deadline -
                                                                  Clock::now())
                .count();
        if (remaining < 0) return fail(timeout_reason);
        pollfd peer{fd, POLLOUT, 0};
        const int ready = ::poll(
            &peer, 1, static_cast<int>(std::min<int64_t>(remaining + 1, 25)));
        if (ready < 0 && errno != EINTR)
          return fail(HttpWriteFailure::kSocket, errno);
        if (ready > 0 && (peer.revents & (POLLERR | POLLHUP | POLLNVAL))) {
          const auto reason =
              (peer.revents & POLLHUP) && !(peer.revents & POLLNVAL)
                  ? HttpWriteFailure::kPeerClosed
                  : HttpWriteFailure::kSocket;
          return fail(reason, 0, peer.revents);
        }
        continue;
      }
      const int error = n < 0 ? errno : 0;
      const auto reason =
          error == EPIPE || error == ECONNRESET || error == ENOTCONN
              ? HttpWriteFailure::kPeerClosed
              : HttpWriteFailure::kSocket;
      return fail(reason, error);
    }
    off += static_cast<size_t>(n);
    observation->transferred = off;
    if (header_state) {
      *header_state = ResponseState::kHeadersStarted;
      ServeFaultPoint("handler.partial_header");
    }
  }
  return true;
}
}  // namespace

std::string NewRequestId(std::string_view prefix) {
  unsigned char random[16];
  size_t offset = 0;
  while (offset < sizeof(random)) {
    const auto size = ::getrandom(random + offset, sizeof(random) - offset, 0);
    if (size < 0 && errno == EINTR) continue;
    if (size <= 0) throw std::runtime_error("request identity entropy failed");
    offset += static_cast<size_t>(size);
  }
  std::string result(prefix);
  for (unsigned char byte : random) {
    result += "0123456789abcdef"[byte >> 4];
    result += "0123456789abcdef"[byte & 15];
  }
  return result;
}

HttpResponseContext::HttpResponseContext(int fd, HttpTransportMetrics* metrics)
    : fd_(fd),
      previous_(current_response),
      trace_id_(NewRequestId()),
      metrics_(metrics) {
  current_response = this;
}

HttpResponseContext::~HttpResponseContext() {
  StopHeartbeats();
  // Both reasons are finalized after join and before the socket is closed.
  // A read rejection may also fail to write; these are separate observations.
  if (metrics_ && receive_failure_ != HttpReceiveFailure::kNone) {
    const auto index = static_cast<size_t>(receive_failure_);
    metrics_->receive[index].fetch_add(1, std::memory_order_relaxed);
    std::fprintf(stderr,
                 "[q4t][http-receive] trace_id=%s reason=%s status=%d "
                 "system_error=%d\n",
                 trace_id_.c_str(), kHttpReceiveReasons[index], receive_status_,
                 receive_system_error_);
  }
  if (metrics_ && write_failure_.reason != HttpWriteFailure::kNone) {
    const auto reason = static_cast<size_t>(write_failure_.reason);
    const auto origin = static_cast<size_t>(write_failure_.origin);
    metrics_->write[origin][reason].fetch_add(1, std::memory_order_relaxed);
    std::fprintf(stderr,
                 "[q4t][http-write] trace_id=%s reason=%s origin=%s "
                 "transferred=%zu system_error=%d poll_events=%d\n",
                 trace_id_.c_str(), kHttpWriteReasons[reason],
                 kHttpWriteOrigins[origin], write_failure_.transferred,
                 write_failure_.system_error, write_failure_.poll_events);
  }
  current_response = previous_;
}

void HttpResponseContext::RecordReceiveFailure(HttpReceiveFailure reason,
                                               int status, int system_error) {
  const auto index = static_cast<size_t>(reason);
  if (index == 0 || index >= kHttpReceiveReasons.size() ||
      receive_failure_ != HttpReceiveFailure::kNone)
    return;
  receive_failure_ = reason;
  receive_status_ = status;
  receive_system_error_ = system_error;
}

HttpWriteObservation HttpResponseContext::WriteFailure() {
  const std::lock_guard<std::mutex> lock(write_mu_);
  return write_failure_;
}

void HttpResponseContext::RecordWriteFailure(HttpWriteObservation value,
                                             HttpWriteOrigin origin) {
  if (write_failure_.reason != HttpWriteFailure::kNone) return;
  value.origin = origin;
  write_failure_ = value;
}

HttpResponseContext* HttpResponseContext::Current(int fd) {
  return current_response && current_response->fd_ == fd ? current_response
                                                         : nullptr;
}

bool HttpResponseContext::SetTraceId(std::string_view id) {
  if (state_ != ResponseState::kUnsent || !ValidHttpRequestId(id)) return false;
  trace_id_ = id;
  return true;
}

bool HttpResponseContext::Write(const char* data, size_t size,
                                const WriteOptions& options,
                                ResponseState* header_state) {
  const std::lock_guard<std::mutex> lock(write_mu_);
  if (write_failure_.reason != HttpWriteFailure::kNone) return false;
  HttpWriteObservation observation;
  try {
    if (!WriteAllImpl(fd_, data, size, options, header_state, &observation)) {
      RecordWriteFailure(observation, HttpWriteOrigin::kHandler);
      return false;
    }
  } catch (...) {
    observation.reason = HttpWriteFailure::kException;
    RecordWriteFailure(observation, HttpWriteOrigin::kHandler);
    throw;
  }
  last_write_ = std::chrono::steady_clock::now();
  wake_.notify_all();
  return true;
}

void HttpResponseContext::StartHeartbeats(
    std::shared_ptr<RequestControl> control,
    std::chrono::milliseconds interval) {
  if (state_ != ResponseState::kStreaming || heartbeat_.joinable() ||
      interval.count() <= 0)
    throw std::logic_error("invalid heartbeat state");
  control_ = std::move(control);
  interval_ = interval;
  heartbeat_ = std::thread([this] {
    try {
      HeartbeatLoop();
    } catch (...) {
      const std::lock_guard<std::mutex> lock(write_mu_);
      RecordWriteFailure({.reason = HttpWriteFailure::kException},
                         HttpWriteOrigin::kHeartbeat);
      control_->Cancel(RequestControl::State::kFailure);
    }
  });
}

void HttpResponseContext::StopHeartbeats() {
  {
    const std::lock_guard<std::mutex> lock(write_mu_);
    stop_ = true;
    wake_.notify_all();
  }
  if (heartbeat_.joinable()) heartbeat_.join();
}

void HttpResponseContext::HeartbeatLoop() {
  std::unique_lock<std::mutex> lock(write_mu_);
  while (!stop_ && write_failure_.reason == HttpWriteFailure::kNone) {
    const auto due = last_write_ + interval_;
    wake_.wait_until(lock, due);
    if (stop_ || write_failure_.reason != HttpWriteFailure::kNone ||
        control_->Cancelled())
      return;
    if (std::chrono::steady_clock::now() < last_write_ + interval_) continue;
    constexpr char comment[] = ": keep-alive\r\n\r\n";
    // A heartbeat cannot pin the handler for the normal 30 second write
    // window. Partial failed frames close the stream; never append a terminal.
    const WriteOptions options{
        std::chrono::steady_clock::now() + std::chrono::seconds(1),
        control_.get()};
    HttpWriteObservation observation;
    try {
      if (!WriteAllImpl(fd_, comment, sizeof(comment) - 1, options, nullptr,
                        &observation)) {
        RecordWriteFailure(observation, HttpWriteOrigin::kHeartbeat);
        control_->Cancel(RequestControl::State::kDisconnect);
        return;
      }
    } catch (...) {
      observation.reason = HttpWriteFailure::kException;
      RecordWriteFailure(observation, HttpWriteOrigin::kHeartbeat);
      throw;
    }
    last_write_ = std::chrono::steady_clock::now();
  }
}

bool WriteAll(int fd, const char* data, size_t len,
              const WriteOptions& options) {
  if (auto* context = HttpResponseContext::Current(fd))
    return context->Write(data, len, options);
  return WriteAllImpl(fd, data, len, options, nullptr);
}

bool BeginStreamingResponse(int fd, ResponseState* state,
                            const WriteOptions& options) {
  const std::string header =
      "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
      "Cache-Control: no-cache\r\nX-Accel-Buffering: no\r\n" +
      RequestIdHeader(fd) + "Connection: close\r\n\r\n";
  ServeFaultPoint("handler.before_header");
  auto* context = HttpResponseContext::Current(fd);
  const bool written =
      context ? context->Write(header.data(), header.size(), options, state)
              : WriteAllImpl(fd, header.data(), header.size(), options, state);
  if (!written) return false;
  *state = ResponseState::kStreaming;
  ServeFaultPoint("handler.after_header");
  return true;
}

bool WriteAll(int fd, const std::string& s, const WriteOptions& options) {
  return WriteAll(fd, s.data(), s.size(), options);
}

bool SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type, const WriteOptions& options,
                std::string_view allow) {
  std::string resp =
      "HTTP/1.1 " + std::to_string(code) + " " + status +
      "\r\nContent-Type: " + content_type +
      "\r\nContent-Length: " + std::to_string(body.size()) + "\r\n" +
      RequestIdHeader(fd) +
      (allow.empty() ? "" : "Allow: " + std::string(allow) + "\r\n") +
      "Connection: close\r\n\r\n" + body;
  auto* context = HttpResponseContext::Current(fd);
  if (context) {
    context->BeforeTerminal();
    *context->State() = ResponseState::kTerminal;
  }
  const bool written = WriteAll(fd, resp, options);
  if (context && written) *context->State() = ResponseState::kComplete;
  return written;
}

namespace {
struct ErrorInfo {
  const char* reason;
  const char* type;
  const char* code;
};

ErrorInfo ErrorForStatus(int status) {
  switch (status) {
    case 400:
      return {"Bad Request", "invalid_request_error", "invalid_request"};
    case 401:
      return {"Unauthorized", "authentication_error", "unauthorized"};
    case 403:
      return {"Forbidden", "permission_error", "forbidden"};
    case 404:
      return {"Not Found", "invalid_request_error", "not_found"};
    case 405:
      return {"Method Not Allowed", "invalid_request_error",
              "method_not_allowed"};
    case 415:
      return {"Unsupported Media Type", "invalid_request_error",
              "unsupported_media_type"};
    case 408:
      return {"Request Timeout", "timeout_error", "request_timeout"};
    case 409:
      return {"Conflict", "conflict_error", "request_conflict"};
    case 413:
      return {"Content Too Large", "invalid_request_error", "body_too_large"};
    case 429:
      return {"Too Many Requests", "rate_limit_error", "capacity_exhausted"};
    case 502:
      return {"Bad Gateway", "server_error", "upstream_error"};
    case 503:
      return {"Service Unavailable", "service_unavailable_error",
              "service_unavailable"};
    default:
      return {"Internal Server Error", "server_error", "internal_error"};
  }
}
}  // namespace

std::string ErrorJson(int status, const std::string& message,
                      std::string_view error_code, std::string_view param) {
  const auto info = ErrorForStatus(status);
  return "{\"error\":{\"message\":\"" + JsonEscape(message) + "\",\"type\":\"" +
         info.type + "\",\"code\":\"" +
         JsonEscape(std::string(error_code.empty() ? info.code : error_code)) +
         "\",\"param\":" +
         (param.empty() ? "null"
                        : "\"" + JsonEscape(std::string(param)) + "\"") +
         "}}";
}

std::string HttpResponseContext::StreamError(
    int status, const std::string& message, std::string_view code,
    std::string_view param) {
  if (stream_error_renderer_)
    return stream_error_renderer_(stream_error_owner_, status, message, code,
                                  param);
  return "data: " + ErrorJson(status, message, code, param) +
         "\r\n\r\ndata: [DONE]\r\n\r\n";
}

void SendError(int fd, int code, const std::string& message,
               const WriteOptions& options, std::string_view error_code,
               std::string_view param, std::string_view allow) {
  if (error_code.empty()) error_code = ErrorForStatus(code).code;
  if (auto* context = HttpResponseContext::Current(fd)) {
    context->ObserveError(code, message, error_code);
    context->StopHeartbeats();
    std::fprintf(stderr, "[q4t][http] trace_id=%s status=%d error=%.*s\n",
                 context->TraceId().c_str(), code,
                 static_cast<int>(error_code.size()), error_code.data());
    if (*context->State() == ResponseState::kStreaming) {
      const std::string tail =
          context->StreamError(code, message, error_code, param);
      context->BeforeTerminal();
      *context->State() = ResponseState::kTerminal;
      WriteAll(fd, tail, options);
      return;
    }
    if (*context->State() != ResponseState::kUnsent) return;
  }
  SendSimple(fd, code, ErrorForStatus(code).reason,
             ErrorJson(code, message, error_code, param), "application/json",
             options, allow);
}

void SendStreamError(int fd, ResponseState* state, int status,
                     const std::string& message, const WriteOptions& options,
                     std::string_view error_code) {
  if (auto* context = HttpResponseContext::Current(fd)) {
    context->ObserveError(status, message, error_code);
    context->StopHeartbeats();
  }
  if (*state != ResponseState::kStreaming) return;
  auto* context = HttpResponseContext::Current(fd);
  const std::string tail = context
      ? context->StreamError(status, message, error_code)
      : "data: " + ErrorJson(status, message, error_code) +
          "\r\n\r\ndata: [DONE]\r\n\r\n";
  if (auto* context = HttpResponseContext::Current(fd))
    context->BeforeTerminal();
  *state = ResponseState::kTerminal;
  WriteAll(fd, tail, options);
}

void SendResponseParseError(int fd, bool stream, ResponseState* state,
                            const std::string& message,
                            const WriteOptions& options, std::string_view code,
                            int http_status) {
  if (*state != (stream ? ResponseState::kStreaming : ResponseState::kUnsent))
    return;
  if (stream) {
    SendStreamError(fd, state, http_status, message, options, code);
  } else {
    SendError(fd, http_status, message, options, code);
    *state = ResponseState::kTerminal;
  }
}

// OpenAI SSE data chunk for a streaming completion.
std::string SseChunk(const std::string& id, const std::string& model,
                     const std::string& delta_role, const std::string& content,
                     const std::string& finish_reason, int index,
                     int64_t created, bool include_usage) {
  std::string delta = "{";
  if (!delta_role.empty()) {
    delta += "\"role\":\"" + JsonEscape(delta_role) + "\",";
  }
  delta += "\"content\":\"" + JsonEscape(content) + "\"}";
  return SseDeltaChunk(id, model, delta, finish_reason, index, created,
                       include_usage);
}

std::string SseDeltaChunk(const std::string& id, const std::string& model,
                          const std::string& delta_json,
                          const std::string& finish_reason, int index,
                          int64_t created, bool include_usage,
                          std::string_view logprobs_json) {
  std::string choice =
      "{\"index\":" + std::to_string(index) + ",\"delta\":" + delta_json;
  if (!finish_reason.empty()) {
    choice += ",\"finish_reason\":\"" + finish_reason + "\"";
  } else {
    choice += ",\"finish_reason\":null";
  }
  choice += ",\"logprobs\":" + std::string(logprobs_json) + '}';
  std::string obj = "{\"id\":\"" + JsonEscape(id) +
                    "\",\"object\":\"chat.completion.chunk\","
                    "\"created\":" +
                    std::to_string(created ? created : std::time(nullptr)) +
                    ",\"model\":\"" + JsonEscape(model) + "\",\"choices\":[" +
                    choice + "]" + (include_usage ? ",\"usage\":null" : "") +
                    "}";
  return "data: " + obj + "\r\n\r\n";
}

}  // namespace q4t::server::detail
