#include "q4t/server/chat_response_parser.h"

#include <algorithm>
#include <charconv>
#include <unordered_set>

#include "q4t/server/http_response.h"
#include "q4t/server/legacy_tools.h"

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
         "\"id\":" + Quote(call.id) + ",\"type\":" +
         Quote(call.custom ? "custom" : "function") + ',' +
         Quote(call.custom ? "custom" : "function") + ":{" +
         "\"name\":" + Quote(call.name) +
         ',' + Quote(call.custom ? "input" : "arguments") + ':' +
         Quote(call.arguments) + "}}";
}

size_t Lookahead(std::string_view text, std::string_view marker) {
  for (size_t n = std::min(text.size(), marker.size() - 1); n > 0; --n)
    if (text.ends_with(marker.substr(0, n))) return n;
  return 0;
}
}  // namespace

std::string ResponseDelta::ToJson() const {
  if (kind == Kind::kTool) {
    if (legacy_function)
      return "{\"function_call\":{" +
             (tool_start ? "\"name\":" + Quote(call.name) + ',' : "") +
             "\"arguments\":" + Quote(call.arguments) + "}}";
    if (tool_start) return "{\"tool_calls\":[" + ToolJson(call, true) + "]}";
    return "{\"tool_calls\":[{\"index\":" + std::to_string(call.index) +
           ',' + Quote(call.custom ? "custom" : "function") + ":{" +
           Quote(call.custom ? "input" : "arguments") + ':' +
           Quote(call.arguments) + "}}]}";
  }
  const auto field = kind == Kind::kReasoning
                         ? ReasoningFieldName(reasoning_field)
                         : "content";
  return '{' + Quote(field) + ':' + Quote(text) + '}';
}

Status ChatResponseParser::Init(const io::Json& request,
                                std::string_view prompt,
                                const std::string& request_id, bool collect,
                                bool legacy_function) {
  if (initialized_) return Fail("parser already initialized");
  if (HasLegacyTools(request)) {
    LegacyToolRequest normalized;
    const auto status = NormalizeLegacyTools(request, &normalized);
    if (!status) return Fail(status.message());
    normalized_request_ = std::move(normalized.request);
    return Init(*normalized_request_, prompt, request_id, collect,
                normalized.legacy_output);
  }
  initialized_ = true;
  legacy_function_ = legacy_function;
  request_ = &request;
  auto reason_status = ParseReasoningOptions(request, &reasoning_options_);
  if (!reason_status) return Fail(reason_status.message());
  auto plan_status = PlanOutputConstraints(request, &constraints_);
  if (!plan_status) return Fail(plan_status.message());
  if (legacy_function_ && constraints_.parallel)
    return Fail("legacy response cannot contain parallel calls");
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
    if (tool.GetString("type") == "custom") {
      const auto* custom = tool.Find("custom");
      if (!custom || legacy_function_)
        return Fail("custom definition cannot use legacy response dialect");
      CustomToolFormat format;
      const auto status = ParseCustomToolFormat(*custom, &format);
      if (!status) return Fail(status.message());
      const auto name = custom->GetString("name");
      if (!tools_.emplace(name, custom).second) return Fail("duplicate tool name");
      custom_tools_.insert(name);
      continue;
    }
    const auto* fn = tool.Find("function");
    if (tool.GetString("type") != "function" || !fn || !fn->IsObject())
      return Fail("parsed responses require function tool definitions");
    const auto name = fn->GetString("name");
    if (name.empty() || name.size() > 256 ||
        name.find_first_of("<>\r\n") != name.npos ||
        !tools_.emplace(name, fn).second)
      return Fail("invalid or duplicate tool name");
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
  if (reasoning && !reasoning_options_.include) return;
  if (collect_) (reasoning ? reasoning_ : content_).append(text);
  const auto kind = reasoning ? ResponseDelta::Kind::kReasoning
                              : ResponseDelta::Kind::kContent;
  auto* target = constraints_.active ? &deferred_ : deltas;
  if (constraints_.active && !target->empty() && target->back().kind == kind &&
      target->back().source_end == pending_offset_) {
    target->back().text.append(text);
    target->back().source_end += text.size();
  } else {
    target->push_back({kind,
                       std::string(text),
                       {},
                       reasoning_options_.field,
                       pending_offset_,
                       pending_offset_ + text.size()});
  }
}

