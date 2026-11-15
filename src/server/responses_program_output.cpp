#include "q4t/server/responses_output.h"

#include <limits>

#include "q4t/server/responses_item_shape.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_program.h"
#include "q4t/server/request_json.h"
#include "q4t/server/input_contract.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;
namespace {
constexpr std::string_view kContentPrefix = "q4t_program_content_v1\n";
constexpr size_t kContentBytes = 4 * 1024 * 1024;
Status CheckContent(const io::Json& content) {
  if (!content.IsArray() || content.array.size() > 256)
    return Status::Fail("Invalid program output content");
  for (const auto& part : content.array) {
    const auto type = part.GetString("type");
    std::string param;
    if (type == "input_text") {
      if (!InputFields(part, {"type", "text"}, &param, "result") ||
          !part.Find("text") || !part.Find("text")->IsString())
        return Status::Fail("Invalid program output text");
    } else if (type == "input_image") {
      if (!InputFields(part, {"type", "image_url", "detail"}, &param,
                       "result") ||
          !part.Find("image_url") || !part.Find("image_url")->IsString() ||
          part.GetString("image_url").empty())
        return Status::Fail("Invalid program output image");
      const auto detail = part.GetString("detail", "auto");
      if ((part.Has("detail") && !part.Find("detail")->IsString()) ||
          (detail != "auto" && detail != "low" && detail != "high" &&
           detail != "original"))
        return Status::Fail("Invalid program image detail");
    } else {
      return Status::Fail("Invalid program output content type");
    }
  }
  return {};
}
}  // namespace

Status DecodeResponsesProgramOutput(std::string_view result,
                                    io::Json* content) {
  if (!content || result.size() > kContentBytes)
    return Status::Fail("Program output content exceeds capacity");
  *content = {};
  if (!result.starts_with(kContentPrefix)) {
    *content = String(std::string(result));
    return {};
  }
  auto limits = kSchemaJsonLimits;
  limits.max_string_bytes = kContentBytes;
  auto status = io::ParseJson(std::string(result.substr(kContentPrefix.size())),
                              content, true, limits);
  return status ? CheckContent(*content) : status;
}

Status EncodeResponsesProgramOutput(const io::Json& content,
                                    std::string* result) {
  if (!result) return Status::Fail("Missing program output owner");
  auto parts = content;
  if (parts.IsString())
    parts = Array({Object({{"type", String("input_text")}, {"text", parts}})});
  auto status = CheckContent(parts);
  if (!status) return status;
  std::string text;
  bool media = false;
  for (size_t i = 0; i < parts.array.size(); ++i) {
    const auto& part = parts.array[i];
    if (part.GetString("type") == "input_image")
      media = true;
    else {
      if (i) text += '\n';
      text += part.GetString("text");
    }
  }
  if (media || text.starts_with(kContentPrefix))
    text = std::string(kContentPrefix) + OutputJson(parts);
  if (text.size() > kContentBytes)
    return Status::Fail("Program output content exceeds capacity");
  *result = std::move(text);
  return {};
}

Status ResponsesOutput::AppendProgramItems(const io::Json& items,
                                           std::string* wire) {
  wire->clear();
  if (!started_ || terminal_ || !items.IsArray() ||
      response_.GetString("status") != "in_progress" ||
      items.array.size() > kMaxItems - Items().array.size())
    return Status::Fail("Invalid program output publication");
  // The owner uses a candidate and checkpoints the complete program dispatch
  // before publishing it. Validation never leaks a prefix of rejected work.
  for (const auto& item : items.array) {
    const auto type = item.GetString("type");
    if (type != "program" && type != "program_output" &&
        type != "function_call" && type != "custom_tool_call")
      return Status::Fail("Invalid program output item");
    if ((type == "function_call" || type == "custom_tool_call") &&
        (!item.Find("caller") ||
         item.Find("caller")->GetString("type") != "program" ||
         item.GetString("status") != "completed"))
      return Status::Fail("Program child is not ready for publication");
    ChatPlanError error;
    auto status = ValidateResponsesItemShape(ResponsesPublicValue(item),
                                             ResponsesItemShape::kOutput,
                                             &error, "program");
    if (status) status = Charge(OutputJson(item).size() + 8 * sizeof(io::Json));
    if (!status) return status;
  }
  for (const auto& item : items.array) {
    const auto index = Items().array.size();
    Items().array.push_back(item);
    call_indices_.push_back(std::numeric_limits<size_t>::max());
    Event("response.output_item.added",
          Object({{"output_index", Number(index)}, {"item", item}}), wire);
    if (item.GetString("type") == "program") {
      // Program has no status field in the frozen SDK shape.
      Event("response.output_item.done",
            Object({{"output_index", Number(index)}, {"item", item}}), wire);
      closed_items_.insert(index);
    } else {
      CloseItem(index, item.GetString("status") == "incomplete", wire);
    }
  }
  return {};
}
}  // namespace q4t::server
