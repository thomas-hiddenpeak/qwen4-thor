#include "q4t/server/chat_response_parser.h"

#include <algorithm>
#include <charconv>
#include <unordered_set>

#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
constexpr std::string_view kThinkOpen = "<think>";
constexpr std::string_view kThinkClose = "</think>";
constexpr std::string_view kToolOpen = "<tool_call>";
constexpr std::string_view kToolClose = "</tool_call>";

std::string Quote(std::string_view text) {
  return "\"" + detail::JsonEscape(std::string(text)) + "\"";
}

std::string_view Trim(std::string_view text) {
  constexpr std::string_view space = " \t\r\n";
  const auto begin = text.find_first_not_of(space);
  if (begin == text.npos) return {};
  return text.substr(begin, text.find_last_not_of(space) - begin + 1);
}

std::string JsonValue(const io::Json& value) {
  using Type = io::Json::Type;
  switch (value.type) {
    case Type::kNull:
      return "null";
    case Type::kBool:
      return value.boolean ? "true" : "false";
    case Type::kNumber:
      return value.str;  // ParseJson retained exact spelling.
    case Type::kString:
      return Quote(value.str);
    case Type::kArray: {
      std::string out = "[";
      for (const auto& item : value.array) {
        if (out.size() != 1) out += ',';
        out += JsonValue(item);
      }
      return out + ']';
    }
    case Type::kObject: {
      std::string out = "{";
      for (const auto& [key, item] : value.object) {
        if (out.size() != 1) out += ',';
        out += Quote(key) + ':' + JsonValue(item);
      }
      return out + '}';
    }
  }
  return {};
}

bool ExactInteger(std::string_view number) {
  // Do not use the binary64 value: 1.0000000000000001 rounds to 1.0.
  const auto e = number.find_first_of("eE");
  auto mantissa = number.substr(0, e);
  int exponent = 0;
  if (e != number.npos) {
    auto suffix = number.substr(e + 1);
    if (suffix.starts_with('+')) suffix.remove_prefix(1);
    const auto result =
        std::from_chars(suffix.data(), suffix.data() + suffix.size(), exponent);
    if (result.ec != std::errc() || result.ptr != suffix.data() + suffix.size())
      return false;
  }
  const auto dot = mantissa.find('.');
  const auto fraction = dot == mantissa.npos ? 0 : mantissa.size() - dot - 1;
  int64_t needed = static_cast<int64_t>(fraction) - exponent;
  if (needed <= 0) return true;
  for (auto i = mantissa.size(); i > 0; --i) {
    const char c = mantissa[i - 1];
    if (c == '.' || c == '-') continue;
    if (c != '0') return false;
    if (--needed == 0) return true;
  }
  return true;  // The entire mantissa was zero.
}

bool Matches(const io::Json& value, const std::string& type) {
  if (type == "string") return value.IsString();
  if (type == "number") return value.IsNumber();
  if (type == "integer") return value.IsNumber() && ExactInteger(value.str);
  if (type == "boolean") return value.IsBool();
  if (type == "null") return value.IsNull();
  if (type == "object") return value.IsObject();
  if (type == "array") return value.IsArray();
  return false;
}

