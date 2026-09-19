// OpenAI-compatible HTTP server implementation. See chat_server.h.
#include "q4t/server/chat_server.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <thread>
#include <vector>

#include <cuda_bf16.h>
#include <cuda_profiler_api.h>
#include <cuda_runtime.h>

#include "q4t/io/json.h"
#include "q4t/io/weight_loader.h"
#include "q4t/runtime/memory_budget.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/vision.h"

namespace q4t {
namespace server {

namespace {

constexpr const char* kDefaultModelName = "qwen3.8-flash-next";

// le boundaries (seconds) shared by MetricHistogram::Observe and /metrics.
constexpr double kLatencyBounds[MetricHistogram::kNumBuckets] = {
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5,   5.0,  10.0,  30.0, 60.0, 120.0};

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

// Decode a base64 string into raw bytes. Whitespace and '=' padding are
// ignored; both the standard and URL-safe alphabets are accepted. Returns
// false on an invalid character.
bool Base64Decode(const std::string& in, std::string* out) {
  static const std::vector<int8_t> kDec = [] {
    std::vector<int8_t> t(256, -1);
    const char* a =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    for (int i = 0; i < 64; ++i) t[static_cast<unsigned char>(a[i])] =
        static_cast<int8_t>(i);
    t[static_cast<unsigned char>('-')] = 62;  // url-safe
    t[static_cast<unsigned char>('_')] = 63;
    return t;
  }();
  out->clear();
  int val = 0;
  int bits = -8;
  for (unsigned char c : in) {
    if (c == '=' || c == '\n' || c == '\r' || c == ' ' || c == '\t') continue;
    const int8_t d = kDec[c];
    if (d < 0) return false;
    val = (val << 6) | d;
    bits += 6;
    if (bits >= 0) {
      out->push_back(static_cast<char>((val >> bits) & 0xFF));
      bits -= 8;
    }
  }
  return true;
}

// Extract raw image bytes from an OpenAI image_url. Supports base64 data URLs
// ("data:image/png;base64,....") and raw base64 strings. Remote http(s) URLs
// are rejected (the server performs no network fetches).
bool DecodeImageUrl(const std::string& url, std::string* out,
                    std::string* err) {
  const std::string kData = "data:";
  const std::string kB64 = "base64,";
  std::string b64;
  if (url.rfind(kData, 0) == 0) {
    const size_t comma = url.find(kB64);
    if (comma == std::string::npos) {
      if (err) *err = "unsupported data url (need base64 payload)";
      return false;
    }
    b64 = url.substr(comma + kB64.size());
  } else if (url.rfind("http://", 0) == 0 || url.rfind("https://", 0) == 0) {
    if (err) *err = "remote image_url not supported (use a base64 data url)";
    return false;
  } else {
    b64 = url;  // assume a raw base64 payload
  }
  if (!Base64Decode(b64, out)) {
    if (err) *err = "base64 decode failed";
    return false;
  }
  if (out->empty()) {
    if (err) *err = "empty image payload";
    return false;
  }
  return true;
}

// Render one message's `content` to a prompt string. Text parts are appended
// verbatim; image parts append the literal <|image_pad|> placeholder (the
// tokenizer encodes it as a single image_token_id = 248056) and push the
// decoded image bytes to `items` (as a 1-frame VisionItem); video parts
// append <|video_pad|> (video_token_id = 248057) and push their decoded frames
// as a multi-frame VisionItem. Items are appended in content-part order so the
// vision feature rows line up with the placeholders left-to-right. Returns
// false (with err) on a bad image_url / video frame; on success the rendered
// text is returned.
std::string RenderContent(const io::Json& m, std::vector<VisionItem>* items,
                          std::string* err) {
  std::string out;
  const io::Json* content = m.Find("content");
  if (content == nullptr) return out;
  if (content->IsString()) return content->str;
  if (!content->IsArray()) return out;
  for (const io::Json& part : content->array) {
    const io::Json* text = part.Find("text");
    if (text != nullptr && text->IsString()) {
      out += text->str;
      continue;
    }
    const io::Json* iu = part.Find("image_url");
    if (iu != nullptr) {
      std::string url;
      if (iu->IsObject()) {
        url = iu->GetString("url", "");
      } else if (iu->IsString()) {
        url = iu->str;
      }
      std::string bytes;
      if (!DecodeImageUrl(url, &bytes, err)) return std::string();
      VisionItem item;
      item.kind = VisionItem::kImage;
      item.frames.push_back(std::move(bytes));
      items->push_back(std::move(item));
      out += "<|image_pad|>";
      continue;
    }
    const io::Json* vf = part.Find("video_frames");
    if (vf != nullptr) {
      if (!vf->IsArray()) {
        if (err) *err = "video_frames must be an array of base64 data urls";
        return std::string();
      }
      VisionItem item;
      item.kind = VisionItem::kVideo;
      for (const io::Json& f : vf->array) {
        std::string url;
        if (f.IsObject()) {
          url = f.GetString("url", "");
        } else if (f.IsString()) {
          url = f.str;
        }
        std::string bytes;
        if (!DecodeImageUrl(url, &bytes, err)) return std::string();
        item.frames.push_back(std::move(bytes));
      }
      if (item.frames.empty()) {
        if (err) *err = "video_frames is empty";
        return std::string();
      }
      items->push_back(std::move(item));
      out += "<|video_pad|>";
      continue;
    }
    // unknown part type: skip
  }
  return out;
}

}  // namespace

ChatServer::~ChatServer() {
  // Release any requests queued in AllocSeqId so their threads can exit.
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    seq_stopping_ = true;
  }
  seq_cv_.notify_all();
  // B2b: stop the scheduler thread FIRST (it touches model_ + d_sched_logits_
  // under model_mu_, so it must be joined before those are freed).
  StopScheduler();
  if (d_sched_logits_) {
    cudaFree(d_sched_logits_);
    d_sched_logits_ = nullptr;
  }
  if (d_sched_tokens_) {
    cudaFree(d_sched_tokens_);
    d_sched_tokens_ = nullptr;
  }
  if (d_prefill_logits_) {
    cudaFree(d_prefill_logits_);
    d_prefill_logits_ = nullptr;
  }
  if (vision_tower_) {
    vision_tower_->Free();
    vision_tower_.reset();
  }
  // MTP draft model: free its device weights before model_ (the borrowed
  // embed/lm_head point into model_'s memory). Then the rolling draft-trunk
  // buffers.
  if (mtp_loaded_) {
    mtp_.Free();
    mtp_loaded_ = false;
  }
}

namespace {
// Per-phase startup timer: prints "[q4t][startup] <phase> <ms> ms" on scope
// exit. Industrial-grade startup observability — makes it trivial to see which
// phase dominates and to track startup-time regressions.
struct PhaseTimer {
  const char* phase;
  std::chrono::steady_clock::time_point t0;
  explicit PhaseTimer(const char* p) : phase(p), t0(std::chrono::steady_clock::now()) {}
  ~PhaseTimer() {
    const double ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
            .count();
    std::fprintf(stderr, "[q4t][startup] %s %.0f ms\n", phase, ms);
  }
};
}  // namespace

Status ChatServer::Start(const ServerOptions& opts) {
  PhaseTimer total("startup_total");
  {
    PhaseTimer pt("tokenizer");
    text::TokenizerLimits limits;
    Status s = text::Tokenizer::Load(opts.model_dir + "/tokenizer.json", limits,
                                     &tok_);
    if (!s.ok()) {
      return Status::Fail("tokenizer load failed: " + s.message());
    }
  }
  Status s;

  // OOM-safe memory budget (vllm-style gpu_memory_utilization). Compute the
  // maximum (max_len, max_seq) that fits within mem_fraction x MemTotal BEFORE
  // any allocation, and cap the user's request to it. This is what makes an
  // over-aggressive config (e.g. --max-len 262144 --max-seq 8) safe instead of
  // OOM-rebooting the unified-memory box. Skipped when opts.no_budget is set.
  {
    PhaseTimer pt("budget");
    if (!opts.no_budget) {
    const size_t mem_total = runtime::ReadMemTotal();
    if (mem_total == 0) {
      return Status::Fail("could not read MemTotal from /proc/meminfo");
    }
    // Weights: the index's total_size is the exact GPU weight byte count
    // (main + MTP + vision). Open the index read-only just to read it.
    size_t weights = 0;
    {
      io::WeightIndex* idx = nullptr;
      if (io::WeightIndex::Open(
               opts.model_dir + "/model.safetensors.index.json", &idx)
              .ok()) {
        weights = idx->total_size();
        delete idx;
      }
    }
    if (weights == 0) {
      return Status::Fail(
          "could not read weight total_size from model index (budget)");
    }
    runtime::BudgetRequest breq;
    breq.mem_fraction = opts.mem_fraction;
    breq.max_len = opts.max_len;
    breq.max_seq = opts.max_seq > 0 ? opts.max_seq : 8;
    breq.max_prefill =
        opts.max_prefill > 0 ? opts.max_prefill : 8192;
    budget_ = runtime::ComputeMemoryBudget(runtime::BudgetModelParams{},
                                           breq, weights, mem_total);
    budget_valid_ = true;
    std::fputs(budget_.report.c_str(), stderr);
    }
  }  // budget phase

  // Effective (possibly budget-capped) max_len / max_seq.
  const int eff_max_len =
      budget_valid_ && budget_.max_len > 0 ? budget_.max_len : opts.max_len;
  const int eff_max_seq =
      budget_valid_ && budget_.max_seq > 0 ? budget_.max_seq : opts.max_seq;

  model::ModelConfig cfg;
  cfg.model_dir = opts.model_dir;
  cfg.index_path = opts.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = opts.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  if (opts.max_prefill > 0) cfg.max_prefill = opts.max_prefill;
  if (eff_max_len > 0) cfg.max_len = eff_max_len;
  // B1: pool the per-sequence recurrent state for up to max_seq concurrent
  // requests. Each in-flight request owns one seq_id.
  max_seq_ = eff_max_seq > 0 ? eff_max_seq : 8;
  cfg.max_seq = max_seq_;
  seq_free_.assign(static_cast<size_t>(max_seq_), true);
  conn_cap_ = std::max(max_seq_ * 8, 128);  // in-flight request-thread cap
  {
    PhaseTimer pt("model_load");
    s = model::LoadModel(cfg, &model_, nullptr);
    if (!s.ok()) {
      tok_.reset();
      return Status::Fail("model load failed: " + s.message());
    }
  }

  // Load the MTP draft model (optional). Borrowed embed/lm_head from the main
  // model. On failure the server falls back to plain decode (mirrors the CLI
  // --mtp behavior). Skipped entirely when opts.no_mtp is set.
  if (!opts.no_mtp) {
    PhaseTimer pt("mtp_load");
    mtp::MtpConfig mcfg;
    mcfg.mtp_dir = opts.model_dir + "/mtp";
    mcfg.max_prefill = cfg.max_prefill;  // MTP draft-extend runs over the whole
                                         // prompt; size its workspace to match.
    mcfg.max_len = cfg.max_len;  // draft full-attn KV/indexer tracks the main
                                 // sequence positions; size to the same length.
    // Stage 2c: pool the draft full-attention KV/indexer for max_seq sequences
    // so concurrent MTP requests own independent draft-state slices (Stage 1).
    mcfg.max_seq = max_seq_;
    s = mtp::LoadMtp(mcfg, model_.head.embed_tokens, model_.head.lm_head,
                     &mtp_, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "[q4t] MTP load failed (%s); plain decode only\n",
                   s.message().c_str());
    } else {
      mtp_loaded_ = true;
      std::fprintf(stderr, "[q4t] MTP loaded (k=%d max_seq=%d)\n", mtp_k_,
                   max_seq_);
    }
  }

