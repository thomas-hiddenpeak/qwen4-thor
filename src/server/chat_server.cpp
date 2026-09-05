// OpenAI-compatible HTTP server implementation. See chat_server.h.
#include "q4t/server/chat_server.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include "q4t/io/json.h"

namespace q4t {
namespace server {

namespace {

constexpr const char* kDefaultModelName = "qwen3.8-flash-next";

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

bool WriteAll(int fd, const char* data, size_t len) {
  size_t off = 0;
  while (off < len) {
    const ssize_t n = ::write(fd, data + off, len - off);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      return false;
    }
    off += static_cast<size_t>(n);
  }
  return true;
}

bool WriteAll(int fd, const std::string& s) {
  return WriteAll(fd, s.data(), s.size());
}

// Read the full HTTP request: request line + headers (until blank line) +
// Content-Length body. Returns false on a malformed or closed request.
bool ReadRequest(int fd, std::string* method, std::string* path,
                 std::string* body) {
  std::string buf;
  char tmp[4096];
  size_t header_end = std::string::npos;
  while (header_end == std::string::npos) {
    const ssize_t n = ::read(fd, tmp, sizeof(tmp));
    if (n <= 0) return false;
    buf.append(tmp, static_cast<size_t>(n));
    header_end = buf.find("\r\n\r\n");
    if (buf.size() > 1 << 20) return false;  // header guard
  }
  const size_t head_len = header_end + 4;
  std::string head = buf.substr(0, header_end);
  std::string rest = buf.substr(head_len);

  const size_t line_end = head.find("\r\n");
  std::string request_line = (line_end == std::string::npos)
                                 ? head
                                 : head.substr(0, line_end);
  {
    const size_t sp1 = request_line.find(' ');
    const size_t sp2 = request_line.rfind(' ');
    if (sp1 == std::string::npos || sp2 == std::string::npos || sp2 <= sp1) {
      return false;
    }
    *method = request_line.substr(0, sp1);
    *path = request_line.substr(sp1 + 1, sp2 - sp1 - 1);
  }

  // Content-Length (case-insensitive scan of headers).
  size_t content_length = 0;
  std::string lower = head;
  for (auto& c : lower) c = static_cast<char>(std::tolower(c));
  const size_t cl = lower.find("content-length:");
  if (cl != std::string::npos) {
    size_t i = cl + std::string("content-length:").size();
    while (i < head.size() && (head[i] == ' ' || head[i] == '\t')) ++i;
    content_length = static_cast<size_t>(std::strtoul(head.c_str() + i, nullptr,
                                                      10));
  }
  if (content_length > (16u << 20)) return false;  // body guard
  while (rest.size() < content_length) {
    const ssize_t n = ::read(fd, tmp, sizeof(tmp));
    if (n <= 0) return false;
    rest.append(tmp, static_cast<size_t>(n));
  }
  *body = rest.substr(0, content_length);
  return true;
}

void SendSimple(int fd, int code, const char* status, const std::string& body,
                const std::string& content_type) {
  std::string resp = "HTTP/1.1 " + std::to_string(code) + " " + status +
                     "\r\nContent-Type: " + content_type +
                     "\r\nContent-Length: " + std::to_string(body.size()) +
                     "\r\nConnection: close\r\n\r\n" +
                     body;
  WriteAll(fd, resp);
}

void SendError(int fd, int code, const std::string& message) {
  const std::string body =
      "{\"error\":{\"message\":\"" + JsonEscape(message) +
      "\",\"type\":\"invalid_request_error\",\"code\":null}}";
  SendSimple(fd, code, "Bad Request", body, "application/json");
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
      std::to_string(std::time(nullptr)) + ",\"model\":\"" + model +
      "\",\"choices\":[" + choice + "]}";
  return "data: " + obj + "\r\n\r\n";
}

}  // namespace

ChatServer::~ChatServer() = default;

Status ChatServer::Start(const ServerOptions& opts) {
  text::TokenizerLimits limits;
  Status s = text::Tokenizer::Load(opts.model_dir + "/tokenizer.json", limits,
                                   &tok_);
  if (!s.ok()) {
    return Status::Fail("tokenizer load failed: " + s.message());
  }

  model::ModelConfig cfg;
  cfg.model_dir = opts.model_dir;
  cfg.index_path = opts.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = opts.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  s = model::LoadModel(cfg, &model_, nullptr);
  if (!s.ok()) {
    tok_.reset();
    return Status::Fail("model load failed: " + s.message());
  }
  port_ = opts.port;
  max_tokens_default_ = opts.max_tokens;
  max_prefill_ = cfg.max_prefill;
  max_len_ = cfg.max_len;
  model_name_ = kDefaultModelName;
  return Status();
}

Status ChatServer::Run() {
  int listen_fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listen_fd < 0) {
    return Status::Fail(std::string("socket: ") + std::strerror(errno));
  }
  int one = 1;
  ::setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(static_cast<uint16_t>(port_));
  if (::bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) <
      0) {
    const std::string err = std::strerror(errno);
    ::close(listen_fd);
    return Status::Fail("bind: " + err);
  }
  if (::listen(listen_fd, 16) < 0) {
    const std::string err = std::strerror(errno);
    ::close(listen_fd);
    return Status::Fail("listen: " + err);
  }
  std::fprintf(stderr, "[q4t] serving on port %d (model %s)\n", port_,
               model_name_.c_str());
  while (true) {
    sockaddr_in client{};
    socklen_t clen = sizeof(client);
    const int fd =
        ::accept(listen_fd, reinterpret_cast<sockaddr*>(&client), &clen);
    if (fd < 0) {
      if (errno == EINTR) continue;
      ::close(listen_fd);
      return Status::Fail(std::string("accept: ") + std::strerror(errno));
    }
    int nodelay = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    HandleClient(fd);
    ::close(fd);
  }
}

