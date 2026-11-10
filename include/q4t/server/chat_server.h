// OpenAI-compatible HTTP server for q4t (Phase 1 `serve` command).
//
// Native HTTP/1.1 on POSIX sockets: Chat, Completions, Responses, Conversations
// and operational routes. Optional crypto/schema/media dependencies are scoped
// to the features that use them. Preflight/config are protocol extensions.
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
#include <cstdint>
#include <memory>
#include <mutex>
#include <span>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>

#include "q4t/trace/router_collector.h"
#include "q4t/server/suffix_token_cache.h"
#include "q4t/server/session_cache.h"
#include "q4t/server/prompt_cache_diagnostics.h"
#include "q4t/server/http_request.h"
#include "q4t/server/http_listener.h"
#include "q4t/server/http_response.h"
#include "q4t/server/server_options.h"
#include "q4t/server/sequence_admission.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/model/model_owner.h"
#include "q4t/mtp/mtp.h"
#include "q4t/server/request_control.h"
#include "q4t/server/progress_transport.h"
#include "q4t/server/client_policy_admin.h"
#include "q4t/server/state_key_admin.h"
#include "q4t/status.h"
#include "q4t/runtime/memory_budget.h"
#ifdef Q4T_STARTUP_RESERVATION
#include "q4t/runtime/cuda_allocation_quota.h"
#include "q4t/runtime/startup_reservation.h"
#endif
#include "q4t/text/tokenizer.h"
#include "q4t/server/abort_reason.h"
#include "q4t/server/memory_admission.h"
#include "q4t/server/service_health.h"
#include "q4t/server/startup_admission.h"
#include "q4t/server/structured_admission.h"
#include "q4t/server/device_memory.h"
#include "q4t/server/grammar_process.h"
#include "q4t/server/object_store.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/server/batch_execution.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/media_plan.h"
#include "q4t/vision/vision.h"

namespace q4t {
namespace server {

// Fixed-bucket, lock-free latency histogram (Prometheus style). Buckets are
// le boundaries in seconds; sum is accumulated in microseconds.
struct MetricHistogram {
  static constexpr int kNumBuckets = 14;
  std::atomic<uint64_t> bucket[kNumBuckets]{};  // per-bucket (non-cumulative)
  std::atomic<uint64_t> inf{0};                 // observations past the last le
  std::atomic<uint64_t> count{0};
  std::atomic<uint64_t> sum_us{0};
  void Observe(double seconds);
};

// serve/scheduler observability counters + histograms (exposed at /metrics).
struct ServeMetrics {
  HttpTransportMetrics http_transport;
  std::array<std::atomic<uint64_t>, kHttpConnectionClasses.size()>
      connection_rejections{};
  std::array<std::atomic<uint64_t>, kHttpConnectionClasses.size()>
      operations_connection_rejections{};
  std::atomic<uint64_t> requests_total{0};
  std::atomic<uint64_t> requests_success{0};
  std::atomic<uint64_t> requests_error{0};    // rejected before generation
  std::atomic<uint64_t> requests_aborted{0};  // all cooperative abort reasons
  std::atomic<uint64_t> text_admission_rejected{0};
  std::atomic<uint64_t> queue_rejected{0};
  std::array<std::array<std::atomic<uint64_t>, 2>, 3> memory_rejected{};
  std::array<std::atomic<uint64_t>, kAbortReasons.size()> abort_reasons{};
  std::atomic<int> structured_active{0};  // prep through response cleanup
  std::atomic<int> structured_preparing{0};
  std::atomic<uint64_t> structured_rejected{0};
  std::atomic<uint64_t> structured_prepare_failed{0};
  std::atomic<uint64_t> structured_vocab_bytes{0};
  std::shared_ptr<GrammarWorkerCounters> structured_workers =
      std::make_shared<GrammarWorkerCounters>();
  std::atomic<uint64_t> structured_workers_ready{0};
  std::atomic<uint64_t> structured_compiles_started{0};
  std::atomic<uint64_t> structured_compile_timeouts{0};
  std::atomic<uint64_t> structured_resource_exhausted{0};
  std::atomic<uint64_t> prompt_tokens_total{0};
  std::atomic<uint64_t> generation_tokens_total{0};
  std::atomic<uint64_t> reasoning_budget_tokens_total{0};
  std::atomic<uint64_t> reasoning_forced_tokens_total{0};
  std::atomic<uint64_t> reasoning_forced_requests{0};
  std::atomic<uint64_t> reasoning_mtp_fallbacks{0};
  std::atomic<uint64_t> execution_steps_total{0};
  MetricHistogram ttft_seconds;  // streaming: arrival -> first content sent
  MetricHistogram target_ready_seconds;  // arrival -> first target token ready
  MetricHistogram response_seconds;      // arrival -> full local response write
  MetricHistogram e2e_seconds;           // arrival -> last token
  MetricHistogram queue_seconds;         // arrival -> seq slot acquired
  MetricHistogram slot_wait_seconds;     // only AllocSeqId
  MetricHistogram structured_prepare_seconds;  // vocabulary + compile
};

struct DeviceMemoryResult;

class ChatServer {
 public:
  ChatServer() = default;
  ~ChatServer();

