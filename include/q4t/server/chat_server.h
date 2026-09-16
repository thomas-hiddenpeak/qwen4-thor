// OpenAI-compatible HTTP server for q4t (Phase 1 `serve` command).
//
// A minimal, dependency-free HTTP/1.1 server built on POSIX sockets. It serves
// three endpoints:
//   GET  /healthz               -> 200 "ok"
//   GET  /v1/models             -> 200 OpenAI model list
//   POST /v1/chat/completions   -> 200 OpenAI chat completion (stream or not)
//
// The model is stateful (per-layer SSM/conv/KV caches persist across decode
// steps). B1 makes requests concurrent: each client is handled on its own
// thread + CUDA stream, the per-sequence recurrent state is pooled and
// isolated by seq_id (see ServerOptions::max_seq), and the model's single
// shared per-forward scratch is serialized behind a forward-level mutex. CPU
// post-processing (D2H/argmax/tokenize/SSE) overlaps across requests.
#pragma once

#include <array>
#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
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
  // Max sequence length (sizes the full-attention KV/indexer/rope caches).
  // 0 = use ModelConfig default (8192); >0 overrides (e.g. 262144 for the
  // model's full context; see PHASES.md 262K memory budget — use max_seq=1).
  int max_len = 0;
  // Max concurrent sequences (per-sequence recurrent-state pool size). Each
  // in-flight request owns one seq_id; the model's SSM/conv/PLE-conv/KV/indexer
  // state is pooled [max_seq, ...] so concurrent requests are isolated.
  int max_seq = 8;
  // Disable the MTP draft model (plain greedy decode only). Useful for
  // isolating MTP-specific concurrency issues from the base scheduler.
  bool no_mtp = false;
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

  // Accept loop: blocks, spawning one thread per client (concurrent requests;
  // see the B1 scheduling note above), until an error or closed. Returns the
  // exit Status.
  Status Run();

 private:
  void HandleClient(int fd);
  void Dispatch(int fd, const std::string& method, const std::string& path,
                const std::string& body);
  void HandleHealth(int fd);
  void HandleModels(int fd);
  void HandleChat(int fd, const std::string& body);

  // B1 multi-request scheduling. The model's per-forward scratch (GEMM ws,
  // PLE gather, MTP speculative buffers) is a single shared allocation, so all
  // model forwards are serialized behind `model_mu_`; the per-sequence
  // recurrent state is pooled and isolated by seq_id. CPU post-processing
  // (D2H/argmax/tokenize/SSE) and encoding run OUTSIDE `model_mu_` on a
  // per-request CUDA stream, so one request's D2H overlaps another request's
  // forward on the GPU.
  int AllocSeqId();  // blocks (queues) until a slot frees; -1 only on shutdown
  void FreeSeqId(int seq_id);
  std::mutex model_mu_;   // serializes all model forwards (main + MTP + vision)
  std::mutex seq_mu_;     // guards the seq_id free pool
  std::condition_variable seq_cv_;  // requests queue here for a free seq slot
  bool seq_stopping_ = false;       // set on shutdown to release seq waiters

  // B2b continuous batching: a central scheduler thread collects the current
  // decode token of every ACTIVE plain-decode request and runs them in ONE
  // packed ModelDecodeBatchMulti (the 84 GB of weights is read once for all B
  // tokens instead of B times — the decode throughput win). Stage 2c adds an
  // MTP branch: concurrent MTP requests are batched into ONE
  // MtpSpeculativeStepMulti (batched draft loop + ModelVerifyMulti + batched
  // extend). Each request thread registers its step input, blocks on its own
  // cv, and is woken with the step's output.
  //
  // An ActiveRequest is the scheduler's view of one in-flight request.
  // `pending` is set by the request thread (under sched_mu_) before it signals
  // the scheduler; `done` is set by the scheduler (under sched_mu_) once the
  // forward has produced this request's result. `next_token` carries the
  // plain-decode argmax back; `mtp_*` carry the MTP step output.
  struct ActiveRequest {
    int seq_id = 0;        // pooled-state slice index
    int position = 0;      // absolute position of the token to decode
    int32_t token = 0;     // the token to feed (this request's next input)
    std::vector<int32_t> ple_hist;  // PLE n-gram context (ngram_size-1, oldest
                                     // first, EOS-filled) for `token`
    model::ModelSequence* seq = nullptr;  // the request's main sequence (owned
                                           // by the request thread; the
                                           // scheduler reads its
                                           // position/history/seq_id/stage to
                                           // run the MTP step — the request
                                           // thread is blocked on `cv` for the
                                           // whole step, so no race)
    bool pending = false;  // token registered, awaiting the packed forward
    bool done = false;     // scheduler produced `next_token`
    int32_t next_token = 0;
    // MTP speculative-decode state (Phase 2 Stage 2c). When `is_mtp` is set
    // the scheduler runs this request's speculative step inside a batched
    // MtpSpeculativeStepMulti (concurrent MTP requests share ONE step: batched
    // draft loop + ModelVerifyMulti + batched extend). The request thread
    // registers `mtp_b`/`mtp_d0`/`mtp_g` (the step's inputs) and blocks on
    // `cv`; the scheduler writes `mtp_next_b`/`mtp_next_d0`/`mtp_accepted`/
    // `mtp_accepted_count` (the step's outputs) and wakes it. `mtp_g` is a
    // per-request draft-trunk buffer (device [hc*hs], allocated by the request
    // thread, freed in HandleChat's cleanup); the scheduler writes the next
    // step's trunk into it. `mtp_accepted` holds up to mtp_k+1 accepted tokens
    // (the bonus + accepted drafts) for this step.
    bool is_mtp = false;
    int32_t mtp_b = 0;         // bonus token t_P (step input)
    int32_t mtp_d0 = 0;        // first draft token (step input)
    uint16_t* mtp_g = nullptr;  // draft trunk [hc*hs] (step input + next output)
    int32_t mtp_next_b = 0;     // correction / next bonus (step output)
    int32_t mtp_next_d0 = 0;    // next first draft (step output)
    int mtp_accepted_count = 0;  // accepted tokens this step (step output)
    int32_t mtp_accepted[64];   // accepted tokens (bonus + drafts, step output)
    std::condition_variable cv;  // request thread waits here for `done`
  };
  void SchedulerLoop();
  void StopScheduler();  // signal + join the scheduler thread (destructor)
  std::mutex sched_mu_;  // guards active_ + the pending/done handshake
  std::condition_variable sched_cv_;  // scheduler waits for pending work
  std::vector<ActiveRequest*> active_;  // live plain-decode requests
  std::thread scheduler_thread_;
  bool scheduler_stop_ = false;
  bool scheduler_active_ = false;  // false if the scheduler buffer alloc failed
                                    // (plain decode then uses the per-request path)
  // Scheduler's packed-logits buffers (device [max_seq, vocab] + host mirror).
  // Only the scheduler thread touches these (the write is under model_mu_).
  uint16_t* d_sched_logits_ = nullptr;
  int32_t* d_sched_tokens_ = nullptr;        // [max_seq] GPU argmax result
  std::vector<int32_t> h_sched_tokens_;      // host mirror (B ints, not vocab)
  // Shared prefill-logits buffer [max_prefill, vocab], used only under
  // model_mu_ (prefills serialize there). Replaces the per-request ~1 GB
  // d_logits so peak GPU memory does not scale with prompt length x
  // concurrency (that over-commit OOM'd the box under load); only the last
  // prefill row is ever consumed.
  uint16_t* d_prefill_logits_ = nullptr;
  // The tokenizer's ICU 74 regex engine is NOT thread-safe (concurrent Encode
  // trips U_INTERNAL_PROGRAM_ERROR), so all Encode/Decode calls are serialized
  // behind tok_mu_ even though the rest of the tokenizer is immutable.
  std::mutex tok_mu_;
  std::vector<bool> seq_free_;  // [max_seq]; true = available
  int max_seq_ = 8;
  // In-flight request-thread cap: bounds how many connections may be waiting
  // in the AllocSeqId queue at once, so a connection flood cannot spawn
  // unbounded threads. Excess connections are refused (503) to shed load.
  std::atomic<int> active_conns_{0};
  int conn_cap_ = 512;

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
  // NOTE (Stage 2c): the legacy shared rolling draft-trunk buffers d_g_/
  // d_g_next_ are gone — concurrent MTP requests would corrupt each other's
  // trunk. Each MTP request now owns a per-request d_mtp_g (HandleChat local,
  // freed in cleanup), and the draft KV is pooled per seq_id (mcfg.max_seq).
  // Rolling draft-trunk double buffer for the speculative step (persistent;
  // freed in the destructor). d_trunk_full (prefill trunk_out [T, hc*hs]) is
  // per-request and freed by HandleChat's cleanup lambda.
  // Vision tower (multimodal). Null if the model has no visual weights.
  std::unique_ptr<vision::VisionTower> vision_tower_;
  vision::ProcessorConfig proc_cfg_;       // image budget (65536/16777216)
  vision::ProcessorConfig video_proc_cfg_;  // video budget (4096/25165824)
};

}  // namespace server
}  // namespace q4t