  // Load the vision tower (multimodal). Optional: if the checkpoint has no
  // "model.visual." tensors, skip gracefully (the server then serves text
  // only and rejects image parts). The loader is read-only (mmap) and is
  // released after the weights are copied to device.
  {
    PhaseTimer pt("vision_load");
    io::WeightIndex* index = nullptr;
    s = io::WeightIndex::Open(cfg.index_path, &index);
    if (s.ok()) {
      io::WeightLoader* loader = nullptr;
      s = io::WeightLoader::Create(opts.model_dir, *index, 8, &loader);
      if (s.ok()) {
        vision::VisionConfig vcfg;  // defaults match this model's vision_config
        auto tower = std::make_unique<vision::VisionTower>();
        std::string verr;
        if (vision::LoadVision(*loader, vcfg, tower.get(), &verr, nullptr)) {
          vision_tower_ = std::move(tower);
          std::fprintf(stderr, "[q4t] vision tower loaded (%d blocks)\n",
                       vcfg.depth);
        } else {
          std::fprintf(stderr,
                       "[q4t] vision tower unavailable (%s); text-only mode\n",
                       verr.c_str());
        }
        delete loader;
      }
      delete index;
    }
  }

  port_ = opts.port;
  max_tokens_default_ = opts.max_tokens;
  max_prefill_ = cfg.max_prefill;
  max_len_ = cfg.max_len;
  model_name_ = kDefaultModelName;
  // Video processor budget comes from video_preprocessor_config.json
  // (4096 / 25165824), NOT the image budget. Same patch/merge/temporal dims.
  video_proc_cfg_ = proc_cfg_;
  video_proc_cfg_.min_pixels = 4096;
  video_proc_cfg_.max_pixels = 25165824;

  // B2b continuous batching: the scheduler's packed-logits buffer (device
  // [max_seq, vocab]) + a GPU-argmax token buffer (device [max_seq] + host
  // mirror) + the scheduler thread.
  if (cudaMalloc(reinterpret_cast<void**>(&d_sched_logits_),
                 static_cast<size_t>(max_seq_) * cfg.vocab * 2) != cudaSuccess) {
    std::fprintf(stderr, "[q4t] scheduler logits alloc failed; plain decode\n");
  } else if (cudaMalloc(reinterpret_cast<void**>(&d_sched_tokens_),
                        static_cast<size_t>(max_seq_) * sizeof(int32_t)) !=
             cudaSuccess) {
    std::fprintf(stderr, "[q4t] scheduler tokens alloc failed; plain decode\n");
    cudaFree(d_sched_logits_);
    d_sched_logits_ = nullptr;
  } else {
    h_sched_tokens_.assign(static_cast<size_t>(max_seq_), 0);
    scheduler_thread_ = std::thread([this] { SchedulerLoop(); });
    scheduler_active_ = true;
    std::fprintf(stderr, "[q4t] continuous-batching scheduler started "
                         "(max_seq=%d)\n",
                 max_seq_);
  }
  // Shared prefill-logits buffer [max_prefill, vocab], used only under
  // model_mu_ (prefills serialize there). One buffer instead of per-request
  // ~1 GB so peak memory does not scale with prompt length x concurrency.
  if (cudaMalloc(reinterpret_cast<void**>(&d_prefill_logits_),
                 static_cast<size_t>(cfg.max_prefill) * cfg.vocab * 2) !=
      cudaSuccess) {
    return Status::Fail("prefill logits buffer alloc failed (out of memory)");
  }
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
  if (::listen(listen_fd, 128) < 0) {
    const std::string err = std::strerror(errno);
    ::close(listen_fd);
    return Status::Fail("listen: " + err);
  }
  listen_fd_ = listen_fd;  // RequestStop() shutdown(2)s this to break accept
  std::fprintf(stderr, "[q4t] serving on port %d (model %s)\n", port_,
               model_name_.c_str());
  while (true) {
    sockaddr_in client{};
    socklen_t clen = sizeof(client);
    const int fd =
        ::accept(listen_fd, reinterpret_cast<sockaddr*>(&client), &clen);
    if (fd < 0) {
      if (stop_requested_.load()) break;  // RequestStop() shutdown() the socket
      if (errno == EINTR) continue;
      ::close(listen_fd);
      return Status::Fail(std::string("accept: ") + std::strerror(errno));
    }
    int nodelay = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
    // Read/write timeouts: a slow or stalled client (slowloris) must not pin a
    // request thread + seq slot indefinitely. Each blocking recv/send fails
    // after 30 s of no progress (well above a legitimate inter-write gap), so
    // ReadRequest / WriteAll return and the slot frees.
    struct timeval tv {
      30, 0
    };
    ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    // B1: handle each client on its own thread so multiple requests are
    // processed concurrently. The model forwards are serialized behind
    // model_mu_ (see HandleChat); CPU post-processing (D2H/argmax/tokenize/SSE)
    // overlaps across requests. Run() blocks in the accept loop for the
    // server's lifetime, so the ChatServer object outlives the detached
    // request threads.
    // Bound in-flight request threads: shed load (503) beyond conn_cap_ so a
    // connection flood cannot exhaust host memory with blocked threads (the
    // AllocSeqId queue absorbs bursts up to this cap).
    if (active_conns_.fetch_add(1, std::memory_order_relaxed) >= conn_cap_) {
      active_conns_.fetch_sub(1, std::memory_order_relaxed);
      SendSimple(fd, 503, "Service Unavailable",
                 "{\"error\":{\"message\":\"server overloaded, retry\"}}",
                 "application/json");
      ::close(fd);
      continue;
    }
    std::thread([this, fd]() {
      HandleClient(fd);
      ::close(fd);
      active_conns_.fetch_sub(1, std::memory_order_relaxed);
    }).detach();
  }
  // Graceful shutdown (RequestStop broke the accept loop): stop the scheduler
  // (in-flight decodes get next_token=-1 so their request threads exit),
  // release queued seq waiters, then wait for in-flight request threads to
  // drain before returning — they are detached and cannot be joined, so the
  // destructor's frees must not race a live request thread.
  std::fprintf(stderr, "[q4t] shutting down: draining in-flight requests...\n");
  StopScheduler();
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    seq_stopping_ = true;
  }
  seq_cv_.notify_all();
  for (int i = 0; i < 150 && active_conns_.load() > 0; ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(100));  // <= 15 s
  ::close(listen_fd);
  std::fprintf(stderr, "[q4t] shutdown complete (%d in-flight remaining)\n",
               active_conns_.load(std::memory_order_relaxed));
  return Status();
}