  ChatServer(const ChatServer&) = delete;
  ChatServer& operator=(const ChatServer&) = delete;

  // Reserve listeners before loading the tokenizer + full model, then become
  // ready to Run(). On failure listeners close immediately; destroy this
  // single-use server to release partial model/scheduler resources. Start,
  // Run and destruction must be sequenced by the owner, never concurrent.
  Status Start(const ServerOptions& opts);

  // Accept loop: blocks, spawning one thread per client (concurrent requests;
  // see the B1 scheduling note above), until an error or closed. Returns the
  // exit Status.
  Status Run();

  // Request a graceful shutdown from a signal handler: sets a flag and
  // wakes the bounded accept poll, so Run() rejects new business, briefly
  // serves control requests while draining, and returns. Async-signal-safe.
  void RequestStop();

 private:
  friend struct ServeContractAccess;
#ifdef Q4T_STARTUP_RESERVATION
  runtime::CudaQuotaReportOnDestruction cuda_quota_report_;
  // Destroy last: model, MTP, scheduler and request buffers release their
  // handed allocations before the reservation reclaims unhanded blocks.
  runtime::StartupReservation startup_reservation_;
#endif
  uint64_t cuda_quota_limit_bytes_ = 0;
  void RecordDeviceMemoryFailure(const DeviceMemoryResult& result,
                                 const char* site) noexcept;
  void HandleClient(int fd, bool operations = false);
  bool HandleRequest(int fd, bool operations, HttpRequestState* state,
                     bool allow_keep_alive);
  void StartConnection(int fd, bool operations);
  void FinishConnection(int fd, bool operations) noexcept;
  void Dispatch(int fd, const std::string& method, const std::string& path,
                const std::string& body);
  void HandleHealth(int fd);
  void HandleReadiness(int fd, bool liveness);
  bool CheckMemoryAdmission(int fd, MemoryCheckpoint checkpoint);
  void RecordMemoryRejection(int fd, MemoryState state,
                             MemoryCheckpoint checkpoint);
  void RejectMemoryAdmission(int fd, MemoryState state,
                             MemoryCheckpoint checkpoint);
  void HandleModels(int fd);
  void HandleModel(int fd, const std::string& method, const std::string& path);
  void HandleCapabilities(int fd);
  void HandleMetrics(int fd);
  void HandlePreflight(int fd, const std::string& body);
  void HandleResponsesTokenCount(int fd, const std::string& body);
  void HandleConfig(int fd);
  ChatPlanConfig PlanningConfig(int fd = -1) const;
  enum class GenerationApi { kChat, kCompletion, kResponse, kCompact };
  void HandleChat(int fd, const std::string& body,
                  GenerationApi api = GenerationApi::kChat,
                  std::shared_ptr<RequestControl> owner = {},
                  const io::Json* frozen_inputs = nullptr);
  void HandleChatImpl(int fd, const std::string& body,
                      detail::ResponseState* response_state, bool completion,
                      class ResponsesExecution* responses,
                      std::shared_ptr<RequestControl> background = {},
                      struct InternalModelStep* internal = nullptr,
                      const io::Json* frozen_inputs = nullptr);
  BatchRowResult RunBatchRequest(const std::string& endpoint,
                                 const BatchInputRow& row,
                                 const io::Json& frozen_inputs,
                                 const ClientIdentity& client,
                                 ClientLease& permit,
                                 const std::shared_ptr<RequestControl>& control,
                                 std::shared_ptr<ClientRateLease> rate = {});
  ObjectReply MeasureBatchRequest(std::string_view endpoint,
                                  const BatchInputRow& row,
                                  const io::Json& prepared,
                                  const ChatPlanConfig& config,
                                  const BinaryAccess& access,
                                  BatchInputBudget* budget,
                                  BatchRateBudget* rate = nullptr);
  BatchTokenCodec BatchCodec(const BinaryAccess& access);
  ObjectReply MeasureBackgroundBatchRate(ResponsesExecution& response,
                                         const ChatPlanConfig& config,
                                         const BinaryAccess& access,
                                         BatchRateBudget* budget);
  ObjectReply RunInternalModelStep(int fd, InternalModelStep* step,
                                   std::shared_ptr<RequestControl> control);
  bool TryHandleBackgroundResponse(int fd, const std::string& body);
  void RunBackgroundResponse(ResponsesJobs::Job& job);

