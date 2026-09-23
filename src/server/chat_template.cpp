#include "q4t/server/chat_template.h"

#include <charconv>
#include <cmath>
#include <string_view>

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
        Status s = io::ParseJson(arguments->str, &parsed, true);
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

Status RenderChatPrompt(const io::Json& request,
                        const std::vector<std::string>& contents,
                        std::string* prompt) {
  prompt->clear();
  const io::Json* messages = request.Find("messages");
  if (!messages || !messages->IsArray() || messages->array.empty() ||
      messages->array.size() != contents.size()) {
    return Status::Fail("messages must be a nonempty array");
  }
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
    if (role == "system") {
      if (i) return Status::Fail("System message must be at the beginning.");
    } else if (role == "user") {
      if (!(text.back().starts_with("<tool_response>") &&
            text.back().ends_with("</tool_response>")))
        last_query = i;
    } else if (role != "assistant" && role != "tool") {
      return Status::Fail("Unexpected message role.");
    }
  }
  if (last_query == ms.size()) return Status::Fail("No user query found.");
  const std::string system = ms[0].GetString("role") == "system" ? text[0] : "";
  const io::Json* tools = request.Find("tools");
  if (tools && !tools->IsNull() && !tools->IsArray()) {
    return Status::Fail("tools must be an array");
  }
  if (tools && tools->IsArray() && !tools->array.empty()) {
    *prompt = "<|im_start|>system\n";
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
  } else if (!system.empty() || !instructions.empty()) {
    *prompt = "<|im_start|>system\n" + instructions;
    if (!system.empty()) {
      if (!instructions.empty()) *prompt += "\n\n";
      *prompt += system;
    }
    *prompt += "<|im_end|>\n";
  }
  for (size_t i = 0; i < ms.size(); ++i) {
    const std::string role = ms[i].GetString("role");
    if (role == "user") {
      *prompt += "<|im_start|>user\n" + text[i] + "<|im_end|>\n";
    } else if (role == "assistant") {
      *prompt += "<|im_start|>assistant\n";
      if (preserve || i > last_query) {
        *prompt += "<think>\n" + Trim(ms[i].GetString("reasoning_content")) +
                   "\n</think>\n\n";
      }
      *prompt += text[i];
      s = AppendToolCalls(ms[i], !text[i].empty(), prompt);
      if (!s) return s;
      *prompt += "<|im_end|>\n";
    } else if (role == "tool") {
      if (i && ms[i - 1].GetString("role") != "tool") {
        *prompt += "<|im_start|>user";
      }
      *prompt += "\n<tool_response>\n" + text[i] + "\n</tool_response>";
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