void ChatServer::HandleClient(int fd) {
  std::string method, path, body;
  if (!ReadRequest(fd, &method, &path, &body)) {
    SendSimple(fd, 400, "Bad Request", "bad request", "text/plain");
    return;
  }
  const auto t0 = std::chrono::steady_clock::now();
  Dispatch(fd, method, path, body);
  static const bool access_log = std::getenv("Q4T_ACCESS_LOG") != nullptr;
  if (access_log) {
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t0)
                          .count();
    std::fprintf(stderr, "[q4t] %s %s %.1fms\n", method.c_str(), path.c_str(),
                 ms);
  }
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
  if (path == "/metrics" && method == "GET") {
    HandleMetrics(fd);
    return;
  }
  if (path == "/v1/chat/completions" && method == "POST") {
    HandleChat(fd, body);
    return;
  }
  SendSimple(fd, 404, "Not Found",
             "{\"error\":{\"message\":\"not found\"}}", "application/json");
}

int ChatServer::AllocSeqId() {
  std::unique_lock<std::mutex> lock(seq_mu_);
  // Queue (block) until a slot frees rather than rejecting — this bounds the
  // number of in-flight requests to max_seq so GPU resources are never
  // over-committed (excess requests wait here instead of OOM-ing the box).
  seq_cv_.wait(lock, [this] {
    if (seq_stopping_) return true;
    for (int i = 0; i < max_seq_; ++i)
      if (seq_free_[static_cast<size_t>(i)]) return true;
    return false;
  });
  if (seq_stopping_) return -1;
  for (int i = 0; i < max_seq_; ++i) {
    if (seq_free_[static_cast<size_t>(i)]) {
      seq_free_[static_cast<size_t>(i)] = false;
      return i;
    }
  }
  return -1;
}

void ChatServer::FreeSeqId(int seq_id) {
  if (seq_id < 0) return;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    if (seq_id < max_seq_) seq_free_[static_cast<size_t>(seq_id)] = true;
  }
  seq_cv_.notify_one();  // wake one queued request
}

// B2b continuous batching: the central scheduler loop.
//
// Each ACTIVE plain-decode request registers its current decode token (its
// `pending` flag is set under sched_mu_ and the scheduler is signalled). The
// scheduler collects ALL pending requests, runs them in ONE packed
// ModelDecodeBatchMulti (the 84 GB of weights is read once for all B tokens
// instead of B times — the decode throughput win), copies the [B, vocab]
// logits back to the host, argmaxes each row, and wakes each request with its
// next token. The packed forward runs under model_mu_ (the shared per-forward
// scratch); the D2H + argmax run under model_mu_ too (they must be ordered
// after the forward on the default stream and before the next forward's H2D).
//
// MTP requests are NOT batched (their draft KV is a single shared buffer);
// they keep the single-sequence path in HandleChat.
void ChatServer::SchedulerLoop() {
  const int vocab = model_.cfg.vocab;
  for (;;) {
    std::vector<ActiveRequest*> pending;
    std::vector<PrefillReq*> pf;
    {
      std::unique_lock<std::mutex> lock(sched_mu_);
      sched_cv_.wait(lock, [this] {
        if (scheduler_stop_) return true;
        // Batched prefill runs opportunistically (prefills arrive as encoding
        // completes, not in lockstep), so ANY pending prefill wakes the loop.
        if (!prefill_pending_.empty()) return true;
        // Lockstep (both plain B2b and MTP plan A): run only when EVERY active
        // request of a kind has registered its step, so the packed forward is
        // B = all active requests (uniform width, like vllm/sglang) instead of
        // a ragged subset. Opportunistic "any pending" fragmented the decode
        // into many small sub-steps, each re-reading the 84 GB of weights — the
        // decode throughput killer. A straggler gates the step; a finishing
        // request removes itself from active_ + notifies so this re-evaluates.
        int active_plain = 0, pending_plain = 0;
        int active_mtp = 0, pending_mtp = 0;
        for (ActiveRequest* r : active_) {
          if (r->is_mtp) {
            ++active_mtp;
            if (r->pending) ++pending_mtp;
          } else {
            ++active_plain;
            if (r->pending) ++pending_plain;
          }
        }
        if (active_plain > 0 && pending_plain == active_plain) return true;
        return active_mtp > 0 && pending_mtp == active_mtp;
      });
      if (scheduler_stop_) {
        // Drain: wake any request that is still waiting so it can exit.
        for (ActiveRequest* r : active_) {
          r->pending = false;
          r->done = true;
          r->next_token = -1;  // sentinel: scheduler is shutting down
          r->cv.notify_one();
        }
        for (PrefillReq* p : prefill_pending_) {
          p->pending = false;
          p->done = true;
          p->ok = false;  // sentinel: scheduler is shutting down
          p->cv.notify_one();
        }
        prefill_pending_.clear();
        return;
      }
      // Collect pending prefills into one batch (up to max_seq sequences /
      // max_prefill packed tokens). Opportunistic: whatever is queued now.
      int ttot = 0;
      while (!prefill_pending_.empty() &&
             static_cast<int>(pf.size()) < max_seq_) {
        PrefillReq* p = prefill_pending_.front();
        if (ttot + p->len > max_prefill_) break;  // token budget
        ttot += p->len;
        pf.push_back(p);
        prefill_pending_.erase(prefill_pending_.begin());
      }
      for (ActiveRequest* r : active_)
        if (r->pending) pending.push_back(r);
    }

    // Batched prefill: pack pf into ONE ModelPrefillBatch (the dense weights
    // are read once for the whole batch instead of per request), then hand each
    // request its last-token logits. Runs under model_mu_ (shared per-forward
    // scratch), like the decode step below. A LONE prefill (Bp == 1) uses the
    // single-seq ModelPrefill instead, so it is bit-identical to the inline
    // path (the multi-seq full-attention indexer rounds differently from the
    // single-seq tensor-core GEMM, which can flip a near-tie token — harmless
    // but a visible change; only actual batching (Bp > 1) accepts it).
    if (!pf.empty()) {
      const int Bp = static_cast<int>(pf.size());
      Status sp;
      {
        const std::lock_guard<std::mutex> lock(model_mu_);
        if (Bp == 1) {
          model::ModelSequence tmp;
          sp = model::ModelBeginSequence(model_, &tmp, nullptr, pf[0]->seq_id);
          if (sp.ok())
            sp = model::ModelPrefill(model_, &tmp, pf[0]->ids, pf[0]->len,
                                     d_prefill_logits_, nullptr, nullptr,
                                     nullptr, pf[0]->seq_id);
          if (sp.ok()) {
            cudaMemcpyAsync(
                pf[0]->h_logits,
                d_prefill_logits_ +
                    static_cast<size_t>(pf[0]->len - 1) * vocab,
                static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost, nullptr);
            cudaStreamSynchronize(nullptr);
          }
        } else {
          std::vector<int32_t> pk_tokens;
          std::vector<int> pk_lens(Bp), pk_seq(Bp);
          std::vector<int> pk_off(Bp + 1, 0);
          for (int i = 0; i < Bp; ++i) {
            pk_lens[i] = pf[i]->len;
            pk_seq[i] = pf[i]->seq_id;
            pk_off[i + 1] = pk_off[i] + pf[i]->len;
            pk_tokens.insert(pk_tokens.end(), pf[i]->ids,
                             pf[i]->ids + pf[i]->len);
          }
          sp = model::ModelPrefillBatch(model_, pk_tokens.data(),
                                        pk_lens.data(), pk_seq.data(), Bp,
                                        d_prefill_logits_, nullptr);
          if (!sp.ok() && cudaPeekAtLastError() != cudaSuccess)
            gpu_healthy_.store(false, std::memory_order_relaxed);
          if (sp.ok()) {
            // D2H each sequence's last-token row (seq_offset[b+1]-1).
            for (int i = 0; i < Bp; ++i) {
              const int last_row = pk_off[i + 1] - 1;
              cudaMemcpyAsync(
                  pf[i]->h_logits,
                  d_prefill_logits_ + static_cast<size_t>(last_row) * vocab,
                  static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost,
                  nullptr);
            }
            cudaStreamSynchronize(nullptr);
          }
        }
      }
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        for (int i = 0; i < Bp; ++i) {
          pf[i]->pending = false;
          pf[i]->done = true;
          pf[i]->ok = sp.ok();
          pf[i]->cv.notify_one();
        }
      }
    }
    if (pending.empty()) continue;

    // Split pending into MTP (speculative) and plain (single-token) requests.
    // MTP requests are batched into ONE MtpSpeculativeStepMulti (Stage 2c:
    // batched draft loop + ModelVerifyMulti + batched extend, weights read
    // once); plain requests into ONE ModelDecodeBatchMulti (B2b). The two
    // groups run as separate forwards (both under model_mu_, sequential).
    std::vector<ActiveRequest*> mtp_reqs, plain_reqs;
    for (ActiveRequest* r : pending)
      (r->is_mtp ? mtp_reqs : plain_reqs).push_back(r);

    if (!mtp_reqs.empty()) {
      const int B = static_cast<int>(mtp_reqs.size());
      if (getenv("Q4T_SCHED_DEBUG") != nullptr)
        std::fprintf(stderr, "[q4t][sched] MTP step B=%d\n", B);
      std::vector<model::ModelSequence*> seqs(B);
      std::vector<int32_t> b_tok(B), d0(B);
      std::vector<const uint16_t*> g_in(B);
      std::vector<uint16_t*> next_g(B);
      for (int i = 0; i < B; ++i) {
        seqs[i] = mtp_reqs[i]->seq;
        b_tok[i] = mtp_reqs[i]->mtp_b;
        d0[i] = mtp_reqs[i]->mtp_d0;
        g_in[i] = mtp_reqs[i]->mtp_g;
        next_g[i] = mtp_reqs[i]->mtp_g;  // trunk rolled in place
      }
      std::vector<int32_t> accepted(static_cast<size_t>(B) * (mtp_k_ + 1));
      std::vector<int> acc_count(B, 0);
      std::vector<int32_t> next_b(B, 0), next_d0(B, 0);
      Status s;
      {
        const std::lock_guard<std::mutex> lock(model_mu_);
        s = mtp::MtpSpeculativeStepMulti(model_, mtp_, seqs.data(),
                                         b_tok.data(), d0.data(), g_in.data(),
                                         B, mtp_k_, accepted.data(),
                                         acc_count.data(), next_b.data(),
                                         next_d0.data(), next_g.data(),
                                         nullptr);
        if (!s.ok() && cudaPeekAtLastError() != cudaSuccess)
          gpu_healthy_.store(false, std::memory_order_relaxed);
      }
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        for (int i = 0; i < B; ++i) {
          ActiveRequest* r = mtp_reqs[i];
          r->pending = false;
          r->done = true;
          if (s.ok()) {
            r->mtp_accepted_count = acc_count[i];
            for (int t = 0; t < acc_count[i]; ++t)
              r->mtp_accepted[t] =
                  accepted[static_cast<size_t>(i) * (mtp_k_ + 1) + t];
            r->mtp_next_b = next_b[i];
            r->mtp_next_d0 = next_d0[i];
          } else {
            r->mtp_accepted_count = 0;  // sentinel: step failed
          }
          r->cv.notify_one();
        }
      }
    }

    if (plain_reqs.empty()) continue;
    const int B = static_cast<int>(plain_reqs.size());
    std::vector<int32_t> tokens(B);
    std::vector<int> positions(B), seq_ids(B);
    std::vector<int32_t> hist_flat(static_cast<size_t>(B) *
                                   static_cast<size_t>(
                                       model_.ple_hash.ngram_size - 1));
    for (int i = 0; i < B; ++i) {
      tokens[i] = plain_reqs[i]->token;
      positions[i] = plain_reqs[i]->position;
      seq_ids[i] = plain_reqs[i]->seq_id;
      std::copy(plain_reqs[i]->ple_hist.begin(), plain_reqs[i]->ple_hist.end(),
                hist_flat.begin() + static_cast<size_t>(i) *
                                        (model_.ple_hash.ngram_size - 1));
    }

    // Q4T_PROFILE_DECODE=1: bracket each decode step with the CUDA profiler
    // API so `nsys --capture-range=cudaProfilerApi` captures ONLY decode
    // (isolating the O(context) QSA indexer cost from prefill).
    static const bool kProfileDecode =
        std::getenv("Q4T_PROFILE_DECODE") != nullptr;
    if (kProfileDecode) cudaProfilerStart();
    Status s;
    {
      const std::lock_guard<std::mutex> lock(model_mu_);
      s = model::ModelDecodeBatchMulti(model_, tokens.data(), positions.data(),
                                       seq_ids.data(), hist_flat.data(), B,
                                       d_sched_logits_, nullptr, nullptr);
      if (!s.ok() && cudaPeekAtLastError() != cudaSuccess)
        gpu_healthy_.store(false, std::memory_order_relaxed);
      if (s.ok()) {
        // GPU argmax: [B, vocab] -> B token ids (moves the 248320-wide
        // reduction off the CPU and shrinks the D2H from B*vocab to B ints).
        s = model::ArgmaxBf16Rows(d_sched_logits_, B, vocab, d_sched_tokens_,
                                  nullptr);
        if (s.ok()) {
          cudaMemcpyAsync(h_sched_tokens_.data(), d_sched_tokens_,
                          static_cast<size_t>(B) * sizeof(int32_t),
                          cudaMemcpyDeviceToHost, nullptr);
          cudaStreamSynchronize(nullptr);
        }
      }
    }
    if (kProfileDecode) cudaProfilerStop();

    // Wake each request with its next token (or -1 on failure). The request
    // thread owns the state-machine advance (position/history) after this.
    {
      const std::lock_guard<std::mutex> lock(sched_mu_);
      for (int i = 0; i < B; ++i) {
        ActiveRequest* r = plain_reqs[i];
        r->pending = false;
        r->done = true;
        r->next_token = s.ok() ? h_sched_tokens_[i] : -1;
        r->cv.notify_one();
      }
    }
  }
}