  // Shared scratch is protected by model_mu_ through default-stream GPU
  // completion and host readback. Only then can another request reuse it.
  // Negative: -1 stopping, -2 cancelled, -3 backend unhealthy, -4 queue full,
  // -5 memory pressure, -6 memory observation unavailable.
  // Admission is provisional: recheck backend health under model_mu_ at submit.
  int AllocSeqId(RequestControl* control, int fd, size_t required = 0);
  RequestRegistry requests_;
  ClientAdmission clients_;
  std::shared_ptr<const AccessProgramPolicy> access_program_policy_;
  std::unique_ptr<ClientPolicyAdmin> client_policy_admin_;
  std::unique_ptr<ObjectStore> objects_;
  std::shared_ptr<const FileExpansionOptions> file_inputs_;
  std::string file_input_policy_sha256_;
  std::unique_ptr<StateKeyAdmin> state_key_admin_;
  std::unique_ptr<ResponsesJobs> background_;
  std::mutex connections_mu_;
  std::unordered_set<int> connections_;
  void FreeSeqId(int seq_id);
  std::mutex model_mu_;  // serializes all model forwards (main + MTP + vision)
  std::mutex seq_mu_;    // guards the seq_id free pool
  SequenceAdmissionQueue seq_waiters_;  // also guarded by seq_mu_
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
    RequestControl* control = nullptr;
    int fd = -1;
    bool retired = false;  // sched_mu_; cancelled row cannot submit again
    int32_t token = 0;     // the token to feed (this request's next input)
    // Borrowed while the request thread waits on cv. The scheduler uses
    // this single state for submission and completion, including plain decode.
    model::ModelSequence* seq = nullptr;
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
    bool complete_terminal_draft = false;
    uint16_t* session_last_trunk = nullptr;
    int32_t mtp_b = 0;   // bonus token t_P (step input)
    int32_t mtp_d0 = 0;  // first draft token (step input)
    bool mtp_suffix_ready = false;
    std::array<int32_t, 3> mtp_suffix_tokens{};
    uint16_t* mtp_g =
        nullptr;              // draft trunk [hc*hs] (step input + next output)
    int32_t mtp_next_b = 0;   // correction / next bonus (step output)
    int32_t mtp_next_d0 = 0;  // next first draft (step output)
    int mtp_accepted_count = 0;  // accepted tokens this step (step output)
    int32_t mtp_accepted[64];  // accepted tokens (bonus + drafts, step output)
    std::condition_variable cv;  // request thread waits here for `done`
  };
  // Batched prefill (Phase 2, plain path). Concurrent requests that need a
  // plain prefill (T <= max_prefill, no vision / chunk / MTP) register a
  // PrefillReq here instead of prefilling inline; the scheduler packs up to a
  // (max_prefill-token, max_seq-sequence) batch into ONE ModelPrefillBatch (the
  // dense weights are read once for the whole batch instead of per request) and
  // hands each request its last-row logits. Mirrors the decode collect-batch-
  // distribute handshake. `ids` is owned by the request thread (blocked on `cv`
  // for the whole prefill, so no lifetime race); `h_logits` is the request's
  // [vocab] host buffer that the scheduler D2Hs the sequence's last row into.
  struct PrefillReq {
    RequestControl* control = nullptr;
    int fd = -1;
    model::ModelSequence* seq = nullptr;
    int seq_id = 0;
    const int32_t* ids = nullptr;
    int len = 0;
    uint16_t* h_logits = nullptr;
    bool pending = false;
    bool done = false;
    bool ok = false;
    std::condition_variable cv;
  };
  // Request thread owns these buffers and waits until the entire prompt is
  // done. Only the scheduler advances seq between chunk completions.
  // err carries the scheduler-side failure message (e.g. residency fault
  // injection) so the request thread can surface it in the HTTP error.
  struct ChunkPrefillReq {
    RequestControl* control = nullptr;
    int fd = -1;  // Borrowed until done; owned by the waiting request thread.
    bool cancelled = false;
    model::ModelSequence* seq = nullptr;
    int seq_id = 0;
    const int32_t* ids = nullptr;
    int len = 0;
    uint16_t* h_logits = nullptr;
    bool done = false;
    bool ok = false;
    std::string err;  // set by the scheduler when ok=false
    std::condition_variable cv;
  };
  void RunOnePrefillChunk();
  void ReleaseActiveRequest(ActiveRequest* request) noexcept;
  void AbortBackendExecution() noexcept;  // model_mu_ held; drain before unlock
  void AbortScheduler(std::span<PrefillReq* const> selected = {},
                      ChunkPrefillReq* chunk = nullptr) noexcept;
  void SchedulerLoop();
  void StopScheduler();  // signal + join the scheduler thread (destructor)
  std::mutex sched_mu_;  // guards active_ + the pending/done handshake
  std::condition_variable sched_cv_;    // scheduler waits for pending work
  std::vector<ActiveRequest*> active_;  // live plain-decode requests
  std::vector<PrefillReq*>
      prefill_pending_;  // requests awaiting batched prefill
  std::vector<ChunkPrefillReq*> chunk_prefill_pending_;
  std::thread scheduler_thread_;
  bool scheduler_stop_ = false;
  bool scheduler_active_ =
      false;  // false if the scheduler buffer alloc failed
              // (plain decode then uses the per-request path)
  // Scheduler's packed-logits buffers (device [max_seq, vocab] + host mirror).
  // Only the scheduler thread touches these (the write is under model_mu_).
  uint16_t* d_sched_logits_ = nullptr;
  int32_t* d_sched_tokens_ = nullptr;    // [max_seq] GPU argmax result
  std::vector<int32_t> h_sched_tokens_;  // host mirror (B ints, not vocab)
  // Shared prefill logits [max_seq, vocab], accessed under model_mu_.
  // Batched prefill writes B selected rows; single/inline/vision/MTP main
  // prefill and fallback decode write one row. Full trunk output is separate.
  uint16_t* d_prefill_logits_ = nullptr;
  // The tokenizer's ICU 74 regex engine is NOT thread-safe (concurrent Encode
  // trips U_INTERNAL_PROGRAM_ERROR), so all Encode/Decode calls are serialized
  // behind tok_mu_ even though the rest of the tokenizer is immutable.
  std::mutex tok_mu_;
  std::vector<bool> seq_free_;  // [max_seq]; true = available
  int max_seq_ = 8;
  AdmissionLimits admission_limits_;
  std::vector<int> seq_capacities_;
  // In-flight request-thread cap: bounds how many connections may be waiting
  // in the AllocSeqId queue at once, so a connection flood cannot spawn
  // unbounded threads. Excess connections are refused (503) to shed load.
  HttpBodyBudget body_budget_{kAggregateChatBodyBytes};
  HttpBodyBudget cancel_body_budget_{kCancelBodyBudgetBytes};
  std::atomic<int> active_chats_{0};
  std::atomic<int> active_conns_{0};
  std::atomic<int> public_conns_{0};
  std::atomic<int> operations_conns_{0};
  std::atomic<int> operations_observation_conns_{0};
  std::atomic<int> operations_control_conns_{0};
  std::atomic<int> business_conns_{0};
  std::atomic<int> observation_conns_{0};
  std::atomic<int> control_conns_{0};
  static constexpr int kControlConnections = kReservedControlConnections;
  static constexpr int kObservationConnections =
      kReservedObservationConnections;
  int conn_cap_ = 512;
  // Poll observes stop within 25ms. Draining serves control and observation
  // for at most 2 seconds; remaining owned sockets then close before cleanup.
  std::atomic<bool> stop_requested_{false};
  bool start_attempted_ = false;
  HttpListeners startup_listeners_;
  std::atomic<int> listen_fd_{-1};
  std::atomic<int> operations_listen_fd_{-1};
  // GPU health: a forward's CUDA error is sticky (it poisons every later
  // forward), so once one is observed the server reports unhealthy (healthz
  // 503) and refuses new requests (503) instead of silently failing them all.
  std::atomic<bool> gpu_healthy_{true};
  std::atomic<bool> serving_failed_{false};
  std::atomic<bool> model_ready_{false};
  ServiceHealth ObserveHealth(MemoryState memory = MemoryState::kDisabled)
      const noexcept;
  Status ExecutionStatus() const;
  bool RejectUnhealthy(int fd, const detail::WriteOptions& options = {});
  ServeMetrics metrics_;  // /metrics observability
  RequestProgress progress_;
  std::unique_ptr<ProgressPublisher> progress_publisher_;

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
                         const vision::MediaPlan& plan, uint16_t** out_feats,
                         int* out_num_tokens, std::vector<int>* out_counts,
                         std::vector<std::array<int, 3>>* out_grids,
                         std::string* err, DeviceMemoryResult* device_result,
                         const char* request_id,
                         const std::function<bool()>& cancelled,
                         ChatPlanError* media_error);