// Type conversion, not full JSON Schema validation/constrained decoding.
Status Argument(std::string_view raw, const io::Json* schema,
                std::string* output) {
  std::vector<std::string> types;
  const auto* type = schema ? schema->Find("type") : nullptr;
  if (type && type->IsString()) types.push_back(type->str);
  if (type && type->IsArray()) {
    for (const auto& item : type->array)
      if (item.IsString()) types.push_back(item.str);
  }
  // Strings in Qwen XML are not JSON quoted. Trim only the newline framing
  // emitted by the chat template; preserve user-significant spaces/quotes.
  std::string_view string_value = raw;
  if (string_value.starts_with("\r\n"))
    string_value.remove_prefix(2);
  else if (string_value.starts_with('\n'))
    string_value.remove_prefix(1);
  if (string_value.ends_with("\r\n"))
    string_value.remove_suffix(2);
  else if (string_value.ends_with('\n'))
    string_value.remove_suffix(1);
  if (types.size() == 1 && types[0] == "string") {
    *output = Quote(string_value);
    return Status();
  }
  io::Json value;
  const io::JsonParseLimits limits{32, 65536, 1024 * 1024, true};
  const auto status =
      io::ParseJson(std::string(Trim(raw)), &value, true, limits);
  if (status && (types.empty() ||
                 std::any_of(types.begin(), types.end(), [&](const auto& name) {
                   return Matches(value, name);
                 }))) {
    *output = JsonValue(value);
    return Status();
  }
  if (!status && types.empty() &&
      (Trim(raw).starts_with('{') || Trim(raw).starts_with('[')))
    return Status::Fail("invalid JSON tool argument");
  if (types.empty() ||
      std::find(types.begin(), types.end(), "string") != types.end()) {
    *output = Quote(string_value);
    return Status();
  }
  return Status::Fail("tool argument does not match its declared type");
}

std::string ToolJson(const ResponseToolCall& call, bool indexed) {
  return "{" +
         (indexed ? "\"index\":" + std::to_string(call.index) + ','
                  : std::string()) +
         "\"id\":" + Quote(call.id) + ",\"type\":\"function\",\"function\":{" +
         "\"name\":" + Quote(call.name) +
         ",\"arguments\":" + Quote(call.arguments) + "}}";
}

size_t Lookahead(std::string_view text, std::string_view marker) {
  for (size_t n = std::min(text.size(), marker.size() - 1); n > 0; --n)
    if (text.ends_with(marker.substr(0, n))) return n;
  return 0;
}
}  // namespace

std::string ResponseDelta::ToJson() const {
  if (kind == Kind::kTool)
    return "{\"tool_calls\":[" + ToolJson(call, true) + "]}";
  return std::string(kind == Kind::kReasoning ? "{\"reasoning_content\":"
                                              : "{\"content\":") +
         Quote(text) + '}';
}

Status ChatResponseParser::Init(const io::Json& request,
                                std::string_view prompt,
                                const std::string& request_id, bool collect) {
  if (initialized_) return Fail("parser already initialized");
  initialized_ = true;
  request_ = &request;
  auto plan_status = PlanOutputConstraints(request, &constraints_);
  if (!plan_status) return Fail(plan_status.message());
  collect_ = collect || constraints_.active;
  parsed_ = request.Has("messages") && request.GetBool("parse_response", true);
  id_prefix_ = "call_" + request_id + '_';
  if (!parsed_) return Status();
  if (prompt.ends_with("<think>\n"))
    region_ = Region::kReasoning;
  else if (!prompt.ends_with("<think>\n\n</think>\n\n"))
    return Fail("unsupported assistant generation prefix");
  const auto* definitions = request.GetArray("tools");
  if (!definitions) return Status();
  for (const auto& tool : definitions->array) {
    const auto* fn = tool.Find("function");
    if (tool.GetString("type") != "function" || !fn || !fn->IsObject())
      return Fail("parsed responses require function tool definitions");
    const auto name = fn->GetString("name");
    if (name.empty() || name.size() > 256 ||
        name.find_first_of("<>\r\n") != name.npos ||
        !tools_.emplace(name, fn).second)
      return Fail("invalid or duplicate tool name");
    if (const auto* strict = fn->Find("strict"); strict && !strict->IsBool())
      return Fail("strict must be boolean");
    const auto* parameters = fn->Find("parameters");
    if (parameters && !parameters->IsObject())
      return Fail("tool parameters must be a schema object");
    const auto* properties =
        parameters ? parameters->Find("properties") : nullptr;
    if (properties && !properties->IsObject())
      return Fail("tool properties must be an object");
  }
  return Status();
}

Status ChatResponseParser::Fail(const std::string& message) {
  status_ = Status::Fail(message);
  return status_;
}

