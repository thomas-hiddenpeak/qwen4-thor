// OpenAI-compatible HTTP server for q4t (Phase 1 `serve` command).
//
// A minimal, dependency-free HTTP/1.1 server built on POSIX sockets. It serves
// three endpoints:
//   GET  /healthz               -> 200 "ok"
//   GET  /v1/models             -> 200 OpenAI model list
//   POST /v1/chat/completions   -> 200 OpenAI chat completion (stream or not)
//
// The model is stateful (per-layer SSM/conv/KV caches persist across decode
// steps), so all requests are serialized behind a single mutex: each request
// runs a fresh prefill (which resets per-layer state) followed by a greedy
// decode loop. This is correct but not concurrent; concurrency is Phase 2.
#pragma once

#include <mutex>
#include <string>

#include "q4t/model/model.h"
#include "q4t/status.h"
#include "q4t/text/tokenizer.h"

namespace q4t {
namespace server {

struct ServerOptions {
  int port = 8000;
  std::string model_dir;
  int max_tokens = 256;  // default cap when the request omits max_tokens
};

class ChatServer {
 public:
  ChatServer() = default;
  ~ChatServer();

  ChatServer(const ChatServer&) = delete;
  ChatServer& operator=(const ChatServer&) = delete;

  // Load the tokenizer + full model. On success returns ok and the server is
  // ready to Run(); on failure the Status carries a message and nothing is
  // running.
  Status Start(const ServerOptions& opts);

  // Accept loop: blocks, handling one client at a time, until an error or
  // closed. Returns the exit Status.
  Status Run();

 private:
  void HandleClient(int fd);
  void Dispatch(int fd, const std::string& method, const std::string& path,
                const std::string& body);
  void HandleHealth(int fd);
  void HandleModels(int fd);
  void HandleChat(int fd, const std::string& body);

  std::string model_name_;
  int port_ = 8000;
  int max_tokens_default_ = 256;
  int max_prefill_ = 2048;
  int max_len_ = 2048;
  std::unique_ptr<text::Tokenizer> tok_;
  model::Model model_;
  std::mutex mu_;
};

}  // namespace server
}  // namespace q4t
