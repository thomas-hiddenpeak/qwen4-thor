#include "q4t/server/custom_tools.h"

#include <stdexcept>

#include "q4t/server/chat_plan.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/custom_grammar.h"
#include "q4t/server/http_response.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/test.h"
#ifdef Q4T_ENABLE_XGRAMMAR
#include <xgrammar/compiler.h>
#include <xgrammar/matcher.h>
#endif

namespace {
using namespace q4t::server;
using q4t::io::Json;
constexpr auto kPrefix = "<|im_start|>assistant\n<think>\n\n</think>\n\n";
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("invalid fixture");
  return value;
}
Json& Member(Json& value, const std::string& key) {
  for (auto& [name, member] : value.object)
    if (name == key) return member;
  throw std::runtime_error("missing fixture member");
}
std::string Quote(std::string_view text) {
  return '"' + detail::JsonEscape(std::string(text)) + '"';
}
Json Request(std::string_view syntax = "text", std::string_view grammar = "") {
  const auto format = syntax == "text"
                          ? R"({"type":"text"})"
                          : R"({"type":"grammar","grammar":{"syntax":)" +
                                Quote(syntax) + R"(,"definition":)" +
                                Quote(grammar) + "}}";
  return Parse(
      R"({"messages":[{"role":"user","content":"q"}],)"
      R"("enable_thinking":false,"tools":[{"type":"custom",)"
      R"("custom":{"name":"read","format":)" +
      std::string(format) +
      R"(}}],"tool_choice":{"type":"custom","custom":{"name":"read"}}})");
}
std::string Call(std::string_view input, std::string_view name = "read") {
  return "<tool_call>\n<custom=" + std::string(name) + ">\n" +
         EncodeCustomInput(input) + "\n</custom>\n</tool_call>";
}
OutputConstraints Plan(const Json& request) {
  OutputConstraints plan;
  const auto status = PlanOutputConstraints(request, &plan);
  if (!status) throw std::runtime_error(status.message());
  return plan;
}
#ifdef Q4T_ENABLE_XGRAMMAR
std::vector<std::string> Vocabulary() {
  std::vector<std::string> result(257);
  for (int c = 0; c < 256; ++c) result[c + 1] = char(c);
  return result;
}
bool Language(std::string_view syntax, std::string_view definition,
              const std::vector<std::string>& yes,
              const std::vector<std::string>& no) {
  auto plan = Plan(Request(syntax, definition));
  xgrammar::TokenizerInfo tokenizer(Vocabulary(), xgrammar::VocabType::RAW, 257,
                                    std::vector<int32_t>{0}, false);
  xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
  const auto compiled = compiler.CompileStructuralTag(
      ExpandCustomGrammarFormats(plan.structural_tag));
  const auto accepts = [&](const std::string& input) {
    xgrammar::GrammarMatcher matcher(compiled);
    return matcher.AcceptString(Call(input)) && matcher.AcceptToken(0) &&
           matcher.IsTerminated();
  };
  for (const auto& value : yes) {
    if (!accepts(value)) {
      std::fprintf(stderr, "expected grammar accept: %s\n",
                   Quote(value).c_str());
      return false;
    }
  }
  for (const auto& value : no) {
    if (accepts(value)) {
      std::fprintf(stderr, "expected grammar reject: %s\n",
                   Quote(value).c_str());
      return false;
    }
  }
  return true;
}
#endif
}  // namespace

