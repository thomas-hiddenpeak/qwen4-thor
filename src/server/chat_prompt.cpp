#include "q4t/server/chat_prompt.h"

#include <cstdint>
#include <utility>

#include "q4t/server/chat_template.h"

namespace q4t::server {
namespace {
// Decode a base64 string into raw bytes. Whitespace and '=' padding are
// ignored; both the standard and URL-safe alphabets are accepted. Returns
// false on an invalid character.
bool Base64Decode(const std::string& in, std::string* out) {
  static const std::vector<int8_t> kDec = [] {
    std::vector<int8_t> t(256, -1);
    const char* a =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    for (int i = 0; i < 64; ++i)
      t[static_cast<unsigned char>(a[i])] = static_cast<int8_t>(i);
    t[static_cast<unsigned char>('-')] = 62;  // url-safe
    t[static_cast<unsigned char>('_')] = 63;
    return t;
  }();
  out->clear();
  unsigned val = 0;
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
      if (IsChatInstructionRole(m.GetString("role"))) {
        *err = "Instruction message cannot contain images.";
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
      if (add_vision_id)
        out += "Picture " + std::to_string(*image_count) + ": ";
      out += "<|vision_start|><|image_pad|><|vision_end|>";
      continue;
    }
    const io::Json* vf = part.Find("video_frames");
    if (vf != nullptr) {
      if (IsChatInstructionRole(m.GetString("role"))) {
        *err = "Instruction message cannot contain videos.";
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

}  // namespace

Status PrepareChatPrompt(const io::Json& request,
                         const OutputConstraints& constraints,
                         std::string* prompt_out,
                         std::vector<VisionItem>* items_out,
                         std::string* error_param) {
  prompt_out->clear();
  items_out->clear();
  error_param->clear();
  const auto adapted = constraints.active
                           ? ConstraintTemplateRequest(request, constraints)
                           : io::Json{};
  const auto& req = constraints.active ? adapted : request;
  const auto fail = [&](std::string message) {
    *error_param = req.Has("messages") ? "messages" : "prompt";
    return Status::Fail(std::move(message));
  };
  std::string& prompt = *prompt_out;
  std::vector<VisionItem>& items = *items_out;
  if (const auto* messages = req.Find("messages")) {
    const auto profile = ValidateChatMessageProfile(*messages, error_param);
    if (!profile) return profile;
    const auto* options = req.Find("chat_template_kwargs");
    const auto* vision_id = options ? options->Find("add_vision_id") : nullptr;
    if (!vision_id) vision_id = req.Find("add_vision_id");
    int image_count = 0, video_count = 0;
    std::vector<std::string> contents;
    for (const auto& message : messages->array) {
      std::string error;
      contents.push_back(RenderContent(message, &items, &error,
                                       vision_id && vision_id->boolean,
                                       &image_count, &video_count));
      if (!error.empty()) return fail(error);
    }
    const auto rendered = RenderChatPrompt(req, contents, &prompt);
    if (!rendered) return fail(rendered.message());
  } else {
    prompt = req.GetString("prompt");
  }
  return prompt.empty() ? fail("empty prompt") : Status();
}
}  // namespace q4t::server
