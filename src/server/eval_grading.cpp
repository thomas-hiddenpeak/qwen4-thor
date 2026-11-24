#include "eval_grading.h"

#include <algorithm>
#include <charconv>

#include "object_protocol.h"
#include "q4t/server/program_worker.h"

// The pinned Unicode 17 tables and UTF-8 codec are already linked in q4t.
// This calls the plain C conversion routines without creating an interpreter.
extern "C" {
#include <cutils.h>
#include <libunicode.h>
}

namespace q4t::server::eval_detail {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string_view param, std::string message) {
  auto result = Error(400, "invalid_eval_template", std::move(message));
  result.value = Object({{"param", String(std::string(param))}});
  return result;
}

ObjectReply Append(std::string_view text, size_t* remaining, std::string* out) {
  if (text.size() > *remaining)
    return Error(413, "eval_expansion_limit", "Evaluation text exceeds budget");
  *remaining -= text.size();
  out->append(text);
  return {};
}

ObjectReply Fold(std::string_view text, size_t* remaining, std::string* out) {
  while (!text.empty()) {
    const auto* first = reinterpret_cast<const uint8_t*>(text.data());
    const uint8_t* next = first;
    const auto point = unicode_from_utf8(
        first, static_cast<int>(std::min<size_t>(text.size(), 4)), &next);
    if (point < 0 || point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff))
      return Invalid("testing_criteria", "Expected valid UTF-8");
    text.remove_prefix(static_cast<size_t>(next - first));
    uint32_t points[LRE_CC_RES_LEN_MAX];
    const int count = lre_case_conv(points, point, 2);
    for (int i = 0; i < count; ++i) {
      uint8_t bytes[UTF8_CHAR_LEN_MAX];
      const auto size = unicode_to_utf8(bytes, points[i]);
      auto result =
          Append({reinterpret_cast<char*>(bytes), static_cast<size_t>(size)},
                 remaining, out);
      if (!result.ok()) return result;
    }
  }
  return {};
}

bool Contains(const std::string& text, const std::string& pattern) {
  return pattern.empty() ||
         memmem(text.data(), text.size(), pattern.data(), pattern.size());
}
}  // namespace

const io::Json* ResolveVariable(std::string_view path, const Json& row) {
  const Json* value = &row;
  size_t depth = 0;
  for (;;) {
    const auto dot = path.find('.');
    const auto key = path.substr(0, dot);
    if (key.empty() || ++depth > 64) return nullptr;
    const auto open = key.find('[');
    const auto field = key.substr(0, open);
    if (depth == 1 && field != "item" && field != "sample") return nullptr;
    const auto at = [](const Json* list, std::string_view digits) -> const Json* {
      if (!list || !list->IsArray() || digits.empty()) return nullptr;
      size_t index = 0;
      const auto [end, ec] = std::from_chars(digits.data(), digits.data() + digits.size(), index);
      if (ec != std::errc{} || end != digits.data() + digits.size() || index >= list->array.size())
        return nullptr;
      return &list->array[index];
    };
    // Existing literal object keys (including bracket characters) keep precedence.
    if (const auto* literal = value->Find(std::string(key));
        literal && (depth != 1 || key == field)) {
      value = literal;
    } else {
      value = value->IsArray() ? at(value, field) : value->Find(std::string(field));
      if (!value) return nullptr;
      auto brackets = key.substr(field.size());
      while (!brackets.empty()) {
        const auto close = brackets.find(']');
        if (brackets.front() != '[' || close == brackets.npos || ++depth > 64)
          return nullptr;
        value = at(value, brackets.substr(1, close - 1));
        if (!value) return nullptr;
        brackets.remove_prefix(close + 1);
      }
    }
    if (dot == path.npos) return value;
    path.remove_prefix(dot + 1);
  }
}