Q4T_TEST(custom_tools_plan_choice_filter_and_history) {
  auto request = Request();
  Member(request, "tool_choice") =
      Parse(R"({"type":"allowed_tools",)"
            R"("allowed_tools":{"mode":"required","tools":[)"
            R"({"type":"custom","custom":{"name":"read"}}]}})");
  Member(request, "tools")
      .array.push_back(
          Parse(R"({"type":"function",)"
                R"("function":{"name":"other","parameters":{"type":"object",)"
                R"("properties":{},"additionalProperties":false}}})"));
  const auto plan = Plan(request);
  Q4T_CHECK(plan.active && plan.restrict_tools && plan.AllowsTool("read"));
  Q4T_CHECK(!plan.AllowsTool("other"));
  std::string prompt, error;
  std::vector<VisionItem> items;
  Q4T_CHECK(PrepareChatPrompt(request, plan, &prompt, &items, &error));
  Q4T_CHECK(prompt.find("\"other\"") == prompt.npos);
  Q4T_CHECK(prompt.find("<custom=") != prompt.npos);
  const std::string input = "read \"文档\"\n</custom></tool_call>\\done";
  Member(request, "messages") = Parse(
      R"([{"role":"assistant","content":null,)"
      R"("tool_calls":[{"id":"a","type":"custom","custom":{"name":"read",)"
      R"("input":)" +
      Quote(input) +
      R"(}}]},{"role":"tool",)"
      R"("tool_call_id":"a","content":"record"}])");
  Q4T_CHECK(PrepareChatPrompt(request, plan, &prompt, &items, &error));
  Q4T_CHECK(prompt.find(EncodeCustomInput(input)) != prompt.npos);
  Member(request, "messages").array.pop_back();
  Q4T_CHECK(!PrepareChatPrompt(request, plan, &prompt, &items, &error));
  return true;
}

Q4T_TEST(custom_tools_shape_errors_are_precise) {
  for (
      const auto& [custom, path] :
      std::vector<std::pair<std::string, std::string>>{
          {R"({"name":"read","format":null})", "format"},
          {R"({"name":"read","format":{"type":"text","extra":1}})",
           "format.extra"},
          {R"({"name":"read","format":{"type":"grammar","grammar":{"syntax":"ebnf","definition":"x"}}})",
           "format.grammar.syntax"},
          {R"({"name":"read","format":{"type":"grammar","grammar":{"syntax":"regex","definition":""}}})",
           "format.grammar.definition"},
          {R"({"name":"bad<name"})", "name"}}) {
    auto request = Request();
    Member(Member(request, "tools").array[0], "custom") = Parse(custom);
    OutputConstraints plan;
    std::string error;
    Q4T_CHECK(!PlanOutputConstraints(request, &plan, &error));
    Q4T_CHECK(error == "tools[0].custom." + path);
  }
  auto request = Request();
  Member(request, "tool_choice") =
      Parse(R"({"type":"function","function":{"name":"read"}})");
  OutputConstraints plan;
  Q4T_CHECK(!PlanOutputConstraints(request, &plan));
  return true;
}

Q4T_TEST(custom_tools_incremental_unicode_and_all_boundaries) {
  std::string input = "汉字😀\\\"\n\t</custom></tool_call><think>";
  for (int c = 0; c < 32; ++c) input += char(c);
  const auto encoded = EncodeCustomInput(input);
  Q4T_CHECK(encoded.find('<') == encoded.npos);
  for (size_t split = 0; split <= encoded.size(); ++split) {
    CustomInputStream stream;
    size_t first, second;
    std::string a, b;
    Q4T_CHECK(stream.Append(encoded.substr(0, split), &first, &a));
    Q4T_CHECK(stream.Append(encoded.substr(split) + "trailing", &second, &b));
    Q4T_CHECK(first + second == encoded.size() && stream.Done());
    Q4T_CHECK(a + b == input);
  }
  CustomInputStream stream;
  size_t consumed;
  std::string value;
  Q4T_CHECK(stream.Append(R"("\ud83d\ude00")", &consumed, &value));
  Q4T_CHECK(value == "😀" && stream.Done());
  return true;
}

Q4T_TEST(custom_tools_invalid_encoding_cannot_finish) {
  for (const auto& value : std::vector<std::string>{
           R"("\ud800x")", R"("\ude00")", R"("\uZZZZ")", R"("\x")",
           std::string("\"\xc0\x80\"", 4), std::string("\"\xed\xa0\x80\"", 5),
           std::string("\"\xf4\x90\x80\x80\"", 6), std::string("\"\n\"", 3)}) {
    CustomInputStream stream;
    size_t consumed;
    std::string output;
    Q4T_CHECK(!stream.Append(value, &consumed, &output));
    Q4T_CHECK(!stream.Done());
  }
  return true;
}