void ChatServer::HandleClient(int fd) {
  std::string method, path, body;
  if (!ReadRequest(fd, &method, &path, &body)) {
    SendSimple(fd, 400, "Bad Request", "bad request", "text/plain");
    return;
  }
  Dispatch(fd, method, path, body);
}

void ChatServer::Dispatch(int fd, const std::string& method,
                          const std::string& path, const std::string& body) {
  if (path == "/healthz" && method == "GET") {
    HandleHealth(fd);
    return;
  }
  if (path == "/v1/models" && method == "GET") {
    HandleModels(fd);
    return;
  }
  if (path == "/v1/chat/completions" && method == "POST") {
    HandleChat(fd, body);
    return;
  }
  SendSimple(fd, 404, "Not Found",
             "{\"error\":{\"message\":\"not found\"}}", "application/json");
}

void ChatServer::HandleHealth(int fd) {
  SendSimple(fd, 200, "OK", "ok", "text/plain");
}

void ChatServer::HandleModels(int fd) {
  const std::string body =
      "{\"object\":\"list\",\"data\":[{\"id\":\"" + model_name_ +
      "\",\"object\":\"model\",\"created\":" + std::to_string(std::time(nullptr)) +
      ",\"owned_by\":\"q4t\"}]}";
  SendSimple(fd, 200, "OK", body, "application/json");
}

