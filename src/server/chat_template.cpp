#include "q4t/server/chat_template.h"

#include "q4t/server/chat_tool_history.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/legacy_tools.h"
#include "q4t/server/request_json.h"
#include "q4t/server/reasoning_options.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <string_view>
#include <utility>

namespace q4t::server {
namespace {

// Python/Jinja str.strip uses Unicode whitespace, not the process locale.
bool IsSpace(unsigned cp) {
  return (cp >= 9 && cp <= 13) || (cp >= 0x1c && cp <= 0x20) || cp == 0x85 ||
         cp == 0xa0 || cp == 0x1680 || (cp >= 0x2000 && cp <= 0x200a) ||
         cp == 0x2028 || cp == 0x2029 || cp == 0x202f || cp == 0x205f ||
         cp == 0x3000;
}

std::string Trim(const std::string& s) {
  size_t first = s.size(), last = 0;
  for (size_t i = 0; i < s.size();) {
    const size_t start = i;
    unsigned cp = static_cast<unsigned char>(s[i++]);
    int rest = 0;
    if ((cp & 0xe0) == 0xc0) {
      cp &= 0x1f;
      rest = 1;
    } else if ((cp & 0xf0) == 0xe0) {
      cp &= 0x0f;
      rest = 2;
    } else if ((cp & 0xf8) == 0xf0) {
      cp &= 7;
      rest = 3;
    }
    while (rest-- > 0 && i < s.size()) {
      cp = (cp << 6) | (static_cast<unsigned char>(s[i++]) & 0x3f);
    }
    if (!IsSpace(cp)) {
      if (first == s.size()) first = start;
      last = i;
    }
  }
  return first == s.size() ? "" : s.substr(first, last - first);
}

void Quote(const std::string& s, std::string* out) {
  constexpr char hex[] = "0123456789abcdef";
  *out += '"';
  for (unsigned char c : s) {
    switch (c) {
      case '"':
        *out += "\\\"";
        break;
      case '\\':
        *out += "\\\\";
        break;
      case '\b':
        *out += "\\b";
        break;
      case '\f':
        *out += "\\f";
        break;
      case '\n':
        *out += "\\n";
        break;
      case '\r':
        *out += "\\r";
        break;
      case '\t':
        *out += "\\t";
        break;
      default:
        if (c < 0x20) {
          *out += "\\u00";
          *out += hex[c >> 4];
          *out += hex[c & 15];
        } else {
          *out += static_cast<char>(c);
        }
    }
  }
  *out += '"';
}

// A name is model-visible participant metadata, never an authority or client
// identity. JSON quoting keeps whitespace in the name out of the role syntax;
// escaping angle brackets also prevents tokenizer control-token injection.
std::string Participant(const io::Json& message) {
  const auto* name = message.Find("name");
  if (!name || name->IsNull()) return {};
  std::string escaped;
  Quote(name->str, &escaped);
  std::string result;
  for (const char c : escaped) {
    if (c == '<')
      result += "\\u003c";
    else if (c == '>')
      result += "\\u003e";
    else
      result += c;
  }
  return " name=" + result;
}

std::string Header(const io::Json& message, std::string_view role) {
  return "<|im_start|>" + std::string(role) + Participant(message) + "\n";
}

// Transformers' tojson preserves member order, Unicode and default separators.
Status ToJson(const io::Json& value, std::string* out) {
  using Type = io::Json::Type;
  switch (value.type) {
    case Type::kNull:
      *out += "null";
      break;
    case Type::kBool:
      *out += value.boolean ? "true" : "false";
      break;
    case Type::kString:
      Quote(value.str, out);
      break;
    case Type::kNumber: {
      if (!value.str.empty() &&
          value.str.find_first_of(".eE") == std::string::npos) {
        *out += value.str == "-0" ? "0" : value.str;
        break;
      }
      if (!std::isfinite(value.number)) {
        return Status::Fail("non-finite tool parameter");
      }
      char buffer[64];
      const auto result = std::to_chars(
          buffer, buffer + sizeof(buffer), value.number,
          (std::abs(value.number) >= 1e-4 && std::abs(value.number) < 1e16) ||
                  value.number == 0
              ? std::chars_format::fixed
              : std::chars_format::scientific);
      if (result.ec != std::errc()) return Status::Fail("invalid number");
      std::string number(buffer, result.ptr);
      if (number.find_first_of(".eE") == std::string::npos) number += ".0";
      *out += number;
      break;
    }
    case Type::kArray:
      *out += '[';
      for (size_t i = 0; i < value.array.size(); ++i) {
        if (i) *out += ", ";
        Status s = ToJson(value.array[i], out);
        if (!s) return s;
      }
      *out += ']';
      break;
    case Type::kObject:
      *out += '{';
      for (size_t i = 0; i < value.object.size(); ++i) {
        if (i) *out += ", ";
        Quote(value.object[i].first, out);
        *out += ": ";
        Status s = ToJson(value.object[i].second, out);
        if (!s) return s;
      }
      *out += '}';
      break;
  }
  return Status();
}

const io::Json* Option(const io::Json& request, const char* name) {
  const io::Json* options = request.Find("chat_template_kwargs");
  if (options && options->Has(name)) return options->Find(name);
  return request.Find(name);
}

Status BoolOption(const io::Json& request, const char* name, bool fallback,
                  bool* out) {
  const io::Json* value = Option(request, name);
  if (value && !value->IsBool()) {
    return Status::Fail(std::string(name) + " must be boolean");
  }
  *out = value ? value->boolean : fallback;
  return Status();
}

constexpr std::string_view kToolInstructions =
    "\n\nIf you choose to call a function ONLY reply in the following format "
    "with NO suffix:\n\n<tool_call>\n<function=example_function_name>\n"
    "<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
    "<parameter=example_parameter_2>\nThis is the value for the second "
    "parameter\n"
    "that can span\nmultiple lines\n</parameter>\n</function>\n</tool_call>\n\n"
    "<IMPORTANT>\nReminder:\n- Function calls MUST follow the specified "
    "format: "
    "an inner <function=...></function> block must be nested within "
    "<tool_call></tool_call> XML tags\n- Required parameters MUST be "
    "specified\n"
    "- You may provide optional reasoning for your function call in natural "
    "language BEFORE the function call, but NOT after\n"
    "- If there is no function call available, answer the question like normal "
    "with your current knowledge and do not tell the user about function "
    "calls\n"
    "</IMPORTANT>";

Status AppendToolCalls(const io::Json& message, bool has_content,
                       std::string* prompt) {
  const io::Json* calls = message.Find("tool_calls");
  if (!calls || calls->IsNull()) return Status();
  if (!calls->IsArray()) return Status::Fail("tool_calls must be an array");
  for (size_t i = 0; i < calls->array.size(); ++i) {
    const io::Json& call = calls->array[i];
    const io::Json* fn = call.Find("function");
    if (!fn) fn = &call;
    const io::Json* name = fn->Find("name");
    if (!name || !name->IsString() || name->str.empty()) {
      return Status::Fail("tool call requires a function name");
    }
    *prompt += i ? "\n" : (has_content ? "\n\n" : "");
    *prompt += "<tool_call>\n<function=" + name->str + ">\n";
    const io::Json* arguments = fn->Find("arguments");
    io::Json parsed;
    // OpenAI transports arguments as JSON text; the model template consumes
    // the decoded mapping. Never silently iterate a string or discard it.
    if (arguments && arguments->IsString()) {
      if (arguments->str.empty())
        arguments = nullptr;
      else {
        Status s =
            io::ParseJson(arguments->str, &parsed, true, kRequestJsonLimits);
        if (!s) return Status::Fail("invalid tool arguments JSON");
        arguments = &parsed;
      }
    }
    if (arguments) {
      if (!arguments->IsObject()) {
        return Status::Fail("tool arguments must be an object");
      }
      for (const auto& [key, value] : arguments->object) {
        *prompt += "<parameter=" + key + ">\n";
        if (value.IsString())
          *prompt += value.str;
        else {
          Status s = ToJson(value, prompt);
          if (!s) return s;
        }
        *prompt += "\n</parameter>\n";
      }
    }
    *prompt += "</function>\n</tool_call>";
  }
  return Status();
}

}  // namespace

bool IsChatInstructionRole(std::string_view role) {
  return role == "system" || role == "developer";
}

Status ValidateChatMessageProfile(const io::Json& messages,
                                  std::string* error_param) {
  io::Json request;
  request.type = io::Json::Type::kObject;
  // Avoid copying ordinary histories on this shared validation path.
  if (messages.IsArray() &&
      std::any_of(messages.array.begin(), messages.array.end(),
                  [](const io::Json& message) {
                    return message.Has("function_call") ||
                           message.GetString("role") == "function";
                  })) {
    request.object.emplace_back("messages", messages);
    LegacyToolRequest normalized;
    auto status = NormalizeLegacyTools(request, &normalized, error_param);
    if (status)
      status = ValidateChatMessageProfile(*normalized.request.Find("messages"),
                                         error_param);
    if (!status) LegacyToolErrorParam(request, error_param);
    return status;
  }
  if (error_param) error_param->clear();
  const auto invalid = [&](std::string param, std::string message) {
    if (error_param) *error_param = std::move(param);
    return Status::Fail(std::move(message));
  };
  if (!messages.IsArray() || messages.array.empty())
    return invalid("messages", "messages must be a nonempty array");
  for (size_t i = 0; i < messages.array.size(); ++i) {
    const auto& message = messages.array[i];
    const std::string path = "messages[" + std::to_string(i) + "]";
    if (!message.IsObject()) return invalid(path, "message must be an object");
    const auto role = message.GetString("role");
    const bool instruction = IsChatInstructionRole(role);
    if (!instruction && role != "user" && role != "assistant" && role != "tool")
      return invalid(path + ".role", "unsupported message role");
    const auto* name = message.Find("name");
    if (name && !name->IsNull() &&
        (!name->IsString() || name->str.size() > 4096))
      return invalid(path + ".name",
                     "message name must be a string up to 4096 bytes or null");
    const auto* refusal = message.Find("refusal");
    if (refusal &&
        (role != "assistant" || (!refusal->IsNull() && !refusal->IsString())))
      return invalid(path + ".refusal",
                     "refusal requires assistant and a string or null");
    const auto* content = message.Find("content");
    const auto* calls = message.GetArray("tool_calls");
    const auto* legacy = message.Find("function_call");
    const bool assistant_payload =
        role == "assistant" &&
        ((calls && !calls->array.empty()) || (legacy && !legacy->IsNull()) ||
         (refusal && refusal->IsString()));
    if ((!content || content->IsNull()) && !assistant_payload)
      return invalid(path + ".content", "message content is required");
    if (content && !content->IsNull() && !content->IsString() &&
        !content->IsArray())
      return invalid(path + ".content",
                     "message content must be text or content parts");
    if (content && content->IsArray() && content->array.empty())
      return invalid(path + ".content", "content parts must not be empty");
    if (content && content->IsArray()) {
      for (size_t j = 0; j < content->array.size(); ++j) {
        const auto& part = content->array[j];
        if (part.GetString("type") != "refusal" && !part.Has("refusal"))
          continue;
        const auto* text = part.Find("refusal");
        if (role != "assistant" || content->array.size() != 1 ||
            !InputFields(part, {"type", "refusal"}) ||
            part.GetString("type") != "refusal" || !text || !text->IsString())
          return invalid(path + ".content[" + std::to_string(j) + "]",
                         "refusal must be one assistant refusal content part");
      }
    }
    std::string_view reasoning;
    auto reason_status =
        ResolveMessageReasoning(message, i, &reasoning, error_param);
    if (!reason_status) return reason_status;
    if (!instruction) continue;
    if (!content || content->IsNull() || content->IsString()) continue;
    if (!content->IsArray())
      return invalid(path + ".content", "instruction content must be text");
    for (size_t j = 0; j < content->array.size(); ++j) {
      const auto& part = content->array[j];
      const auto* type = part.Find("type");
      const auto* text = part.Find("text");
      auto status = InputFields(part, {"type", "text"});
      if (!status || !text || !text->IsString() ||
          (type && (!type->IsString() || type->str != "text")))
        return invalid(path + ".content[" + std::to_string(j) + "]",
                       "instruction content parts must contain only text");
    }
  }
  return Status();
}

Status RenderChatPrompt(const io::Json& request,
                        const std::vector<std::string>& contents,
                        std::string* prompt) {
  if (HasLegacyTools(request)) {
    LegacyToolRequest normalized;
    const auto status = NormalizeLegacyTools(request, &normalized);
    return status ? RenderChatPrompt(normalized.request, contents, prompt)
                  : status;
  }
  prompt->clear();
  auto definitions = ValidateToolDefinitions(request);
  if (!definitions) return definitions;
  const io::Json* messages = request.Find("messages");
  if (!messages || !messages->IsArray() || messages->array.empty() ||
      messages->array.size() != contents.size()) {
    return Status::Fail("messages must be a nonempty array");
  }
  auto profile = ValidateChatMessageProfile(*messages);
  if (!profile) return profile;
  std::vector<size_t> tool_result_order;
  auto history = PlanToolResultOrder(*messages, &tool_result_order);
  if (!history) return history;
  const io::Json* options = request.Find("chat_template_kwargs");
  if (options && !options->IsObject()) {
    return Status::Fail("chat_template_kwargs must be an object");
  }
  bool thinking, preserve;
  Status s = BoolOption(request, "enable_thinking", true, &thinking);
  if (!s) return s;
  s = BoolOption(request, "preserve_thinking", true, &preserve);
  if (!s) return s;
  std::string instructions;
  if (thinking) {
    const io::Json* value = Option(request, "reasoning_effort");
    if (value && !value->IsString()) {
      return Status::Fail("reasoning_effort must be a string");
    }
    const std::string effort = value ? value->str : "xhigh";
    if (effort == "xhigh") {
      instructions =
          "Reasoning effort is set to xhigh. Please think carefully "
          "through the task, validate key assumptions, consider plausible "
          "alternatives, and prioritize correctness, consistency, and clarity "
          "in the final answer.";
    } else if (effort == "low") {
      instructions =
          "Reasoning effort is set to low. Keep your thinking brief "
          "and focused, moving directly to the conclusion without unnecessary "
          "elaboration.";
    } else if (effort != "medium") {
      return Status::Fail("reasoning_effort must be xhigh, medium, or low");
    }
  }
  const auto& ms = messages->array;
  std::vector<std::string> text;
  size_t last_query = ms.size();
  for (size_t i = 0; i < ms.size(); ++i) {
    if (!ms[i].IsObject()) return Status::Fail("message must be an object");
    text.push_back(Trim(contents[i]));
    const std::string role = ms[i].GetString("role");
    if (role == "assistant") {
      const auto refusal = Trim(ms[i].GetString("refusal"));
      if (!refusal.empty() && text.back() != refusal) {
        if (!text.back().empty()) text.back() += "\n\n";
        text.back() += refusal;
      }
    }
    if (role == "user") {
      if (!(text.back().starts_with("<tool_response>") &&
            text.back().ends_with("</tool_response>")))
        last_query = i;
    }
  }
  const bool leading_instruction =
      IsChatInstructionRole(ms[0].GetString("role"));
  const std::string system = leading_instruction ? text[0] : "";
  const auto first_header =
      leading_instruction ? Header(ms[0], "system") : "<|im_start|>system\n";
  const io::Json* tools = request.Find("tools");
  if (tools && !tools->IsNull() && !tools->IsArray()) {
    return Status::Fail("tools must be an array");
  }
  if (tools && tools->IsArray() && !tools->array.empty()) {
    *prompt = first_header;
    if (!instructions.empty()) *prompt += instructions + "\n\n";
    *prompt +=
        "# Tools\n\nYou have access to the following functions:\n\n<tools>";
    for (const auto& tool : tools->array) {
      if (!tool.IsObject()) return Status::Fail("tool must be an object");
      *prompt += '\n';
      s = ToJson(tool, prompt);
      if (!s) return s;
    }
    *prompt += "\n</tools>";
    *prompt += kToolInstructions;
    if (!system.empty()) *prompt += "\n\n" + system;
    *prompt += "<|im_end|>\n";
  } else if (!system.empty() || !instructions.empty() ||
             (leading_instruction && !Participant(ms[0]).empty())) {
    *prompt = first_header + instructions;
    if (!system.empty()) {
      if (!instructions.empty()) *prompt += "\n\n";
      *prompt += system;
    }
    *prompt += "<|im_end|>\n";
  }
  for (size_t i = 0; i < ms.size(); ++i) {
    const std::string role = ms[i].GetString("role");
    if (IsChatInstructionRole(role)) {
      if (i) *prompt += Header(ms[i], "system") + text[i] + "<|im_end|>\n";
    } else if (role == "user") {
      *prompt += Header(ms[i], "user") + text[i] + "<|im_end|>\n";
    } else if (role == "assistant") {
      *prompt += Header(ms[i], "assistant");
      if (preserve || i > last_query) {
        std::string_view reasoning;
        auto status = ResolveMessageReasoning(ms[i], i, &reasoning);
        if (!status) return status;
        *prompt +=
            "<think>\n" + Trim(std::string(reasoning)) + "\n</think>\n\n";
      }
      *prompt += text[i];
      s = AppendToolCalls(ms[i], !text[i].empty(), prompt);
      if (!s) return s;
      *prompt += "<|im_end|>\n";
    } else if (role == "tool") {
      if (i == 0 || ms[i - 1].GetString("role") != "tool") {
        *prompt += "<|im_start|>user";
      }
      const auto result = tool_result_order[i];
      const auto participant = Participant(ms[result]);
      *prompt += "\n<tool_response>\n";
      if (!participant.empty()) *prompt += participant.substr(1) + "\n";
      *prompt += text[result] + "\n</tool_response>";
      if (i + 1 == ms.size() || ms[i + 1].GetString("role") != "tool") {
        *prompt += "<|im_end|>\n";
      }
    }
  }
  *prompt += "<|im_start|>assistant\n";
  *prompt += thinking ? "<think>\n" : "<think>\n\n</think>\n\n";
  return Status();
}

}  // namespace q4t::server