void ChatServer::RequestStop() {
  stop_requested_.store(true, std::memory_order_relaxed);
  // shutdown(2) is async-signal-safe and wakes the blocked accept(2).
  if (listen_fd_ >= 0) ::shutdown(listen_fd_, SHUT_RDWR);
}

void ChatServer::StopScheduler() {
  {
    const std::lock_guard<std::mutex> lock(sched_mu_);
    scheduler_stop_ = true;
  }
  sched_cv_.notify_all();
  if (scheduler_thread_.joinable()) scheduler_thread_.join();
}

void MetricHistogram::Observe(double seconds) {
  int i = 0;
  while (i < kNumBuckets && seconds > kLatencyBounds[i]) ++i;
  if (i < kNumBuckets)
    bucket[i].fetch_add(1, std::memory_order_relaxed);
  else
    inf.fetch_add(1, std::memory_order_relaxed);
  count.fetch_add(1, std::memory_order_relaxed);
  if (seconds > 0)
    sum_us.fetch_add(static_cast<uint64_t>(seconds * 1e6),
                     std::memory_order_relaxed);
}

void ChatServer::HandleMetrics(int fd) {
  std::string b;
  b.reserve(2048);
  auto counter = [&](const char* name, const char* help, uint64_t v) {
    b += "# HELP "; b += name; b += ' '; b += help; b += "\n# TYPE ";
    b += name; b += " counter\n"; b += name; b += ' ';
    b += std::to_string(v); b += '\n';
  };
  auto gauge = [&](const char* name, const char* help, long v) {
    b += "# HELP "; b += name; b += ' '; b += help; b += "\n# TYPE ";
    b += name; b += " gauge\n"; b += name; b += ' ';
    b += std::to_string(v); b += '\n';
  };
  auto histogram = [&](const char* name, const char* help,
                       const MetricHistogram& h) {
    b += "# HELP "; b += name; b += ' '; b += help; b += "\n# TYPE ";
    b += name; b += " histogram\n";
    uint64_t cum = 0;
    for (int i = 0; i < MetricHistogram::kNumBuckets; ++i) {
      cum += h.bucket[i].load(std::memory_order_relaxed);
      b += name; b += "_bucket{le=\""; b += std::to_string(kLatencyBounds[i]);
      b += "\"} "; b += std::to_string(cum); b += '\n';
    }
    cum += h.inf.load(std::memory_order_relaxed);
    b += name; b += "_bucket{le=\"+Inf\"} "; b += std::to_string(cum);
    b += '\n'; b += name; b += "_sum ";
    b += std::to_string(h.sum_us.load(std::memory_order_relaxed) / 1e6);
    b += '\n'; b += name; b += "_count "; b += std::to_string(cum); b += '\n';
  };

  counter("q4t_requests_total", "Total chat requests received.",
          metrics_.requests_total.load());
  counter("q4t_requests_success_total", "Requests that finished generating.",
          metrics_.requests_success.load());
  counter("q4t_requests_error_total", "Requests rejected before generation.",
          metrics_.requests_error.load());
  counter("q4t_requests_aborted_total", "Requests aborted by client disconnect.",
          metrics_.requests_aborted.load());
  counter("q4t_prompt_tokens_total", "Total prompt tokens processed.",
          metrics_.prompt_tokens_total.load());
  counter("q4t_generation_tokens_total", "Total tokens generated.",
          metrics_.generation_tokens_total.load());

  int free_slots = 0;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    for (bool f : seq_free_)
      if (f) ++free_slots;
  }
  gauge("q4t_num_requests_running", "In-flight request threads.",
        active_conns_.load());
  gauge("q4t_seq_slots_total", "Sequence-state pool size (max_seq).", max_seq_);
  gauge("q4t_seq_slots_free", "Free sequence-state slots.", free_slots);
  gauge("q4t_gpu_healthy", "1 if no sticky CUDA error observed, else 0.",
        gpu_healthy_.load() ? 1 : 0);

  histogram("q4t_ttft_seconds", "Time to first token.", metrics_.ttft_seconds);
  histogram("q4t_e2e_seconds", "End-to-end request latency.",
            metrics_.e2e_seconds);
  histogram("q4t_queue_seconds", "Seq-slot queue wait.",
            metrics_.queue_seconds);

  SendSimple(fd, 200, "OK", b, "text/plain; version=0.0.4");
}