void ChatResponseParser::Emit(Region region, std::string_view text,
                              std::vector<ResponseDelta>* deltas) {
  if (text.empty()) return;
  const bool reasoning = region == Region::kReasoning;
  if (collect_) (reasoning ? reasoning_ : content_).append(text);
  const auto kind = reasoning ? ResponseDelta::Kind::kReasoning
                              : ResponseDelta::Kind::kContent;
  auto* target = constraints_.active ? &deferred_ : deltas;
  if (constraints_.active && !target->empty() && target->back().kind == kind)
    target->back().text.append(text);
  else
    target->push_back({kind, std::string(text), {}});
}

Status ChatResponseParser::ParseTool(std::string_view block,
                                     ResponseToolCall* call) const {
  block = Trim(block);
  if (!block.starts_with("<function="))
    return Status::Fail("tool call must start with a function");
  const auto end = block.find('>');
  if (end == block.npos) return Status::Fail("incomplete function name");
  call->name = std::string(block.substr(10, end - 10));
  if (constraints_.choice == OutputConstraints::ToolChoice::kNone)
    return Status::Fail("tool call forbidden by tool_choice=none");
  if (constraints_.choice == OutputConstraints::ToolChoice::kNamed &&
      call->name != constraints_.named_tool)
    return Status::Fail("generated tool does not match named choice");
  if ((!constraints_.parallel ||
       constraints_.choice == OutputConstraints::ToolChoice::kNamed) &&
      tool_count_ != 0)
    return Status::Fail("multiple tool calls forbidden by request");
  const auto fn = tools_.find(call->name);
  if (fn == tools_.end()) return Status::Fail("unknown generated tool name");
  const auto* parameters = fn->second->Find("parameters");
  const auto* properties =
      parameters ? parameters->Find("properties") : nullptr;
  block.remove_prefix(end + 1);
  std::unordered_set<std::string> seen;
  call->arguments = "{";
  for (;;) {
    block = Trim(block);
    if (block == "</function>") break;
    if (!block.starts_with("<parameter="))
      return Status::Fail("invalid parameter or function closing tag");
    const auto name_end = block.find('>');
    if (name_end == block.npos)
      return Status::Fail("incomplete parameter name");
    const auto name = std::string(block.substr(11, name_end - 11));
    if (name.empty() || name.size() > 256 ||
        name.find_first_of("<>\r\n") != name.npos || !seen.insert(name).second)
      return Status::Fail("invalid or duplicate tool parameter");
    block.remove_prefix(name_end + 1);
    const auto close = block.find("</parameter>");
    if (close == block.npos) return Status::Fail("incomplete tool parameter");
    std::string value;
    auto status =
        Argument(block.substr(0, close),
                 properties ? properties->Find(name) : nullptr, &value);
    if (!status) return status;
    if (seen.size() > 1) call->arguments += ',';
    call->arguments += Quote(name) + ':' + value;
    block.remove_prefix(close + std::string_view("</parameter>").size());
  }
  call->arguments += '}';
  if (constraints_.active) {
    auto status = ValidateConstrainedArguments(*fn->second, call->arguments);
    if (!status) return status;
  }
  call->index = tool_count_;
  call->id = id_prefix_ + std::to_string(tool_count_);
  return Status();
}

