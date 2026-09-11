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

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include "q4t/io/json.h"
#include "q4t/io/weight_loader.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/vision.h"

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
  if (d_g_) {
    cudaFree(d_g_);
    d_g_ = nullptr;
  }
  if (d_g_next_) {
    cudaFree(d_g_next_);
    d_g_next_ = nullptr;
  }
}

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
  if (opts.max_prefill > 0) cfg.max_prefill = opts.max_prefill;
  s = model::LoadModel(cfg, &model_, nullptr);
  if (!s.ok()) {
    tok_.reset();
    return Status::Fail("model load failed: " + s.message());
  }

  // Load the MTP draft model (optional). Borrowed embed/lm_head from the main
  // model. On failure the server falls back to plain decode (mirrors the CLI
  // --mtp behavior).
  {
    mtp::MtpConfig mcfg;
    mcfg.mtp_dir = opts.model_dir + "/mtp";
    mcfg.max_prefill = cfg.max_prefill;  // MTP draft-extend runs over the whole
                                         // prompt; size its workspace to match.
    s = mtp::LoadMtp(mcfg, model_.head.embed_tokens, model_.head.lm_head,
                     &mtp_, nullptr);
    if (!s.ok()) {
      std::fprintf(stderr, "[q4t] MTP load failed (%s); plain decode only\n",
                   s.message().c_str());
    } else {
      mtp_loaded_ = true;
      const size_t hc_dim =
          static_cast<size_t>(mtp_.cfg.hc) * static_cast<size_t>(mtp_.cfg.hs);
      if (cudaMalloc(reinterpret_cast<void**>(&d_g_), hc_dim * 2) !=
              cudaSuccess ||
          cudaMalloc(reinterpret_cast<void**>(&d_g_next_), hc_dim * 2) !=
              cudaSuccess) {
        std::fprintf(stderr,
                     "[q4t] MTP buffer alloc failed; plain decode only\n");
        mtp_.Free();
        mtp_loaded_ = false;
      } else {
        std::fprintf(stderr, "[q4t] MTP loaded (k=%d)\n", mtp_k_);
      }
    }
  }

  // Load the vision tower (multimodal). Optional: if the checkpoint has no
  // "model.visual." tensors, skip gracefully (the server then serves text
  // only and rejects image parts). The loader is read-only (mmap) and is
  // released after the weights are copied to device.
  {
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

bool ChatServer::RunVisionPipeline(const std::vector<VisionItem>& items,
                                   uint16_t** out_feats, int* out_num_tokens,
                                   std::vector<int>* out_counts,
                                   std::string* err) {
  *out_feats = nullptr;
  *out_num_tokens = 0;
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

  // 1b. Multimodal: each <|image_pad|> (image_token_id) / <|video_pad|>
  // (video_token_id) placeholder expands to the number of merged visual tokens
  // for its item, and the vision tower's features are injected in place of
  // those token embeddings (in prompt position order).
  const int img_id = model_.cfg.image_token_id;
  const int vid_id = model_.cfg.video_token_id;
  uint16_t* d_vfeats = nullptr;
  model::VisionFeatures vfeats;
  if (!items.empty()) {
    std::vector<int> counts;
    std::string verr;
    if (!RunVisionPipeline(items, &d_vfeats, &vfeats.num_tokens, &counts,
                           &verr)) {
      SendError(fd, 400, verr);
      return;
    }
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
      cudaFree(d_vfeats);
      return;
    }
    ids = std::move(expanded);
    vfeats.device = d_vfeats;
  }
  const int T = static_cast<int>(ids.size());
  if (T > max_prefill_) {
    SendError(fd, 400,
              "prompt too long: " + std::to_string(T) + " tokens > " +
                  std::to_string(max_prefill_) + " max prefill");
    cudaFree(d_vfeats);
    return;
  }
  if (T >= max_len_) {
    SendError(fd, 400,
              "prompt too long for context: " + std::to_string(T) +
                  " tokens >= " + std::to_string(max_len_) + " max_len");
    cudaFree(d_vfeats);
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
    cudaFree(d_vfeats);
    return;
  }
  // MTP: prefill trunk_out buffer (pre-final-mixer multi stream [T, hc*hs])
  // for the draft-extend. Allocated only when MTP is loaded.
  uint16_t* d_trunk_full = nullptr;
  if (mtp_loaded_) {
    const size_t hc_dim =
        static_cast<size_t>(mtp_.cfg.hc) * static_cast<size_t>(mtp_.cfg.hs);
    if (cudaMalloc(reinterpret_cast<void**>(&d_trunk_full),
                   static_cast<size_t>(T) * hc_dim * 2) != cudaSuccess) {
      cudaFree(d_logits);
      cudaFree(d_vfeats);
      SendError(fd, 500, "cudaMalloc trunk failed");
      return;
    }
  }
  // Release the per-request device buffers on any exit path.
  auto cleanup = [&]() {
    if (d_logits) {
      cudaFree(d_logits);
      d_logits = nullptr;
    }
    if (d_trunk_full) {
      cudaFree(d_trunk_full);
      d_trunk_full = nullptr;
    }
    if (d_vfeats) {
      cudaFree(d_vfeats);
      d_vfeats = nullptr;
    }
  };
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
    cleanup();
    SendError(fd, 500, "begin sequence failed: " + s.message());
    return;
  }
  const model::VisionFeatures* vptr =
      (vfeats.num_tokens > 0) ? &vfeats : nullptr;
  s = model::ModelPrefill(model_, &seq, ids.data(), T, d_logits, nullptr,
                          mtp_loaded_ ? d_trunk_full : nullptr, vptr);
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

  // First decode token comes from the prefill's LAST position (row T-1).
  cudaMemcpy(h_logits.data(), d_logits + static_cast<size_t>(T - 1) * vocab,
             static_cast<size_t>(vocab) * 2, cudaMemcpyDeviceToHost);
  next_token = argmax(h_logits.data());

  // MTP init (mirrors the CLI --mtp path): fresh draft KV, bonus token b =
  // t_P (the first decode token), draft-extend over the prompt to build the
  // draft KV[0..P-1] and seed the first speculative step (d0 + g). On any
  // failure fall back to plain decode (the main seq is still usable).
  bool use_mtp = mtp_loaded_;
  int32_t mtp_b = -1, mtp_d0 = -1;
  if (use_mtp) {
    s = mtp::MtpResetState(mtp_, nullptr);
    if (s.ok()) {
      mtp_b = next_token;
      // EAGLE shift: shifted_ids[p] = t_{p+1}, with t_P := b at the tail.
      std::vector<int32_t> shifted(T);
      for (int i = 0; i < T - 1; ++i) shifted[i] = ids[i + 1];
      shifted[T - 1] = mtp_b;
      std::vector<int> pos(T);
      for (int i = 0; i < T; ++i) pos[i] = i;
      s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full, pos.data(),
                              T, &mtp_d0, d_g_, nullptr);
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

  if (use_mtp) {
    // Speculative decode: each step emits the bonus b + accepted drafts and
    // yields the next (b, d0, g). MtpSpeculativeStep advances the main seq
    // over exactly the accepted prefix (lazy verification, no rollback).
    int32_t accepted_tokens[64];
    int accepted_count_tmp = 0;
    bool done = false;
    while (!done && static_cast<int>(generated.size()) < max_tokens) {
      int32_t next_b = -1, next_d0 = -1;
      s = mtp::MtpSpeculativeStep(model_, mtp_, &seq, mtp_b, mtp_d0, d_g_,
                                  mtp_k_, accepted_tokens, &accepted_count_tmp,
                                  &next_b, &next_d0, d_g_next_, nullptr);
      if (!s.ok() || accepted_count_tmp <= 0) break;
      for (int i = 0; i < accepted_count_tmp &&
                          static_cast<int>(generated.size()) < max_tokens;
           ++i) {
        const int32_t tok_id = accepted_tokens[i];
        generated.push_back(tok_id);
        if (stream) {
          std::vector<std::uint32_t> one(1, static_cast<std::uint32_t>(tok_id));
          std::string piece;
          if (tok_->Decode(one, true, &piece).ok())
            WriteAll(fd, SseChunk(id, model_field, "", piece, "", 0));
        }
        if (tok_id == eos) {
          done = true;
          break;
        }
      }
      if (static_cast<int>(generated.size()) >= max_tokens) {
        finish_reason = "length";
        break;
      }
      if (seq.position + 1 >= max_len_) {
        finish_reason = "length";
        break;  // cannot decode further without exceeding the KV cache
      }
      mtp_b = next_b;
      mtp_d0 = next_d0;
      uint16_t* tmp = d_g_;
      d_g_ = d_g_next_;
      d_g_next_ = tmp;
    }
  } else {
    // Plain greedy decode (baseline / MTP fallback).
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

  cleanup();
}

}  // namespace server
}  // namespace q4t