void ChatServer::HandleHealth(int fd) {
  int free_slots = 0;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    for (bool f : seq_free_)
      if (f) ++free_slots;
  }
  const bool healthy = gpu_healthy_.load(std::memory_order_relaxed);
  const std::string body =
      std::string("{\"status\":\"") + (healthy ? "ok" : "unhealthy") +
      "\",\"gpu_healthy\":" + (healthy ? "true" : "false") +
      ",\"requests_running\":" + std::to_string(active_conns_.load()) +
      ",\"seq_slots_free\":" + std::to_string(free_slots) +
      ",\"seq_slots_total\":" + std::to_string(max_seq_) + "}";
  SendSimple(fd, healthy ? 200 : 503, healthy ? "OK" : "Service Unavailable",
             body, "application/json");
}

void ChatServer::HandleModels(int fd) {
  const std::string body =
      "{\"object\":\"list\",\"data\":[{\"id\":\"" + model_name_ +
      "\",\"object\":\"model\",\"created\":" + std::to_string(std::time(nullptr)) +
      ",\"owned_by\":\"q4t\"}]}";
  SendSimple(fd, 200, "OK", body, "application/json");
}

bool ChatServer::RunVisionPipeline(const std::vector<VisionItem>& items,
                                   uint16_t** out_feats, int* out_num_tokens,
                                   std::vector<int>* out_counts,
                                   std::vector<std::array<int, 3>>* out_grids,
                                   std::string* err) {
  *out_feats = nullptr;
  *out_num_tokens = 0;
  if (out_grids) out_grids->clear();
  if (vision_tower_ == nullptr) {
    if (err) *err = "vision tower not loaded (text-only model)";
    return false;
  }
  if (items.empty()) return true;  // nothing to do

  const vision::VisionConfig& cfg = vision_tower_->cfg;
  const int m = cfg.spatial_merge_size;

  // 1. Process each item (CPU: decode + resize + normalize + patchify).
  //    Images use the image budget (proc_cfg_); videos use the video budget
  //    (video_proc_cfg_). Both produce the same per-patch layout [C,T,P,P]
  //    (96 floats) so they can share one VisionForward batch.
  std::vector<std::vector<float>> pixel_sets;  // per-item float32 patches
  pixel_sets.reserve(items.size());
  std::vector<vision::ImageShape> shapes;
  shapes.reserve(items.size());
  int total_L = 0;
  for (const auto& item : items) {
    std::vector<float> pix;
    vision::ImageShape sh;
    std::string perr;
    if (item.kind == VisionItem::kImage) {
      vision::ProcessedImage pi;
      if (!vision::ProcessImage(
              reinterpret_cast<const uint8_t*>(item.frames[0].data()),
              item.frames[0].size(), proc_cfg_, &pi, &perr)) {
        if (err) *err = "image process failed: " + perr;
        return false;
      }
      pix = std::move(pi.pixel_values);
      sh.h = pi.grid_h;
      sh.w = pi.grid_w;
      sh.t = pi.grid_t;
    } else {
      std::vector<const uint8_t*> fptrs;
      std::vector<size_t> flens;
      fptrs.reserve(item.frames.size());
      flens.reserve(item.frames.size());
      for (const auto& f : item.frames) {
        fptrs.push_back(reinterpret_cast<const uint8_t*>(f.data()));
        flens.push_back(f.size());
      }
      vision::ProcessedVideo pv;
      if (!vision::ProcessVideo(fptrs, flens, video_proc_cfg_, &pv, &perr)) {
        if (err) *err = "video process failed: " + perr;
        return false;
      }
      pix = std::move(pv.pixel_values);
      sh.h = pv.grid_h;
      sh.w = pv.grid_w;
      sh.t = pv.grid_t;
    }
    shapes.push_back(sh);
    total_L += sh.L();
    pixel_sets.push_back(std::move(pix));
    // Patch-unit grid (t, h, w) for the 3D MRoPE table (same order as the
    // feature rows / placeholders).
    if (out_grids) out_grids->push_back({sh.t, sh.h, sh.w});
  }

  // 2. Build the device pixel buffer (float32 -> BF16, concatenated).
  const int patch_dim = cfg.in_channels * cfg.temporal_patch_size *
                        cfg.patch_size * cfg.patch_size;  // 96
  std::vector<uint16_t> pixels_bf16(static_cast<size_t>(total_L) * patch_dim);
  {
    size_t off = 0;
    for (const auto& pix : pixel_sets) {
      const size_t n = pix.size();
      for (size_t i = 0; i < n; ++i) {
        const __nv_bfloat16 b = __float2bfloat16_rn(pix[i]);
        pixels_bf16[off + i] = *reinterpret_cast<const uint16_t*>(&b);
      }
      off += n;
    }
  }
  uint16_t* d_pixels = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_pixels),
                 pixels_bf16.size() * sizeof(uint16_t)) != cudaSuccess) {
    if (err) *err = "cudaMalloc pixels failed";
    return false;
  }
  if (cudaMemcpy(d_pixels, pixels_bf16.data(),
                 pixels_bf16.size() * sizeof(uint16_t),
                 cudaMemcpyHostToDevice) != cudaSuccess) {
    cudaFree(d_pixels);
    if (err) *err = "H2D pixels failed";
    return false;
  }

  // 3. Allocate the tower workspace + run the forward.
  if (!vision_tower_->Allocate(shapes, nullptr)) {
    cudaFree(d_pixels);
    if (err) *err = "vision Allocate failed";
    return false;
  }
  const size_t out_bytes = vision::VisionOutputBytes(cfg, shapes);
  uint16_t* d_out = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_out), out_bytes) != cudaSuccess) {
    cudaFree(d_pixels);
    if (err) *err = "cudaMalloc vision output failed";
    return false;
  }
  std::string ferr;
  if (!vision::VisionForward(*vision_tower_, d_pixels, shapes, d_out, &ferr,
                             nullptr)) {
    cudaFree(d_pixels);
    cudaFree(d_out);
    if (err) *err = "VisionForward failed: " + ferr;
    return false;
  }
  cudaFree(d_pixels);
  cudaDeviceSynchronize();

  // 4. Per-item merged-token counts (the <|image_pad|> / <|video_pad|>
  //    expansion factor). One count per VisionItem, in item (prompt position)
  //    order.
  out_counts->clear();
  int total_tokens = 0;
  for (const auto& sh : shapes) {
    const int c = (sh.h / m) * (sh.w / m) * sh.t;
    out_counts->push_back(c);
    total_tokens += c;
  }
  *out_feats = d_out;
  *out_num_tokens = total_tokens;
  return true;
}