Status ChatResponseParser::Append(std::string_view text,
                                  std::vector<ResponseDelta>* deltas) {
  if (!status_) return status_;
  if (!initialized_ || finished_) return Fail("parser is not accepting input");
  if (constraints_.active) {
    if (text.size() > kMaxToolBytes - constrained_bytes_)
      return Fail("constrained response exceeds 1 MiB");
    constrained_bytes_ += text.size();
  }
  if (!parsed_) {
    Emit(Region::kContent, text, deltas);
    return Status();
  }
  pending_.append(text);
  while (!pending_.empty()) {
    if (constraints_.json_output && region_ == Region::kContent) {
      // Marker-shaped substrings inside JSON strings are data, not tool or
      // reasoning delimiters. The initial thinking prefix is handled above
      // this region by the same native parser as ordinary requests.
      Emit(region_, pending_, deltas);
      pending_.clear();
      return Status();
    }
    if (region_ == Region::kTool) {
      const auto end = pending_.find(kToolClose, tool_scan_);
      const auto body_size =
          end == pending_.npos
              ? pending_.size() - Lookahead(pending_, kToolClose)
              : end;
      if (body_size > kMaxToolBytes) return Fail("tool block exceeds 1 MiB");
      if (end == pending_.npos) {
        tool_scan_ = pending_.size() > kToolClose.size()
                         ? pending_.size() - kToolClose.size()
                         : 0;
        return Status();
      }
      if (tool_count_ == kMaxCalls)
        return Fail("too many generated tool calls");
      ResponseToolCall call;
      auto status = ParseTool(std::string_view(pending_).substr(0, end), &call);
      if (!status) return Fail(status.message());
      ++tool_count_;
      if (collect_) calls_.push_back(call);
      auto* target = constraints_.active ? &deferred_ : deltas;
      target->push_back({ResponseDelta::Kind::kTool, {}, std::move(call)});
      pending_.erase(0, end + kToolClose.size());
      region_ = Region::kContent;
      continue;
    }
    auto marker = region_ == Region::kReasoning ? kThinkClose : kThinkOpen;
    auto found = pending_.find(marker);
    size_t keep = Lookahead(pending_, marker);
    if (region_ == Region::kContent &&
        (!tools_.empty() ||
         constraints_.choice == OutputConstraints::ToolChoice::kNone)) {
      const auto tool = pending_.find(kToolOpen);
      if (tool != pending_.npos && (found == pending_.npos || tool < found)) {
        found = tool;
        marker = kToolOpen;
      }
      keep = std::max(keep, Lookahead(pending_, kToolOpen));
    }
    if (found == pending_.npos) {
      Emit(region_,
           std::string_view(pending_).substr(0, pending_.size() - keep),
           deltas);
      pending_.erase(0, pending_.size() - keep);
      return Status();
    }
    Emit(region_, std::string_view(pending_).substr(0, found), deltas);
    pending_.erase(0, found + marker.size());
    region_ = marker == kToolOpen    ? Region::kTool
              : marker == kThinkOpen ? Region::kReasoning
                                     : Region::kContent;
    tool_scan_ = 0;
  }
  return Status();
}

Status ChatResponseParser::Finish(bool length_limited,
                                  std::vector<ResponseDelta>* deltas) {
  if (!status_) return status_;
  if (!initialized_ || finished_) return Fail("parser already finished");
  finished_ = true;
  if (constraints_.active && length_limited)
    return Fail("constrained response exhausted output/context budget");
  if (region_ == Region::kTool ||
      (!tools_.empty() && pending_.starts_with("<tool_")))
    return Fail("incomplete tool call");
  if (region_ == Region::kReasoning && !length_limited)
    return Fail("reasoning block did not close before stop");
  Emit(region_, pending_, deltas);
  pending_.clear();
  if (constraints_.active) {
    if ((constraints_.choice == OutputConstraints::ToolChoice::kRequired ||
         constraints_.choice == OutputConstraints::ToolChoice::kNamed) &&
        tool_count_ == 0)
      return Fail("required tool call missing");
    if (constraints_.json_output) {
      const auto status = ValidateConstrainedContent(*request_, content_);
      if (!status) return Fail(status.message());
    }
    // No usable content/reasoning/tool frames escape before the entire
    // constrained response passes. A later invalid call invalidates the batch.
    for (auto& delta : deferred_) deltas->push_back(std::move(delta));
    deferred_.clear();
  }
  return Status();
}

std::string ChatResponseParser::MessageJson() const {
  std::string message = "{\"role\":\"assistant\",\"content\":";
  message += content_.empty() && !calls_.empty() ? "null" : Quote(content_);
  if (!reasoning_.empty())
    message += ",\"reasoning_content\":" + Quote(reasoning_);
  if (!calls_.empty()) {
    message += ",\"tool_calls\":[";
    for (size_t i = 0; i < calls_.size(); ++i) {
      if (i) message += ',';
      message += ToolJson(calls_[i], false);
    }
    message += ']';
  }
  return message + '}';
}

}  // namespace q4t::server
