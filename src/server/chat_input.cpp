// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>
#include <cuda_bf16.h>
#include <cuda_runtime.h>
#include "q4t/server/chat_template.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/vision.h"

namespace q4t::server::detail {
namespace {
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

// Render and collect content once, preserving model vision boundaries and
// item order. HTTP video_frames is the transport adapter for a video item.
std::string RenderContent(const io::Json& m, std::vector<VisionItem>* items,
                          std::string* err, bool add_vision_id,
                          int* image_count, int* video_count) {
  std::string out;
  const io::Json* content = m.Find("content");
  if (content == nullptr || content->IsNull()) return out;
  if (content->IsString()) return content->str;
  if (!content->IsArray()) {
    *err = "Unexpected content type.";
    return out;
  }
  for (const io::Json& part : content->array) {
    const io::Json* iu = part.Find("image_url");
    if (iu != nullptr) {
      if (m.GetString("role") == "system") {
        *err = "System message cannot contain images.";
        return {};
      }
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
      ++*image_count;
      if (add_vision_id) out += "Picture " + std::to_string(*image_count) + ": ";
      out += "<|vision_start|><|image_pad|><|vision_end|>";
      continue;
    }
    const io::Json* vf = part.Find("video_frames");
    if (vf != nullptr) {
      if (m.GetString("role") == "system") {
        *err = "System message cannot contain videos.";
        return {};
      }
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
      ++*video_count;
      if (add_vision_id) out += "Video " + std::to_string(*video_count) + ": ";
      out += "<|vision_start|><|video_pad|><|vision_end|>";
      continue;
    }
    const io::Json* text = part.Find("text");
    if (text && text->IsString()) {
      out += text->str;
      continue;
    }
    *err = "Unexpected item type in content.";
    return {};
  }
  return out;
}

}

bool PrepareChatInput(int fd, const io::Json& req, std::string* prompt_out,
                      std::vector<VisionItem>* items_out) {
  // Decode transport content once; the model template owns role boundaries,
  // reasoning history and tool serialization. Bare prompts remain verbatim.
  std::string& prompt = *prompt_out;
  std::vector<VisionItem>& items = *items_out;
  const io::Json* messages = req.Find("messages");
  if (messages) {
    if (!messages->IsArray()) {
      SendError(fd, 400, "messages must be an array");
      return false;
    }
    const io::Json* options = req.Find("chat_template_kwargs");
    if (options && !options->IsObject()) {
      SendError(fd, 400, "chat_template_kwargs must be an object");
      return false;
    }
    const io::Json* vision_id = options ? options->Find("add_vision_id") : nullptr;
    if (!vision_id) vision_id = req.Find("add_vision_id");
    if (vision_id && !vision_id->IsBool()) {
      SendError(fd, 400, "add_vision_id must be boolean");
      return false;
    }
    int image_count = 0, video_count = 0;
    std::vector<std::string> contents;
    for (const io::Json& m : messages->array) {
      std::string error;
      contents.push_back(RenderContent(m, &items, &error,
                                      vision_id && vision_id->boolean,
                                      &image_count, &video_count));
      if (!error.empty()) {
        SendError(fd, 400, error);
        return false;
      }
    }
    Status rendered = RenderChatPrompt(req, contents, &prompt);
    if (!rendered) {
      SendError(fd, 400, rendered.message());
      return false;
    }
  } else {
    prompt = req.GetString("prompt", "");
  }
  if (prompt.empty()) {
    SendError(fd, 400, "empty prompt");
    return false;
  }

  return true;
}
}  // namespace q4t::server::detail

namespace q4t::server {
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

}  // namespace q4t::server