Status ChatResponseParser::ParseTool(std::string_view block,
                                     ResponseToolCall* call) const {
  block = Trim(block);
  call->custom = block.starts_with("<custom=");
  if (!call->custom && !block.starts_with("<function="))
    return Status::Fail("tool call must start with function or custom");
  const auto end = block.find('>');
  if (end == block.npos) return Status::Fail("incomplete function name");
  const size_t prefix = call->custom ? 8 : 10;
  call->name = std::string(block.substr(prefix, end - prefix));
  if (!constraints_.AllowsTool(call->name))
    return Status::Fail("generated tool forbidden by tool_choice");
  if ((!constraints_.parallel ||
       constraints_.choice == OutputConstraints::ToolChoice::kNamed) &&
      tool_count_ != 0)
    return Status::Fail("multiple tool calls forbidden by request");
  const auto fn = tools_.find(call->name);
  if (fn == tools_.end()) return Status::Fail("unknown generated tool name");
  if (call->custom != custom_tools_.contains(call->name))
    return Status::Fail("generated tool type differs from its definition");
  if (call->custom) {
    block.remove_prefix(end + 1);
    block = Trim(block);
    constexpr std::string_view closing = "</custom>";
    if (!block.ends_with(closing))
      return Status::Fail("incomplete custom closing tag");
    block.remove_suffix(closing.size());
    io::Json value;
    auto status = io::ParseJson(std::string(Trim(block)), &value, true,
                                {8, 16, kMaxToolBytes, true});
    if (!status || !value.IsString())
      return Status::Fail("custom payload must be one JSON string");
    call->arguments = std::move(value.str);
    call->index = tool_count_;
    call->id = id_prefix_ + std::to_string(tool_count_);
    return Status();
  }
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

void ChatResponseParser::ToolFragment(std::string text, bool start,
                                      std::vector<ResponseDelta>* deltas) {
  partial_call_.arguments += text;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call.index = partial_call_.index;
  delta.call.custom = partial_call_.custom;
  if (start) {
    delta.call.id = partial_call_.id;
    delta.call.name = partial_call_.name;
  }
  delta.call.arguments = std::move(text);
  delta.tool_start = start;
  delta.legacy_function = legacy_function_;
  deltas->push_back(std::move(delta));
}

Status ChatResponseParser::StreamTool(size_t available, bool complete,
                                      std::vector<ResponseDelta>* deltas) {
  // The original block remains bounded and is independently validated at its
  // closing tag. Incremental fragments are provisional until the successful
  // response terminal. No parser, SDK or tool runner dispatches them here.
  const std::string_view block(pending_.data(), available);
  const auto whitespace = [&](size_t* offset) {
    while (*offset < block.size() &&
           std::string_view(" \t\r\n").find(block[*offset]) != block.npos)
      ++*offset;
  };
  if (!tool_header_) {
    whitespace(&tool_parse_offset_);
    const auto rest = block.substr(tool_parse_offset_);
    constexpr std::string_view function_prefix = "<function=";
    constexpr std::string_view custom_prefix = "<custom=";
    if ((function_prefix.starts_with(rest) || custom_prefix.starts_with(rest)) &&
        !complete)
      return Status();
    partial_call_.custom = rest.starts_with(custom_prefix);
    const auto prefix = partial_call_.custom ? custom_prefix : function_prefix;
    if (!rest.starts_with(prefix)) return Status::Fail("invalid tool tag");
    const auto end = rest.find('>');
    if (end == rest.npos) {
      if (rest.size() > 266 || complete)
        return Status::Fail("incomplete function name");
      return Status();
    }
    partial_call_.name =
        std::string(rest.substr(prefix.size(), end - prefix.size()));
    if (!tools_.contains(partial_call_.name) ||
        !constraints_.AllowsTool(partial_call_.name))
      return Status::Fail("unknown or forbidden generated tool");
    if (partial_call_.custom != custom_tools_.contains(partial_call_.name))
      return Status::Fail("generated tool type differs from definition");
    if (tool_count_ == kMaxCalls ||
        ((!constraints_.parallel ||
          constraints_.choice == OutputConstraints::ToolChoice::kNamed) &&
         tool_count_))
      return Status::Fail("too many generated tool calls");
    partial_call_.index = tool_count_;
    partial_call_.id = id_prefix_ + std::to_string(tool_count_);
    tool_parse_offset_ += end + 1;
    tool_header_ = true;
    ToolFragment(partial_call_.custom ? "" : "{", true, deltas);
  }
  if (partial_call_.custom) {
    if (!custom_input_.Started()) whitespace(&tool_parse_offset_);
    if (!custom_input_.Done()) {
      size_t consumed = 0;
      std::string decoded;
      auto status = custom_input_.Append(block.substr(tool_parse_offset_),
                                         &consumed, &decoded);
      if (!status) return status;
      tool_parse_offset_ += consumed;
      if (!decoded.empty()) ToolFragment(std::move(decoded), false, deltas);
      if (!custom_input_.Done())
        return complete ? Status::Fail("incomplete custom input") : Status();
    }
    whitespace(&tool_parse_offset_);
    if (!tool_function_closed_) {
      const auto rest = block.substr(tool_parse_offset_);
      constexpr std::string_view closing = "</custom>";
      if (!complete && closing.starts_with(rest)) return Status();
      if (!rest.starts_with(closing))
        return Status::Fail("invalid custom closing tag");
      tool_parse_offset_ += closing.size();
      tool_function_closed_ = true;
    }
    whitespace(&tool_parse_offset_);
    return tool_parse_offset_ == block.size()
               ? Status()
               : Status::Fail("data after custom closing tag");
  }
  while (!tool_function_closed_) {
    if (!tool_parameter_) {
      whitespace(&tool_parse_offset_);
      const auto rest = block.substr(tool_parse_offset_);
      constexpr std::string_view closing = "</function>";
      constexpr std::string_view prefix = "<parameter=";
      if (rest.starts_with(closing)) {
        tool_parse_offset_ += closing.size();
        tool_function_closed_ = true;
        break;
      }
      if (!complete && (closing.starts_with(rest) || prefix.starts_with(rest)))
        return Status();
      if (!rest.starts_with(prefix))
        return Status::Fail("invalid parameter or function closing tag");
      const auto end = rest.find('>');
      if (end == rest.npos) {
        if (rest.size() > 267 || complete)
          return Status::Fail("incomplete parameter name");
        return Status();
      }
      tool_parameter_name_ =
          std::string(rest.substr(prefix.size(), end - prefix.size()));
      const auto& name = tool_parameter_name_;
      if (name.empty() || name.size() > 256 ||
          name.find_first_of("<>\r\n") != name.npos ||
          !tool_parameters_.insert(name).second)
        return Status::Fail("invalid or duplicate tool parameter");
      const auto* parameters =
          tools_.at(partial_call_.name)->Find("parameters");
      const auto* properties =
          parameters ? parameters->Find("properties") : nullptr;
      const auto* schema = properties ? properties->Find(name) : nullptr;
      const auto* type = schema ? schema->Find("type") : nullptr;
      tool_string_ = type && ((type->IsString() && type->str == "string") ||
                              (type->IsArray() && type->array.size() == 1 &&
                               type->array[0].IsString() &&
                               type->array[0].str == "string"));
      tool_value_begin_ = tool_parse_offset_ + end + 1;
      tool_value_scan_ = tool_value_begin_;
      tool_value_sent_ = tool_value_begin_;
      tool_string_started_ = false;
      tool_parameter_ = true;
      ToolFragment((tool_parameters_.size() == 1 ? "" : ",") + Quote(name) +
                       ':' + (tool_string_ ? "\"" : ""),
                   false, deltas);
    }
    constexpr std::string_view closing = "</parameter>";
    const auto close = block.find(closing, tool_value_scan_);
    const bool ended = close != block.npos;
    const size_t safe =
        ended ? close
              : block.size() -
                    Lookahead(block.substr(tool_value_begin_), closing);
    tool_value_scan_ =
        block.size() > closing.size()
            ? std::max(tool_value_begin_, block.size() - closing.size())
            : tool_value_begin_;
    if (tool_string_) {
      if (!tool_string_started_) {
        const auto raw =
            block.substr(tool_value_begin_, safe - tool_value_begin_);
        if (!ended && (raw.empty() || raw == "\r")) return Status();
        if (raw.starts_with("\r\n"))
          tool_value_sent_ += 2;
        else if (raw.starts_with('\n'))
          ++tool_value_sent_;
        tool_string_started_ = true;
      }
      size_t end = safe;
      if (ended) {
        const auto raw =
            block.substr(tool_value_sent_, safe - tool_value_sent_);
        if (raw.ends_with("\r\n"))
          end -= 2;
        else if (raw.ends_with('\n'))
          --end;
      } else {
        // Hold a possible framing CRLF and never split a UTF-8 codepoint.
        end = safe - tool_value_sent_ > 2 ? safe - 2 : tool_value_sent_;
        while (end > tool_value_sent_ &&
               (static_cast<unsigned char>(block[end]) & 0xc0) == 0x80)
          --end;
      }
      if (end > tool_value_sent_) {
        auto escaped =
            Quote(block.substr(tool_value_sent_, end - tool_value_sent_));
        ToolFragment(escaped.substr(1, escaped.size() - 2), false, deltas);
        tool_value_sent_ = end;
      }
      if (ended) ToolFragment("\"", false, deltas);
    } else if (ended) {
      const auto* parameters =
          tools_.at(partial_call_.name)->Find("parameters");
      const auto* properties =
          parameters ? parameters->Find("properties") : nullptr;
      std::string value;
      auto status = Argument(
          block.substr(tool_value_begin_, close - tool_value_begin_),
          properties ? properties->Find(tool_parameter_name_) : nullptr,
          &value);
      if (!status) return status;
      ToolFragment(std::move(value), false, deltas);
    }
    if (!ended)
      return complete ? Status::Fail("incomplete tool parameter") : Status();
    tool_parse_offset_ = close + closing.size();
    tool_parameter_ = false;
  }
  whitespace(&tool_parse_offset_);
  if (tool_parse_offset_ != block.size())
    return Status::Fail("data after function closing tag");
  if (complete && !tool_function_closed_)
    return Status::Fail("incomplete function closing tag");
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
    pending_offset_ += text.size();
    return Status();
  }
  pending_.append(text);
  while (!pending_.empty()) {
    if (constraints_.json_output && region_ == Region::kContent) {
      // Marker-shaped substrings inside JSON strings are data, not tool or
      // reasoning delimiters. The initial thinking prefix is handled above
      // this region by the same native parser as ordinary requests.
      Emit(region_, pending_, deltas);
      pending_offset_ += pending_.size();
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
        const auto status = StreamTool(body_size, false, deltas);
        if (!status) return Fail(status.message());
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
      status = StreamTool(end, true, deltas);
      if (!status) return Fail(status.message());
      if ((call.custom ? partial_call_.arguments
                       : partial_call_.arguments + '}') != call.arguments ||
          partial_call_.name != call.name || partial_call_.custom != call.custom)
        return Fail("incremental tool arguments differ from validated call");
      if (!call.custom) ToolFragment("}", false, deltas);
      ++tool_count_;
      if (collect_) calls_.push_back(call);
      pending_offset_ += end + kToolClose.size();
      pending_.erase(0, end + kToolClose.size());
      region_ = Region::kContent;
      continue;
    }
    auto marker = region_ == Region::kReasoning ? kThinkClose : kThinkOpen;
    auto found = pending_.find(marker);
    size_t keep = Lookahead(pending_, marker);
    if (region_ == Region::kContent &&
        (!tools_.empty() || constraints_.restrict_tools ||
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
      pending_offset_ += pending_.size() - keep;
      pending_.erase(0, pending_.size() - keep);
      return Status();
    }
    Emit(region_, std::string_view(pending_).substr(0, found), deltas);
    pending_offset_ += found + marker.size();
    pending_.erase(0, found + marker.size());
    region_ = marker == kToolOpen    ? Region::kTool
              : marker == kThinkOpen ? Region::kReasoning
                                     : Region::kContent;
    tool_scan_ = 0;
    if (region_ == Region::kTool) {
      partial_call_ = {};
      custom_input_ = {};
      tool_parse_offset_ = 0;
      tool_header_ = tool_parameter_ = tool_function_closed_ = false;
      tool_parameters_.clear();
    }
  }
  return Status();
}

Status ChatResponseParser::Finish(bool length_limited,
                                  std::vector<ResponseDelta>* deltas,
                                  bool text_stopped) {
  if (!status_) return status_;
  if (!initialized_ || finished_) return Fail("parser already finished");
  finished_ = true;
  if (constraints_.active && (length_limited || text_stopped))
    return Fail("constrained response exhausted output/context budget");
  if (region_ == Region::kTool ||
      (!tools_.empty() && pending_.starts_with("<tool_")))
    return Fail("incomplete tool call");
  if (region_ == Region::kReasoning && !length_limited && !text_stopped)
    return Fail("reasoning block did not close before stop");
  Emit(region_, pending_, deltas);
  pending_offset_ += pending_.size();
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
    // Structured JSON content remains deferred; tool fragments are provisional
    // and become executable only after this whole-response terminal succeeds.
    for (auto& delta : deferred_) deltas->push_back(std::move(delta));
    deferred_.clear();
  }
  return Status();
}

std::string ChatResponseParser::MessageJson() const {
  std::string message = "{\"role\":\"assistant\",\"content\":";
  message += content_.empty() && !calls_.empty() ? "null" : Quote(content_);
  if (!reasoning_.empty())
    message += ',' + Quote(ReasoningFieldName(reasoning_options_.field)) + ':' +
               Quote(reasoning_);
  if (legacy_function_ && !calls_.empty()) {
    message += ",\"function_call\":{\"name\":" + Quote(calls_[0].name) +
               ",\"arguments\":" + Quote(calls_[0].arguments) + '}';
  } else if (!calls_.empty()) {
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