Q4T_TEST(custom_tools_response_every_split_and_incremental_payload) {
  const auto request = Request();
  const std::string input = "read \"中国\"\n</custom>\\😀";
  const auto raw = Call(input);
  for (size_t split = 0; split <= raw.size(); ++split) {
    ChatResponseParser parser;
    Q4T_CHECK(parser.Init(request, kPrefix, "custom", true));
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Append(raw.substr(0, split), &deltas));
    Q4T_CHECK(parser.Append(raw.substr(split), &deltas));
    Q4T_CHECK(parser.Finish(false, &deltas));
    Q4T_CHECK(std::string(parser.ToolFinishReason()) == "tool_calls");
    std::string assembled;
    bool saw_initial = false;
    for (const auto& delta : deltas) {
      if (delta.kind != ResponseDelta::Kind::kTool) continue;
      const auto wire = Parse(delta.ToJson());
      const auto& call = wire.Find("tool_calls")->array[0];
      Q4T_CHECK(call.GetInt("index", -1) == 0 && !call.Has("function"));
      const auto* custom = call.Find("custom");
      Q4T_CHECK(custom);
      if (call.Has("id")) {
        Q4T_CHECK(!saw_initial && custom->GetString("name") == "read");
        saw_initial = true;
      }
      assembled += custom->GetString("input");
    }
    Q4T_CHECK(saw_initial && assembled == input);
    Q4T_CHECK(Parse(parser.MessageJson())
                  .Find("tool_calls")
                  ->array[0]
                  .Find("custom")
                  ->GetString("input") == input);
  }
  return true;
}

Q4T_TEST(custom_tools_truncation_type_change_and_mixed_calls) {
  auto request = Request();
  ChatResponseParser parser;
  Q4T_CHECK(parser.Init(request, kPrefix, "a", true));
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Append("<tool_call><custom=read>\"partial", &deltas));
  Q4T_CHECK(!deltas.empty() && !parser.Finish(true, &deltas));
  Member(request, "tool_choice") = Parse("\"required\"");
  Member(request, "tools")
      .array.push_back(
          Parse(R"({"type":"function",)"
                R"("function":{"name":"noop","parameters":{"type":"object",)"
                R"("properties":{},"additionalProperties":false}}})"));
  ChatResponseParser mixed;
  Q4T_CHECK(mixed.Init(request, kPrefix, "b", true));
  deltas.clear();
  Q4T_CHECK(mixed.Append(
      Call("first") + "<tool_call><function=noop></function></tool_call>" +
          Call("last"),
      &deltas));
  Q4T_CHECK(mixed.Finish(false, &deltas));
  const auto message = Parse(mixed.MessageJson());
  const auto& calls = message.Find("tool_calls")->array;
  Q4T_CHECK(calls.size() == 3 && calls[0].GetString("type") == "custom" &&
            calls[1].GetString("type") == "function" &&
            calls[2].Find("custom")->GetString("input") == "last");
  ChatResponseParser wrong;
  Q4T_CHECK(wrong.Init(request, kPrefix, "c", true));
  Q4T_CHECK(!wrong.Append("<tool_call><function=read></function></tool_call>",
                          &deltas));
  return true;
}

