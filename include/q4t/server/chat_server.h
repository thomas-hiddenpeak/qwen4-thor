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

#include <array>
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

// One multimodal content part: a single image (1 frame) or a video (N frames).
// `frames` holds raw decoded PNG/JPEG byte buffers. `kind` selects the
// processor budget (image vs video) and the placeholder token (image_token_id
// vs video_token_id). Items are kept in content-part (prompt position) order
// so the vision feature rows line up with the placeholders left-to-right.
struct VisionItem {
  enum Kind { kImage, kVideo };
  Kind kind = kImage;
  std::vector<std::string> frames;  // decoded PNG/JPEG bytes (1 for an image)
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

  // Multimodal pipeline: run the image/video processor + vision tower over the
  // vision items (in content-part order) and return the merged visual features
  // (device BF16) plus the per-item expansion counts and grids. `items` is the
  // list of VisionItem (image or video) in prompt position order; `out_feats`
  // receives the concatenated feature rows (owned by the caller's device
  // buffer, freed by the caller), `out_num_tokens` the total merged-token
  // count, `out_counts` the per-item token counts, and `out_grids` the per-item
  // ViT grid (t, h, w) in PATCH units (same order as `items`). The grids feed
  // the 3D MRoPE position computation (model::VisionFeatures::grids).
  bool RunVisionPipeline(const std::vector<VisionItem>& items,
                         uint16_t** out_feats, int* out_num_tokens,
                         std::vector<int>* out_counts,
                         std::vector<std::array<int, 3>>* out_grids,
                         std::string* err);

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
  vision::ProcessorConfig proc_cfg_;       // image budget (65536/16777216)
  vision::ProcessorConfig video_proc_cfg_;  // video budget (4096/25165824)
  std::mutex mu_;
};

}  // namespace server
}  // namespace q4t