void ChatServer::HandleChat(int fd, const std::string& body) {
  io::Json req;
  Status s = io::ParseJson(body, &req);
  if (!s.ok()) {
    SendError(fd, 400, "invalid JSON: " + s.message());
    return;
  }

  // max_tokens (default to the server cap).
  int max_tokens = static_cast<int>(
      req.GetInt("max_tokens", max_tokens_default_));
  if (max_tokens <= 0) max_tokens = 1;

  // stream flag.
  const bool stream = req.GetBool("stream", false);

  // Build the prompt from the messages array (concatenate role + content).
  std::string prompt;
  const io::Json* messages = req.GetArray("messages");
  if (messages && messages->IsArray()) {
    for (const io::Json& m : messages->array) {
      const std::string role = m.GetString("role", "");
      const std::string content = m.GetString("content", "");
      if (!role.empty()) {
        if (!prompt.empty()) prompt += "\n";
        prompt += role + ": ";
      }
      prompt += content;
    }
  } else {
    // Fallback: a bare "prompt" string field.
    prompt = req.GetString("prompt", "");
  }
  if (prompt.empty()) {
    SendError(fd, 400, "empty prompt");
    return;
  }

  // Serialize: the model is stateful, one request at a time.
  const std::lock_guard<std::mutex> lock(mu_);

  // 1. Encode prompt.
  std::vector<std::uint32_t> prompt_u32;
  s = tok_->Encode(prompt, &prompt_u32);
  if (!s.ok()) {
    SendError(fd, 400, "encode failed: " + s.message());
    return;
  }
  std::vector<int32_t> ids(prompt_u32.begin(), prompt_u32.end());
  const int T = static_cast<int>(ids.size());
  if (T > max_prefill_) {
    SendError(fd, 400,
              "prompt too long: " + std::to_string(T) + " tokens > " +
                  std::to_string(max_prefill_) + " max prefill");
    return;
  }
  if (T >= max_len_) {
    SendError(fd, 400,
              "prompt too long for context: " + std::to_string(T) +
                  " tokens >= " + std::to_string(max_len_) + " max_len");
    return;
  }

  const int vocab = model_.cfg.vocab;
  const int eos = static_cast<int>(model_.cfg.eos_token_id);
  // d_logits must hold T rows (prefill lm_head GEMM outputs [T, vocab]).
  // Decode steps write 1 row (T=1) to row 0, which fits within this allocation.
  uint16_t* d_logits = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_logits),
                 static_cast<size_t>(T) * vocab * 2) != cudaSuccess) {
    SendError(fd, 500, "cudaMalloc logits failed");
    return;
  }
  std::vector<uint16_t> h_logits(static_cast<size_t>(vocab));

  auto argmax = [&](const uint16_t* h) {
    int best = 0;
    float best_v = -1e30f;
    for (int v = 0; v < vocab; ++v) {
      const uint32_t bits = static_cast<uint32_t>(h[v]) << 16;
      float f;
      std::memcpy(&f, &bits, sizeof(f));
      if (f > best_v) {
        best_v = f;
        best = v;
      }
    }
    return best;
  };

  // 2. Prefill (PD-ready 阶段边界 API: Begin -> Prefill).
  model::ModelSequence seq;
  s = model::ModelBeginSequence(model_, &seq, nullptr);
  if (!s.ok()) {
    cudaFree(d_logits);
    SendError(fd, 500, "begin sequence failed: " + s.message());
    return;
  }
  s = model::ModelPrefill(model_, &seq, ids.data(), T, d_logits, nullptr);
  if (!s.ok()) {
    model::ModelEndSequence(&seq);
    cudaFree(d_logits);
    SendError(fd, 500, "prefill failed: " + s.message());
    return;
  }

  // 3. Decode loop (greedy).
  const std::string id = "chatcmpl-" + std::to_string(std::time(nullptr));
  const std::string model_field = model_name_;
  const int created = static_cast<int>(std::time(nullptr));

  // Streaming header (flushed immediately).
  if (stream) {
    const std::string head =
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\nConnection: close\r\n\r\n";
    WriteAll(fd, head);
    WriteAll(fd, SseChunk(id, model_field, "assistant", "", "", 0));
  }

  std::vector<int32_t> generated;
  int next_token = -1;
  std::string finish_reason = "stop";

  // First decode token comes from the prefill's LAST position (row T-1).
  cudaMemcpy(h_logits.data(), d_logits + static_cast<size_t>(T - 1) * vocab,
             static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
  next_token = argmax(h_logits.data());

  for (int step = 0; step < max_tokens; ++step) {
    generated.push_back(next_token);
    if (next_token == eos) {
      finish_reason = "stop";
      break;
    }
    if (step == max_tokens - 1) {
      finish_reason = "length";
    }
    const int32_t tok_id = next_token;
    // Emit the token text (decode this single token).
    if (stream) {
      std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
      std::string piece;
      if (tok_->Decode(one, true, &piece).ok()) {
        WriteAll(fd, SseChunk(id, model_field, "", piece, "", 0));
      }
    }
    if (seq.position + 1 >= max_len_) {
      finish_reason = "length";
      break;  // cannot decode further without exceeding the KV cache
    }
    s = model::ModelDecodeStepSeq(model_, &seq, tok_id, d_logits, nullptr);
    if (!s.ok()) {
      finish_reason = "stop";
      break;
    }
    cudaMemcpy(h_logits.data(), d_logits, static_cast<size_t>(vocab) * 2,
               cudaMemcpyDeviceToHost);
    next_token = argmax(h_logits.data());
  }
  model::ModelEndSequence(&seq);

  // 4. Finalize.
  if (stream) {
    WriteAll(fd, SseChunk(id, model_field, "", "", finish_reason, 0));
    WriteAll(fd, "data: [DONE]\r\n\r\n");
  } else {
    std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
    std::string text;
    if (tok_->Decode(gen_u32, true, &text).ok()) {
      std::string resp =
          "{\"id\":\"" + id + "\",\"object\":\"chat.completion\","
          "\"created\":" +
          std::to_string(created) + ",\"model\":\"" + model_field +
          "\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\","
          "\"content\":\"" +
          JsonEscape(text) +
          "\"},\"finish_reason\":\"" + finish_reason + "\"}],"
          "\"usage\":{\"prompt_tokens\":" +
          std::to_string(T) +
          ",\"completion_tokens\":" + std::to_string(generated.size()) +
          ",\"total_tokens\":" + std::to_string(T + generated.size()) + "}";
      resp += "}";
      SendSimple(fd, 200, "OK", resp, "application/json");
    } else {
      SendError(fd, 500, "decode failed");
    }
  }

  cudaFree(d_logits);
}

}  // namespace server
}  // namespace q4t