#ifdef Q4T_ENABLE_XGRAMMAR
Q4T_TEST(custom_grammar_text_preserves_control_unicode_and_delimiters) {
  Q4T_CHECK(Language(
      "text", "", {"", "中国😀", "\n\\\"</tool_call>", std::string("\0\1", 2)},
      {}));
  return true;
}
Q4T_TEST(custom_grammar_regex_rust_unicode_flags_groups_and_encoding) {
  Q4T_CHECK(
      Language("regex", R"((?i:read)\s+(?P<path>\p{Han}+|docs/MODEL\.md))",
               {"READ 中国", "read docs/MODEL.md", "ReAd\t文档"},
               {"READ China", "read docs/MODELxmd", "read中国", "read 中国x"}));
  Q4T_CHECK(Language("regex", R"("[A-Z]"\n</custom>\\\p{Greek}+)",
                     {"\"A\"\n</custom>\\αβ"},
                     {"\"a\"\n</custom>\\αβ", "\"A\"\\n</custom>\\αβ"}));
  Q4T_CHECK(Language("regex", R"([a-z&&[^aeiou]]{2,3})", {"bc", "xyz"},
                     {"ab", "abcd", "b"}));
  return true;
}
Q4T_TEST(custom_grammar_regex_anchors_and_unicode_word_boundaries) {
  Q4T_CHECK(Language("regex", R"(^read\b 中国\b$)", {"read 中国"},
                     {"read中国", "xread 中国", "read 中国x"}));
  Q4T_CHECK(Language("regex", R"((?m:^a$)\n(?m:^b$))", {"a\nb"},
                     {"a\nb\n", "ab", "a\r\nb"}));
  Q4T_CHECK(
      Language("regex", R"((?i:ä)\B(?i:ö))", {"ÄÖ", "äö"}, {"ä ö", "äO"}));
  Q4T_CHECK(Language("regex", R"(a\Ab)", {}, {"ab", "a", "b", ""}));
  return true;
}
Q4T_TEST(custom_grammar_lark_regex_imports_recursion_and_lexeme_boundaries) {
  Q4T_CHECK(
      Language("lark",
               "start: \"read\"i PATH INT\nPATH: /docs\\/[A-Z]+\\.md/\n%import "
               "common.INT\n%import common.WS_INLINE\n%ignore WS_INLINE\n",
               {"READ docs/MODEL.md 1", "read\tdocs/STATUS.md 16"},
               {"read docs/MODEL.md x", "read d o c s/MODEL.md 1",
                "read docs/model.md 1"}));
  Q4T_CHECK(Language("lark", "start: '(' start ')' | WORD\nWORD: /\\p{Han}+/\n",
                     {"文档", "((中国))"}, {"(中国", "(China)", "(中)国"}));
  Q4T_CHECK(Language("lark", "start: \"ä\"i /[A-Z]+/i \"\\n\" \"</custom>\"\n",
                     {"Äabc\n</custom>"},
                     {"Äabc\\n</custom>", "ä1\n</custom>"}));
  return true;
}
Q4T_TEST(custom_grammar_lark_literal_cannot_alias_internal_placeholder) {
  Q4T_CHECK(Language("lark",
                     R"(start: "\u00514T_REGEX_PLACEHOLDER_0_END" /a+/)",
                     {"Q4T_REGEX_PLACEHOLDER_0_ENDaaa"},
                     {"aaaa", "Q4T_REGEX_PLACEHOLDER_0_ENDb"}));
  Q4T_CHECK(Language("lark", R"(start: /\x514T_REGEX_PLACEHOLDER_0_END/)",
                     {"Q4T_REGEX_PLACEHOLDER_0_END"},
                     {"aaa", "Q4T_REGEX_PLACEHOLDER_1_END"}));
  return true;
}

Q4T_TEST(custom_grammar_worker_enforces_actual_encoded_token_mask) {
  const auto plan = Plan(Request("regex", R"(^read \"中国\"$)"));
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(StructuredDecoder::Create(plan, Vocabulary(), 257, {0}, &decoder));
  for (const unsigned char c : Call("read \"中国\"")) {
    std::vector<uint16_t> logits(257, 0);
    logits[c + 1] = 0x3f80;  // BF16 1.0, unique requested token.
    int32_t token = -1;
    Q4T_CHECK(decoder->Select(logits, &token) && token == c + 1);
  }
  std::vector<uint16_t> logits(257, 0);
  logits[0] = 0x3f80;
  int32_t token = -1;
  Q4T_CHECK(decoder->Select(logits, &token) && token == 0 &&
            decoder->Terminated());
  return true;
}
Q4T_TEST(custom_grammar_invalid_and_resource_errors_do_not_weaken) {
  for (const auto& pattern : {"(?=read)read", "[unclosed", "(?z)read"}) {
    bool rejected = false;
    try {
      (void)ExpandCustomGrammarFormats(
          Plan(Request("regex", pattern)).structural_tag);
    } catch (const std::runtime_error&) {
      rejected = true;
    }
    Q4T_CHECK(rejected);
  }
  bool bounded = false;
  try {
    (void)ExpandCustomGrammarFormats(
        Plan(Request("regex", "^x{9000}$")).structural_tag);
  } catch (const std::length_error&) {
    bounded = true;
  }
  Q4T_CHECK(bounded);
  // A rejected custom grammar cannot modify the function-only path.
  Q4T_CHECK(ExpandCustomGrammarFormats("{\"type\":\"json_schema\"}") ==
            "{\"type\":\"json_schema\"}");
  return true;
}
#endif