  std::string effective_config_;
  std::string model_name_;
  ModelCatalog model_catalog_;
  std::string host_ = "127.0.0.1";
  bool allow_media_ = false;
  int port_ = 8000;
  int operations_port_ = 0;
  int max_tokens_default_ = 256;
  std::vector<int32_t> stop_token_ids_;  // generation_config, not PLE padding
  int max_prefill_ = 2048;
  int max_len_ = 2048;
  // Allocation estimate computed at Start(). Capacity fits the modeled
  // terms, not a physical RAM/OOM guarantee. False when --no-budget.
  runtime::MemoryBudget budget_;
  bool budget_valid_ = false;
  MemoryAdmission memory_admission_;
  StartupMemoryChecks startup_memory_;
  StructuredAdmission structured_admission_;
  std::unique_ptr<text::Tokenizer> tok_;
  model::ModelOwner model_;
  std::unique_ptr<trace::RouterCollector> router_trace_;
  // MTP draft model (optional). Borrowed embed/lm_head from model_ must be
  // freed before model_ (destructor order: members destruct in reverse
  // declaration order, so mtp_ is destroyed before model_).
  mtp::MtpModel mtp_;
  bool mtp_loaded_ = false;
  bool mtp_suffix_ = false;
  // Lazy immutable global-ID properties; no request history is cached.
  // Calls are serialized with tok_mu_; Run drains requests before destruction.
  SuffixTokenCache suffix_token_cache_;
  SessionCache session_cache_;
  SessionCache prefix_cache_{IsolatedWorkerDomain::kPrefixCache};
  PromptCacheDiagnostics prompt_cache_diagnostics_;
  std::vector<int> prefix_cache_checkpoints_;
  // Global intervals sampled at request completion under model_mu_. With
  // concurrent slots an interval can include work from several requests.
  bool residency_enabled_ = false;
  uint64_t residency_interval_ = 0;
  quant::MoEResidency::Stats residency_stats_prev_{};
  model::ResidencyTimingSnapshot residency_timing_prev_{};
  // Default per-request deadline (ms); see ServerOptions::request_deadline_ms.
  int request_deadline_ms_ = 1200000;
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
  vision::ProcessorConfig proc_cfg_;        // image budget (65536/16777216)
  vision::ProcessorConfig video_proc_cfg_;  // video budget (4096/25165824)
  vision::MediaDeviceBudget vision_device_budget_;
};

}  // namespace server
}  // namespace q4t
