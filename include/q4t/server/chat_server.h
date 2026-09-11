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

#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "q4t/model/model.h"
#include "q4t/mtp/mtp.h"
#include "q4t/status.h"
#include "q4t/text/tokenizer.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/vision.h"

namespace q4t {
namespace server {

struct ServerOptions {
  int port = 8000;
  std::string model_dir;
  int max_tokens = 256;  // default cap when the request omits max_tokens
  int max_prefill = 0;  // 0 = use ModelConfig default (2048); >0 overrides
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

  // Multimodal pipeline: decode each image_url part, run the image processor
  // + vision tower, and return the merged visual features (device BF16) plus
  // the number of <image> tokens each image expands to. `images` is the list
  // of raw decoded image bytes (PNG/JPEG) in message order; `out_feats`
  // receives the concatenated feature rows (owned by the caller's device
  // buffer, freed by the caller) and `out_counts` the per-image token counts.
  bool RunVisionPipeline(const std::vector<std::string>& images,
                         uint16_t** out_feats, int* out_num_tokens,
                         std::vector<int>* out_counts, std::string* err);

  std::string model_name_;
  int port_ = 8000;
  int max_tokens_default_ = 256;
  int max_prefill_ = 2048;
  int max_len_ = 2048;
  std::unique_ptr<text::Tokenizer> tok_;
  model::Model model_;
  // MTP draft model (optional). Borrowed embed/lm_head from model_ must be
  // freed before model_ (destructor order: members destruct in reverse
  // declaration order, so mtp_ is destroyed before model_).
  mtp::MtpModel mtp_;
  bool mtp_loaded_ = false;
  int mtp_k_ = 3;  // matches the CLI default (实测最优, 见 docs/LOG.md)
  // Rolling draft-trunk double buffer for the speculative step (persistent;
  // freed in the destructor). d_trunk_full (prefill trunk_out [T, hc*hs]) is
  // per-request and freed by HandleChat's cleanup lambda.
  uint16_t* d_g_ = nullptr;
  uint16_t* d_g_next_ = nullptr;
  // Vision tower (multimodal). Null if the model has no visual weights.
  std::unique_ptr<vision::VisionTower> vision_tower_;
  vision::ProcessorConfig proc_cfg_;
  std::mutex mu_;
};

}  // namespace server
}  // namespace q4t