void ChatServer::HandleChat(int fd, const std::string& body) {
  metrics_.requests_total.fetch_add(1, std::memory_order_relaxed);
  const auto t_arrive = std::chrono::steady_clock::now();
  // Any return before generation starts (validation / alloc / encode / prefill
  // failure) counts as a pre-generation error; cleared once a token is produced.
  struct ErrGuard {
    std::atomic<uint64_t>& err;
    bool ok = false;
    ~ErrGuard() {
      if (!ok) err.fetch_add(1, std::memory_order_relaxed);
    }
  } err_guard{metrics_.requests_error};
  if (!gpu_healthy_.load(std::memory_order_relaxed)) {
    SendError(fd, 503, "GPU unhealthy after a device error; restart the server");
    return;
  }
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
  // Upper bound: decode stops at max_len_ anyway (KV cache), so a huge
  // max_tokens only pins a seq slot and grows host buffers; cap it so one
  // request cannot starve the pool.
  if (max_tokens > max_len_) max_tokens = max_len_;

  // stream flag.
  const bool stream = req.GetBool("stream", false);

  // Build the prompt from the messages array. Each message's `content` may be
  // a plain string or an OpenAI-style array of parts (text + image_url +
  // video_frames). Image parts render as a <|image_pad|> placeholder (the
  // tokenizer encodes it as a single image_token_id = 248056); video parts
  // render as <|video_pad|> (video_token_id = 248057). Decoded bytes are
  // collected into `items` in content-part (prompt position) order.
  std::string prompt;
  std::vector<VisionItem> items;
  const io::Json* messages = req.GetArray("messages");
  if (messages && messages->IsArray()) {
    for (const io::Json& m : messages->array) {
      const std::string role = m.GetString("role", "");
      if (!role.empty()) {
        if (!prompt.empty()) prompt += "\n";
        prompt += role + ": ";
      }
      std::string cerr;
      prompt += RenderContent(m, &items, &cerr);
      if (!cerr.empty()) {
        SendError(fd, 400, cerr);
        return;
      }
    }
  } else {
    // Fallback: a bare "prompt" string field.
    prompt = req.GetString("prompt", "");
  }
  if (prompt.empty()) {
    SendError(fd, 400, "empty prompt");
    return;
  }

  // B1 multi-request scheduling: each request runs on its own thread. All
  // GPU work uses the DEFAULT stream (the model's per-forward scratch is a
  // single shared allocation, and per-sequence state is pooled + isolated by
  // seq_id), so model forwards are serialized behind model_mu_ for both
  // scratch safety and GPU-stream ordering. CPU post-processing (D2H, argmax,
  // tokenize, SSE) and encoding run OUTSIDE model_mu_, overlapping another
  // request's GPU forward.
  const int seq_id = AllocSeqId();  // blocks (queues) until a slot frees
  if (seq_id < 0) {
    SendError(fd, 503, "server shutting down");
    return;
  }
  metrics_.queue_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());
  // Per-request device buffers (freed by `cleanup` on any exit path). Prefill
  // logits use the shared d_prefill_logits_ (under model_mu_), not a per-
  // request buffer.
  uint16_t* d_trunk_full = nullptr;
  uint16_t* d_vfeats = nullptr;
  uint16_t* d_mtp_g = nullptr;  // MTP rolling draft trunk [hc*hs] (Stage 2c:
                                // per-request, so concurrent MTP requests don't
                                // share the legacy d_g_/d_g_next_ double buffer)
  auto cleanup = [&]() {
    if (d_trunk_full) {
      cudaFree(d_trunk_full);
      d_trunk_full = nullptr;
    }
    if (d_vfeats) {
      cudaFree(d_vfeats);
      d_vfeats = nullptr;
    }
    if (d_mtp_g) {
      cudaFree(d_mtp_g);
      d_mtp_g = nullptr;
    }
    FreeSeqId(seq_id);
  };

  // 1. Encode prompt (CPU, outside model_mu_). The tokenizer's ICU regex
  // engine is not thread-safe, so Encode is serialized behind tok_mu_.
  std::vector<std::uint32_t> prompt_u32;
  {
    const std::lock_guard<std::mutex> lock(tok_mu_);
    s = tok_->Encode(prompt, &prompt_u32);
  }
  if (!s.ok()) {
    SendError(fd, 400, "encode failed: " + s.message());
    cleanup();
    return;
  }
  std::vector<int32_t> ids(prompt_u32.begin(), prompt_u32.end());

  // 1b. Multimodal: each <|image_pad|> (image_token_id) / <|video_pad|>
  // (video_token_id) placeholder expands to the number of merged visual tokens
  // for its item, and the vision tower's features are injected in place of
  // those token embeddings (in prompt position order).
  const int img_id = model_.cfg.image_token_id;
  const int vid_id = model_.cfg.video_token_id;
  model::VisionFeatures vfeats;
  if (!items.empty()) {
    std::vector<int> counts;
    std::vector<std::array<int, 3>> grids;
    std::string verr;
    // The vision tower uses a shared workspace, so its forward is serialized
    // behind model_mu_ (the same lock that serializes the main model). The
    // pipeline runs on the default stream and self-synchronizes internally.
    {
      const std::lock_guard<std::mutex> lock(model_mu_);
      if (!RunVisionPipeline(items, &d_vfeats, &vfeats.num_tokens, &counts,
                             &grids, &verr)) {
        SendError(fd, 400, verr);
        cleanup();
        return;
      }
    }
    vfeats.grids = std::move(grids);
    // Split the per-item counts (in item order) into image / video counts,
    // matching the placeholder order in the encoded prompt.
    std::vector<int> img_counts, vid_counts;
    for (size_t i = 0; i < items.size(); ++i) {
      if (items[i].kind == VisionItem::kImage) img_counts.push_back(counts[i]);
      else vid_counts.push_back(counts[i]);
    }
    std::vector<int32_t> expanded;
    if (!model::ExpandMultimodalTokens(ids.data(), static_cast<int>(ids.size()),
                                       img_id, vid_id, img_counts, vid_counts,
                                       &expanded)) {
      SendError(fd, 400, "multimodal count mismatch in prompt");
      cleanup();
      return;
    }
    ids = std::move(expanded);
    vfeats.device = d_vfeats;
  }
  const int T = static_cast<int>(ids.size());
  if (T >= max_len_) {
    SendError(fd, 400,
              "prompt too long for context: " + std::to_string(T) +
                  " tokens >= " + std::to_string(max_len_) + " max_len");
    cleanup();
    return;
  }

  const int vocab = model_.cfg.vocab;
  const int eos = static_cast<int>(model_.cfg.eos_token_id);
  // Chunked prefill (262K context, see PHASES.md 262K memory budget): the
  // forward workspace (d_ws, d_trunk, ...) is sized for max_prefill tokens,
  // so a prompt longer than max_prefill is run in chunks of max_prefill.
  // max_prefill is now the CHUNK size, not a prompt cap (the prompt cap is
  // max_len). Text-only: a vision prompt's special (t,h,w) rope is not
  // reproducible by the continuation chunks' text rope, so vision + chunked
  // is rejected (vision prompts are far shorter than 262K in practice).
  const int chunk = max_prefill_;
  const bool chunked = T > chunk;
  if (chunked && vfeats.num_tokens > 0) {
    SendError(fd, 400,
              "chunked prefill (prompt > max_prefill) does not support "
              "vision input");
    cleanup();
    return;
  }
  // Chunk 0 is a true prefill (state reset + rope table + PLE history);
  // chunks 1.. are ModelDecodeBatch calls that CONTINUE from the existing
  // per-layer state (linear SSM/conv recurrence, full-attention KV/indexer
  // written at absolute positions) — bit-identical to one big prefill. Only
  // the LAST chunk's final logits row is needed (first decode token), so
  // d_logits holds ONE chunk's rows, not T (a 262K one-shot [T, vocab]
  // buffer would be ~130 GB).
  // Prefill logits go to the shared d_prefill_logits_ [max_prefill, vocab]
  // (chunk <= max_prefill), so there is no per-request logits allocation.
  // MTP: prefill trunk_out buffer (pre-final-mixer multi stream [T, hc*hs])
  // for the draft-extend. Allocated when MTP is loaded, for BOTH one-shot and
  // chunked prefill. The chunked path accumulates the trunk across chunks
  // (each chunk's trunk_out writes its [base, base+c) rows); the draft-extend
  // then runs over the full prompt in chunks (MtpDraftExtend's chunked path).
  // The draft KV/indexer is allocated at MTP load time (pooled over max_seq,
  // max_len), so it is NOT a per-request cost -- the only per-request extra is
  // this trunk buffer (T * hc_dim * 2, ~0.9 GB at 44K, ~5.4 GB at 262K). If
  // the allocation fails (not enough headroom), fall back to plain decode.
  const size_t trunk_hc_dim =
      mtp_loaded_
          ? static_cast<size_t>(mtp_.cfg.hc) * static_cast<size_t>(mtp_.cfg.hs)
          : 0;
  if (mtp_loaded_) {
    // OOM preflight (runtime). On unified memory a large cudaMalloc does not
    // merely fail — it can push MemFree to zero and trip the global OOM
    // killer (the 2026-09-19 reboot). So before attempting the trunk
    // allocation, gauge MemFree (which excludes reclaimable page cache) and
    // fall back to plain decode if there is not enough headroom. The trunk
    // buffer (T * hc_dim * 2) plus the MTP draft logits (max_prefill * vocab *
    // 2, allocated inside MtpDraftExtend) plus a 2 GB safety margin must fit.
    const size_t trunk_bytes = static_cast<size_t>(T) * trunk_hc_dim * 2;
    const size_t draft_logits_bytes =
        static_cast<size_t>(max_prefill_) *
        static_cast<size_t>(model_.cfg.vocab) * 2;
    const size_t kPreflightMargin = 2u * 1024u * 1024u * 1024u;  // 2 GB
    const size_t need = trunk_bytes + draft_logits_bytes + kPreflightMargin;
    // MemAvailable (NOT MemFree): it includes reclaimable page cache, which is
    // exactly what the kernel evicts to satisfy a fresh cudaMalloc. In steady
    // state MemFree is only a few GB (weights + model-file cache fill it) but
    // MemAvailable is ~120 GB, so MemFree would wrongly fall back on every
    // request. MemAvailable < need means even cache eviction cannot satisfy
    // the allocation -> the OOM killer would trip, so degrade to plain decode.
    const size_t mem_avail = runtime::ReadMemAvailable();
    if (mem_avail > 0 && mem_avail < need) {
      std::fprintf(
          stderr,
          "[q4t] OOM preflight: MemAvailable=%.2f GB < need=%.2f GB (trunk "
          "T=%d); plain decode\n",
          mem_avail / 1e9, need / 1e9, T);
      d_trunk_full = nullptr;
    } else if (cudaMalloc(reinterpret_cast<void**>(&d_trunk_full),
                          trunk_bytes) != cudaSuccess) {
      std::fprintf(stderr,
                   "[q4t] trunk alloc failed for T=%d; plain decode\n", T);
      d_trunk_full = nullptr;
    }
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

  // 2. Prefill (PD-ready 阶段边界 API: Begin -> Prefill). The forward uses the
  // shared per-forward scratch, so it is serialized behind model_mu_; the
  // per-sequence recurrent state is isolated by seq_id. All GPU work uses the
  // default stream, so the forward is ordered with the D2H below.
  model::ModelSequence seq;
  const model::VisionFeatures* vptr =
      (vfeats.num_tokens > 0) ? &vfeats : nullptr;
  // Batched prefill (plain path): non-vision, non-chunked, non-MTP requests
  // pack their prefill with other concurrent requests into ONE
  // ModelPrefillBatch (the dense weights read once for the whole batch instead
  // of per request). MTP needs the prefill trunk_out (draft-extend) and
  // vision/chunked need the per-request path, so those still prefill inline.
  const bool batched_prefill =
      scheduler_active_ && !mtp_loaded_ && !chunked && vptr == nullptr &&
      getenv("Q4T_NO_BATCH_PREFILL") == nullptr;
  if (batched_prefill) {
    PrefillReq pr;
    pr.seq_id = seq_id;
    pr.ids = ids.data();
    pr.len = T;
    pr.h_logits = h_logits.data();
    {
      std::unique_lock<std::mutex> lock(sched_mu_);
      pr.pending = true;
      prefill_pending_.push_back(&pr);
      sched_cv_.notify_one();
      pr.cv.wait(lock, [&] { return pr.done; });
    }
    s = pr.ok ? Status() : Status::Fail("batched prefill failed");
    if (s.ok()) {
      // ModelPrefillBatch reset + populated this sequence's per-layer state and
      // the scheduler D2H'd the last-token row into h_logits; the ModelSequence
      // is host-only, so set up its decode state machine here.
      seq.stage = model::ModelSequence::Stage::kDecode;
      seq.position = T;
      seq.seq_id = seq_id;
      seq.history.assign(ids.begin(), ids.end());
    }
  } else {
    int last_chunk_c = 0;  // token count of the final chunk (D2H row offset)
    const std::lock_guard<std::mutex> lock(model_mu_);
    s = model::ModelBeginSequence(model_, &seq, nullptr, seq_id);
    if (s.ok()) {
      if (!chunked) {
        // One-shot prefill (T <= max_prefill): unchanged path.
        s = model::ModelPrefill(model_, &seq, ids.data(), T, d_prefill_logits_,
                                nullptr, mtp_loaded_ ? d_trunk_full : nullptr,
                                vptr, seq_id);
      } else {
        // Chunked prefill (T > max_prefill, 262K context). Chunk 0 is a true
        // prefill (state reset + rope table + PLE history via ModelPrefill);
        // chunks 1.. are ModelDecodeBatch calls that CONTINUE from the
        // existing per-layer state (linear SSM/conv recurrence, full-attention
        // KV/indexer written at absolute positions) — bit-identical to one big
        // prefill. Intermediate chunks pass logits=nullptr to skip the
        // lm_head GEMM (only the last chunk's final row is needed for the
        // first decode token). Text-only: a vision prompt's special (t,h,w)
        // rope is not reproducible by ModelDecodeBatch's text rope.
        s = model::ModelPrefill(model_, &seq, ids.data(), chunk,
                                d_prefill_logits_, nullptr, d_trunk_full,
                                nullptr, seq_id);
        for (int base = chunk; s.ok() && base < T; base += chunk) {
          const int c = std::min(chunk, T - base);
          const bool last = (base + c >= T);
          if (last) last_chunk_c = c;
          s = model::ModelDecodeBatch(
              model_, ids.data() + base, c, base, ids.data(), base,
              last ? d_prefill_logits_ : nullptr, nullptr,
              d_trunk_full
                  ? d_trunk_full + static_cast<size_t>(base) * trunk_hc_dim
                  : nullptr,
              false, seq_id);
        }
        // ModelDecodeBatch does NOT advance the sequence state machine (it is
        // a bare forward, unlike ModelDecodeStepSeq). After the chunked
        // prefill the sequence must be at position T with the FULL prompt as
        // PLE history, so the decode loop (ModelDecodeStepSeq / MTP) starts
        // from the right place. Chunk 0's ModelPrefill left position=chunk and
        // history=first-chunk-only; fix both here.
        if (s.ok()) {
          seq.position = T;
          seq.history.assign(ids.begin(), ids.end());
        }
      }
    }
    // First decode token: D2H the last prefill row into h_logits WHILE holding
    // model_mu_, so the shared d_prefill_logits_ is safe from the next prefill.
    if (s.ok()) {
      const uint16_t* last_logits =
          chunked ? d_prefill_logits_ +
                        static_cast<size_t>(last_chunk_c - 1) * vocab
                  : d_prefill_logits_ + static_cast<size_t>(T - 1) * vocab;
      cudaMemcpyAsync(h_logits.data(), last_logits,
                      static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost,
                      nullptr);
      cudaStreamSynchronize(nullptr);
    }
  }
  // DIAG: the prefill kernels are async; surface any launch/async error now
  // (otherwise it shows up later at the next sync point as a misleading
  // "memset"/"H2D" failure).
  {
    const cudaError_t se = cudaStreamSynchronize(nullptr);
    const cudaError_t le = cudaGetLastError();
    if (se != cudaSuccess || le != cudaSuccess) {
      std::fprintf(stderr,
                   "[q4t][diag] prefill seq_id=%d T=%d streamSync=%s "
                   "lastErr=%s\n",
                   seq_id, T, cudaGetErrorString(se), cudaGetErrorString(le));
    }
  }
  if (!s.ok()) {
    model::ModelEndSequence(&seq);
    cleanup();
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

  // First decode token comes from the prefill's LAST position. One-shot
  // prefill: row T-1 of d_logits. Chunked prefill: the last chunk wrote its
  // final row to row 0 of d_logits (ModelDecodeBatch's T=c rows start at
  // row 0). The D2H (outside the lock) runs on the default stream;
  // synchronize before the host argmax so h_logits is fully populated (the
  // original serial code relied on the next forward's H2D to implicitly
  // order this, but the lock now separates them).
  // h_logits holds the last prefill row (D2H'd under the model_mu_ lock above).
  next_token = argmax(h_logits.data());
  metrics_.ttft_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());
  err_guard.ok = true;  // generation has started; not a pre-generation error

  // MTP init (mirrors the CLI --mtp path): fresh draft KV, bonus token b =
  // t_P (the first decode token), draft-extend over the prompt to build the
  // draft KV[0..P-1] and seed the first speculative step (d0 + g). On any
  // failure fall back to plain decode (the main seq is still usable).
  //
  // Stage 2c (4b): the MTP path is per-sequence safe AND batched. The draft
  // KV is pooled per seq_id (mcfg.max_seq, Stage 1) and each request owns a
  // per-request rolling trunk (d_mtp_g). MTP init (per-seq reset + draft
  // extend) runs on this thread under model_mu_; the speculative STEPS are
  // registered with the central scheduler, which batches all concurrent MTP
  // requests into ONE MtpSpeculativeStepMulti (batched draft loop +
  // ModelVerifyMulti + batched extend, weights read once) — the same code path
  // a single MTP request takes (B=1). This thread advances the main seq over
  // the accepted prefix (the multi step does not touch seqs[b]).
  // MTP is enabled for BOTH one-shot and chunked prefill, as long as the
  // full-prompt trunk buffer was allocated (d_trunk_full != nullptr). The
  // chunked path accumulates the trunk across prefill chunks and runs the
  // draft-extend in chunks (MtpDraftExtend's chunked path). The draft
  // KV/indexer is allocated at MTP load time (not per-request), so the only
  // per-request cost is the trunk buffer; if its allocation failed, d_trunk_full
  // is null and we fall back to plain decode. MTP is precision-safe: the main
  // model verifies every draft token, so the output is identical to pure
  // greedy (only the throughput changes).
  bool use_mtp = mtp_loaded_ && d_trunk_full != nullptr;
  int32_t mtp_b = -1, mtp_d0 = -1;
  if (use_mtp) {
    if (cudaMalloc(reinterpret_cast<void**>(&d_mtp_g),
                   static_cast<size_t>(mtp_.cfg.hc) *
                       static_cast<size_t>(mtp_.cfg.hs) * 2) != cudaSuccess) {
      std::fprintf(stderr, "[q4t] MTP trunk alloc failed; plain decode\n");
      use_mtp = false;
    }
  }
  if (use_mtp) {
    {
      const std::lock_guard<std::mutex> lock(model_mu_);
      s = mtp::MtpResetState(mtp_, nullptr, seq_id);
      if (s.ok()) {
        mtp_b = next_token;
        // EAGLE shift: shifted_ids[p] = t_{p+1}, with t_P := b at the tail.
        std::vector<int32_t> shifted(T);
        for (int i = 0; i < T - 1; ++i) shifted[i] = ids[i + 1];
        shifted[T - 1] = mtp_b;
        std::vector<int> pos(T);
        for (int i = 0; i < T; ++i) pos[i] = i;
        s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full, pos.data(),
                                T, &mtp_d0, d_mtp_g, nullptr, seq_id);
        if (s.ok()) {
          s = model::ModelReserveVerifyCheckpoints(model_, mtp_k_);
          if (s.ok())
            s = mtp::MtpReserveScratch(mtp_, mtp_k_ + 1);
        }
      }
      if (!s.ok()) {
        std::fprintf(stderr,
                     "[q4t] MTP init failed (%s); plain decode for this "
                     "request\n",
                     s.message().c_str());
        use_mtp = false;
      }

    }
  }
  // The prompt trunk is only needed for the draft-extend above; free it now so
  // decoding requests do not hold it (bounds concurrent memory).
  if (d_trunk_full) {
    cudaFree(d_trunk_full);
    d_trunk_full = nullptr;
  }
  // Stage 2c (4b): the speculative steps are driven by the central scheduler,
  // which batches all concurrent MTP requests into ONE MtpSpeculativeStepMulti
  // (weights read once). This thread registers each step's input (mtp_b/d0/g),
  // blocks on the request's cv, and is woken with the step's output. It owns
  // the state-machine advance (position/history) over the accepted prefix.
  // Fallback: if the scheduler is unavailable or the pool is full, decode
  // plainly (the main seq is still usable).
  ActiveRequest mtp_ar;
  bool mtp_sched = false;
  if (use_mtp) {
    mtp_sched = scheduler_active_ && d_sched_logits_ &&
                static_cast<int>(active_.size()) < max_seq_;
    if (mtp_sched) {
      mtp_ar.seq_id = seq_id;
      mtp_ar.is_mtp = true;
      mtp_ar.seq = &seq;
      mtp_ar.mtp_g = d_mtp_g;
      mtp_ar.ple_hist.resize(
          static_cast<size_t>(model_.ple_hash.ngram_size - 1), eos);
      const std::lock_guard<std::mutex> lock(sched_mu_);
      active_.push_back(&mtp_ar);
    }
  }
  if (use_mtp) {
    bool done = false;
    while (!done && static_cast<int>(generated.size()) < max_tokens) {
      if (!mtp_sched) break;
      // Register this step's input with the scheduler.
      mtp_ar.mtp_b = mtp_b;
      mtp_ar.mtp_d0 = mtp_d0;
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        mtp_ar.pending = true;
        mtp_ar.done = false;
      }
      sched_cv_.notify_one();
      // Block until the scheduler's batched step yields this step's output.
      {
        std::unique_lock<std::mutex> lock(sched_mu_);
        mtp_ar.cv.wait(lock, [&mtp_ar] { return mtp_ar.done; });
      }
      if (mtp_ar.mtp_accepted_count <= 0) {
        // Scheduler failed the step or is shutting down.
        finish_reason = "stop";
        break;
      }
      for (int i = 0; i < mtp_ar.mtp_accepted_count &&
                          static_cast<int>(generated.size()) < max_tokens;
           ++i) {
        const int32_t tok_id = mtp_ar.mtp_accepted[i];
        generated.push_back(tok_id);
        if (stream) {
          std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
          std::string piece;
          bool wrote = true;
          {
            const std::lock_guard<std::mutex> lock(tok_mu_);
            if (tok_->Decode(one, true, &piece).ok())
              wrote = WriteAll(fd, SseChunk(id, model_field, "", piece, "", 0));
          }
          if (!wrote) {  // client disconnected: stop, free the slot
            metrics_.requests_aborted.fetch_add(1, std::memory_order_relaxed);
            done = true;
            finish_reason = "stop";
            break;
          }
        }
        if (tok_id == eos) {
          done = true;
          break;
        }
      }
      // Advance the main seq over the accepted prefix [b, d_0..d_{a-1}] (the
      // multi step does not touch seqs[b]).
      seq.position += mtp_ar.mtp_accepted_count;
      for (int i = 0; i < mtp_ar.mtp_accepted_count; ++i)
        seq.history.push_back(mtp_ar.mtp_accepted[i]);
      if (static_cast<int>(generated.size()) >= max_tokens) {
        finish_reason = "length";
        break;
      }
      if (seq.position + 1 >= max_len_) {
        finish_reason = "length";
        break;  // cannot decode further without exceeding the KV cache
      }
      mtp_b = mtp_ar.mtp_next_b;
      mtp_d0 = mtp_ar.mtp_next_d0;
      // d_mtp_g was overwritten in place with the next step's trunk.
    }
  }
  if (mtp_sched) {
    {
      const std::lock_guard<std::mutex> lock(sched_mu_);
      active_.erase(std::remove(active_.begin(), active_.end(), &mtp_ar),
                    active_.end());
    }
    // Lockstep predicate depends on active_ size: removing a request can
    // flip "pending_mtp == active_mtp" from false to true. Notify the
    // scheduler so it re-evaluates (fixes a lost-wakeup deadlock where the
    // departing request's notify was consumed before the scheduler slept).
    sched_cv_.notify_one();
  }
  if (!use_mtp) {
    // B2b continuous batching: this request's decode steps are driven by the
    // central scheduler, which packs the current token of EVERY active
    // plain-decode request into ONE ModelDecodeBatchMulti (weights read once
    // for all B tokens). This thread only emits tokens and advances the
    // per-sequence state machine; the GPU forward is the scheduler's job.
    //
    // Fallback: if the scheduler is unavailable (buffer alloc failed) or the
    // active pool is full, this request decodes on its own via
    // ModelDecodeStepSeq (the B1 single-sequence path).
    const bool use_sched =
        scheduler_active_ && d_sched_logits_ &&
        static_cast<int>(active_.size()) < max_seq_;
    ActiveRequest ar;
    if (use_sched) {
      ar.seq_id = seq_id;
      ar.ple_hist.resize(static_cast<size_t>(model_.ple_hash.ngram_size - 1),
                         eos);
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        active_.push_back(&ar);
      }
    }
    for (int step = 0; step < max_tokens; ++step) {
      generated.push_back(next_token);
      if (next_token == eos) {
        finish_reason = "stop";
        break;
      }
      if (step == max_tokens - 1) finish_reason = "length";
      const int32_t tok_id = next_token;
      // Emit the token text (decode this single token).
      if (stream) {
        std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
        std::string piece;
        bool wrote = true;
        {
          const std::lock_guard<std::mutex> lock(tok_mu_);
          if (tok_->Decode(one, true, &piece).ok())
            wrote = WriteAll(fd, SseChunk(id, model_field, "", piece, "", 0));
        }
        if (!wrote) {  // client disconnected: stop, free the slot
          metrics_.requests_aborted.fetch_add(1, std::memory_order_relaxed);
          finish_reason = "stop";
          break;
        }
      }
      if (seq.position + 1 >= max_len_) {
        finish_reason = "length";
        break;  // cannot decode further without exceeding the KV cache
      }
      if (use_sched) {
        // Register this step's token with the scheduler (position + PLE
        // context are read from the per-sequence state BEFORE advancing).
        ar.position = seq.position;
        ar.token = tok_id;
        const int hist_w = model_.ple_hash.ngram_size - 1;
        const size_t hsz = seq.history.size();
        for (int j = 0; j < hist_w; ++j) {
          const long src = static_cast<long>(hsz) - (hist_w - j);
          ar.ple_hist[j] = (src >= 0) ? seq.history[static_cast<size_t>(src)]
                                      : static_cast<int32_t>(eos);
        }
        {
          const std::lock_guard<std::mutex> lock(sched_mu_);
          ar.pending = true;
          ar.done = false;
        }
        sched_cv_.notify_one();
        // Block until the scheduler's packed forward yields this step's token.
        {
          std::unique_lock<std::mutex> lock(sched_mu_);
          ar.cv.wait(lock, [&ar] { return ar.done; });
        }
        if (ar.next_token < 0) {
          // Scheduler failed the forward or is shutting down.
          finish_reason = "stop";
          break;
        }
        next_token = ar.next_token;
        // Advance the per-sequence state machine (owned by this thread).
        seq.position = ar.position + 1;
        seq.history.push_back(tok_id);
      } else {
        // Fallback: single-sequence decode (B1 path).
        const std::lock_guard<std::mutex> lock(model_mu_);
        s = model::ModelDecodeStepSeq(model_, &seq, tok_id, d_prefill_logits_,
                                      nullptr, nullptr, seq_id);
        if (!s.ok()) {
          finish_reason = "stop";
          break;
        }
        cudaMemcpyAsync(h_logits.data(), d_prefill_logits_,
                        static_cast<size_t>(vocab) * 2,
                        cudaMemcpyDeviceToHost, nullptr);
        cudaStreamSynchronize(nullptr);
        next_token = argmax(h_logits.data());
      }
    }
    if (use_sched) {
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        active_.erase(std::remove(active_.begin(), active_.end(), &ar),
                      active_.end());
      }
      // Lockstep depends on active_ size: a departing request can flip
      // "all active pending" to true, so wake the scheduler to re-evaluate.
      sched_cv_.notify_one();
    }
  }
  model::ModelEndSequence(&seq);

  metrics_.requests_success.fetch_add(1, std::memory_order_relaxed);
  metrics_.prompt_tokens_total.fetch_add(static_cast<uint64_t>(T),
                                         std::memory_order_relaxed);
  metrics_.generation_tokens_total.fetch_add(generated.size(),
                                             std::memory_order_relaxed);
  metrics_.e2e_seconds.Observe(
      std::chrono::duration<double>(std::chrono::steady_clock::now() - t_arrive)
          .count());

  // 4. Finalize.
  if (stream) {
    WriteAll(fd, SseChunk(id, model_field, "", "", finish_reason, 0));
    WriteAll(fd, "data: [DONE]\r\n\r\n");
  } else {
    std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
    std::string text;
    bool decoded = false;
    {
      const std::lock_guard<std::mutex> lock(tok_mu_);
      decoded = tok_->Decode(gen_u32, true, &text).ok();
    }
    if (decoded) {
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

  cleanup();
}

}  // namespace server
}  // namespace q4t