ObjectReply RenderText(std::string_view text, const io::Json& row,
                       std::string_view param, size_t* remaining,
                       std::string* rendered) {
  std::string output;
  while (!text.empty()) {
    const auto start = text.find("{{");
    auto result = Append(text.substr(0, start), remaining, &output);
    if (!result.ok()) return result;
    if (start == text.npos) break;
    text.remove_prefix(start + 2);
    const auto end = text.find("}}");
    if (end == text.npos) return Invalid(param, "Unclosed evaluation variable");
    auto path = text.substr(0, end);
    while (path.starts_with(' ')) path.remove_prefix(1);
    while (path.ends_with(' ')) path.remove_suffix(1);
    const auto* value = ResolveVariable(path, row);
    if (!value) return Invalid(param, "Unknown evaluation variable");
    if (value->IsString()) {
      result = Append(value->str, remaining, &output);
    } else {
      result = Append(OutputJson(*value), remaining, &output);
    }
    if (!result.ok()) return result;
    text.remove_prefix(end + 2);
  }
  *rendered = std::move(output);
  return {};
}

ObjectReply GradePython(const io::Json& criterion, const io::Json& row,
                        const StateAccess& access) {
  const auto tag = criterion.GetString("image_tag");
  if (!tag.empty() && tag != "q4t-python3.12")
    return Invalid("testing_criteria.image_tag",
                   "Python grader image is unavailable");
  const auto* item = row.Find("item");
  const auto* sample = row.Find("sample");
  if (!item || !item->IsObject())
    return Invalid("data_source.source.item",
                   "Python grader requires a dataset item");
  auto request = Object(
      {{"files", Array()},
       {"code", String(criterion.GetString("source"))},
       {"grader",
        Object({{"sample", sample ? *sample : Object()}, {"item", *item}})}});
  auto run = ExecutePythonWorker(
      request, {access.scope, access.deadline, access.cancelled});
  if (!run.ok()) return run;
  const bool invalid = run.value.GetBool("error");
  const auto value = invalid ? Number(0) : *run.value.Find("score");
  ObjectReply result;
  result.value = Object(
      {{"name", String(criterion.GetString("name"))},
       {"type", String("python")},
       {"score", value},
       {"passed",
        Boolean(!invalid &&
                value.number >= criterion.GetNumber("pass_threshold", 0.5))}});
  if (invalid)
    Set(&result.value, "error",
        Object({{"code", String("invalid_python_grade")},
                {"message",
                 String("Python grade failed or returned an invalid value")}}));
  return result;
}

ObjectReply GradeString(const io::Json& criterion, const io::Json& row,
                        size_t* remaining) {
  const auto* input = criterion.Find("input");
  const auto* reference = criterion.Find("reference");
  if (!input || !input->IsString() || !reference || !reference->IsString())
    return Invalid("testing_criteria", "String check requires two texts");
  std::string left, right;
  auto result =
      RenderText(input->str, row, "testing_criteria.input", remaining, &left);
  if (!result.ok()) return result;
  result = RenderText(reference->str, row, "testing_criteria.reference",
                      remaining, &right);
  if (!result.ok()) return result;
  const auto operation = criterion.GetString("operation");
  bool passed = false;
  if (operation == "eq") {
    passed = left == right;
  } else if (operation == "ne") {
    passed = left != right;
  } else if (operation == "like") {
    passed = Contains(left, right);
  } else if (operation == "ilike") {
    std::string folded_left, folded_right;
    result = Fold(left, remaining, &folded_left);
    if (!result.ok()) return result;
    result = Fold(right, remaining, &folded_right);
    if (!result.ok()) return result;
    passed = Contains(folded_left, folded_right);
  } else {
    return Invalid("testing_criteria.operation", "Unknown string operation");
  }
  result.value = Object({{"name", String(criterion.GetString("name"))},
                         {"type", String("string_check")},
                         {"score", Number(passed ? 1 : 0)},
                         {"passed", Boolean(passed)}});
  return result;
}
}  // namespace q4t::server::eval_detail
