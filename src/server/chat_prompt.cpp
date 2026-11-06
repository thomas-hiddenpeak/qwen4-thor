#include "q4t/server/chat_prompt.h"

#include <algorithm>
#include <cstdint>
#include <format>
#include <utility>

#include "q4t/server/chat_template.h"
#include "q4t/server/media_admission.h"

namespace q4t::server {
namespace {
// Render and collect content once, preserving model vision boundaries and
// item order. HTTP video_frames is the transport adapter for a video item.
std::string RenderContent(const io::Json& m, std::vector<VisionItem>* items,
                          std::string* err, bool add_vision_id,
                          int* image_count, int* video_count,
                          size_t message_index, std::string* error_param,
                          std::string* error_code) {
  std::string out;
  const io::Json* content = m.Find("content");
  if (content == nullptr || content->IsNull()) return out;
  if (content->IsString()) return content->str;
  if (!content->IsArray()) {
    *err = "Unexpected content type.";
    return out;
  }
  for (size_t part_index = 0; part_index < content->array.size();
       ++part_index) {
    const auto& part = content->array[part_index];
    *error_param = "messages[" + std::to_string(message_index) + "].content[" +
                   std::to_string(part_index) + "]";
    const io::Json* iu = part.Find("image_url");
    if (iu != nullptr) {
      if (m.GetString("role") != "user") {
        *err = "Only user messages may contain images.";
        return {};
      }
      std::string url;
      if (iu->IsObject()) {
        url = iu->GetString("url", "");
      } else if (iu->IsString()) {
        url = iu->str;
      }
      VisionItem item;
      item.kind = VisionItem::kImage;
      item.message_index = message_index;
      item.part_index = part_index;
      *error_param = MediaInputParam(item);
      if (items->size() >= vision::kMediaPlanItems) {
        *error_code = "media_item_limit";
        *err = *error_code;
        return {};
      }
      std::string bytes;
      const char* code = "ok";
      const auto decoded = DecodeMediaUrl(url, &bytes, &code);
      if (!decoded) {
        *error_code = code;
        *err = decoded.message();
        return {};
      }
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
      if (m.GetString("role") != "user") {
        *err = "Only user messages may contain videos.";
        return {};
      }
      if (!vf->IsArray()) {
        if (err) *err = "video_frames must be an array of base64 data urls";
        return std::string();
      }
      VisionItem item;
      item.kind = VisionItem::kVideo;
      item.message_index = message_index;
      item.part_index = part_index;
      *error_param = MediaInputParam(item);
      if (items->size() >= vision::kMediaPlanItems || vf->array.size() < 2 ||
          vf->array.size() > vision::kMediaPlanFrames) {
        *error_code = items->size() >= vision::kMediaPlanItems
                          ? "media_item_limit"
                          : "media_frame_limit";
        *err = *error_code;
        return {};
      }
      std::vector<double> times;
      std::string time_field;
      const auto timing =
          VideoFrameTimes(part, vf->array.size(), &times, &time_field);
      if (!timing) {
        *error_param = "messages[" + std::to_string(message_index) +
                       "].content[" + std::to_string(part_index) + "]." +
                       time_field;
        *error_code = "media_timestamp_invalid";
        *err = timing.message();
        return {};
      }
      for (size_t frame = 0; frame < vf->array.size(); ++frame) {
        const auto& f = vf->array[frame];
        *error_param = MediaInputParam(item, frame);
        std::string url;
        if (f.IsObject()) {
          url = f.GetString("url", "");
        } else if (f.IsString()) {
          url = f.str;
        }
        std::string bytes;
        const char* code = "ok";
        const auto decoded = DecodeMediaUrl(url, &bytes, &code);
        if (!decoded) {
          *error_code = code;
          *err = decoded.message();
          return {};
        }
        item.frames.push_back(std::move(bytes));
      }
      if (item.frames.empty()) {
        if (err) *err = "video_frames is empty";
        return std::string();
      }
      items->push_back(std::move(item));
      ++*video_count;
      if (add_vision_id) out += "Video " + std::to_string(*video_count) + ": ";
      // Match the processor: an odd tail repeats the last frame; each pair
      // receives its mean timestamp and an independent visual boundary.
      for (size_t frame = 0; frame < times.size(); frame += 2) {
        const double last = times[std::min(frame + 1, times.size() - 1)];
        out += std::format("<{:.1f} seconds>", (times[frame] + last) / 2);
        out += "<|vision_start|><|video_pad|><|vision_end|>";
      }
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
                         std::string* error_param, std::string* error_code) {
  prompt_out->clear();
  items_out->clear();
  error_param->clear();
  std::string local_code;
  if (!error_code) error_code = &local_code;
  *error_code = "invalid_request";
  const auto adapted = constraints.active
                           ? ConstraintTemplateRequest(request, constraints)
                           : io::Json{};
  const auto& req = constraints.active ? adapted : request;
  const auto fail = [&](std::string message) {
    if (error_param->empty())
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
    for (size_t index = 0; index < messages->array.size(); ++index) {
      const auto& message = messages->array[index];
      std::string error;
      contents.push_back(RenderContent(
          message, &items, &error, vision_id && vision_id->boolean,
          &image_count, &video_count, index, error_param, error_code));
      if (!error.empty()) return fail(error);
      error_param->clear();
    }
    const auto rendered = RenderChatPrompt(req, contents, &prompt);
    if (!rendered) return fail(rendered.message());
  } else {
    prompt = req.GetString("prompt");
  }
  return prompt.empty() ? fail("empty prompt") : Status();
}
}  // namespace q4t::server
