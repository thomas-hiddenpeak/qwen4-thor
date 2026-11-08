#include "../src/server/custom_regex.h"
#include "../src/server/schema_number_grammar.h"

#include <stdexcept>
#include <string>
#include <vector>

#include <xgrammar/compiler.h>
#include <xgrammar/matcher.h>

#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
q4t::io::Json Parse(const std::string& text) {
  q4t::io::Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("invalid schema language fixture");
  return value;
}
std::vector<std::string> Vocabulary() {
  std::vector<std::string> result(257);
  for (int c = 0; c < 256; ++c) result[c + 1] = char(c);
  return result;
}
xgrammar::CompiledGrammar Compile(const std::string& ebnf) {
  xgrammar::TokenizerInfo tokenizer(Vocabulary(), xgrammar::VocabType::RAW, 257,
                                    std::vector<int32_t>{0}, false);
  xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
  return compiler.CompileGrammar(xgrammar::Grammar::FromEBNF(ebnf));
}
bool Accepts(const xgrammar::CompiledGrammar& compiled,
             const std::string& text) {
  xgrammar::GrammarMatcher matcher(compiled);
  return matcher.AcceptString(text) && matcher.AcceptToken(0) &&
         matcher.IsTerminated();
}
bool Language(const std::string& ebnf, const std::vector<std::string>& yes,
              const std::vector<std::string>& no) {
  const auto compiled = Compile(ebnf);
  for (const auto& text : yes) {
    if (!Accepts(compiled, text)) {
      std::fprintf(stderr, "expected accept: %s\n", text.c_str());
      return false;
    }
  }
  for (const auto& text : no) {
    if (Accepts(compiled, text)) {
      std::fprintf(stderr, "expected reject: %s\n", text.c_str());
      return false;
    }
  }
  return true;
}
}  // namespace

Q4T_TEST(schema_language_intersection_search_length_and_assertions) {
  Q4T_CHECK(
      Language(SchemaRegexEbnf({R"((?s:.*)(?:a\bb)(?s:.*))", R"((?s:.{1,8}))"}),
               {}, {"ab", "a b", ""}));
  Q4T_CHECK(Language(
      SchemaRegexEbnf({R"((?s:.*)(?:\b中国\b)(?s:.*))", R"((?s:.{2,4}))"}),
      {"中国", " 中国 ", "中国!", "\n中国"},
      {"x中国", "中国人", " 中国 !", ""}));
  Q4T_CHECK(Language(SchemaRegexEbnf({R"((?m:^a$)\n(?m:^b$))", R"((?s:.{3}))"}),
                     {"a\nb"}, {"a\r\nb", "a\nb\n", "ab"}));
  return true;
}

Q4T_TEST(schema_language_matcher_unicode_fullmatch_and_work_bound) {
  const auto pattern = R"((?s:.*)(?:\b中国\b)(?s:.*))";
  for (const auto& [text, expected] :
       std::vector<std::pair<std::string, bool>>{{"中国", true},
                                                 {"中", false},
                                                 {"中国!", true},
                                                 {" 中国产", false},
                                                 {"😀中国\n", true}}) {
    size_t work = 1000000;
    Q4T_CHECK(SchemaRegexMatches(pattern, text, &work) == expected);
  }
  size_t work = 0;
  bool exhausted = false;
  try {
    (void)SchemaRegexMatches(".*", "text", &work);
  } catch (const std::length_error&) {
    exhausted = true;
  }
  Q4T_CHECK(exhausted);
  return true;
}

Q4T_TEST(schema_language_counted_chains_preserve_holes_and_dead_prefixes) {
  Q4T_CHECK(Language(
      SchemaRegexEbnf({R"((?s:.*)(?:^[a-z]+$)(?s:.*))", R"((?s:.{1,256}))"}),
      {"a", "hello", std::string(256, 'z')},
      {"", "A", "a\n", std::string(257, 'z')}));
  Q4T_CHECK(Language(SchemaRegexEbnf(
      {R"((?s:.*)(?:^(?:a{1,3}|a{7,8})$)(?s:.*))", R"((?s:.{1,256}))"}),
      {"a", "aaa", "aaaaaaa", "aaaaaaaa"},
      {"", "aaaa", "aaaaa", "aaaaaa", "aaaaaaaaa"}));
  const auto grammar = Compile(SchemaRegexEbnf(
      {R"((?s:.*)(?:^[^\n]*$)(?s:.*))", R"((?s:.{1,256}))"}));
  xgrammar::GrammarMatcher matcher(grammar);
  Q4T_CHECK(matcher.AcceptString("title"));
  Q4T_CHECK(!matcher.AcceptString("\n"));
  Q4T_CHECK(matcher.AcceptToken(0));
  return true;
}

Q4T_TEST(schema_language_single_pattern_rejects_impossible_prefixes) {
  // A schema with only pattern has no length/format intersection. Search
  // wrapping must not leave a prefix that can never satisfy its anchors.
  const auto grammar = Compile(SchemaRegexEbnf(
      {R"((?s:.*)(?:^(left_to_right|right_to_left|up|down|stationary)$)(?s:.*))"}));
  const std::vector<std::string> words{
      "left_to_right", "right_to_left", "up", "down", "stationary"};
  for (const auto& prefix : std::vector<std::string>{
           "", "l", "left_", "right", "rightward", "right_to_left", "up",
           "upward", "down", "stationary", "stationary\n", " x", "\n"}) {
    bool expected = false;
    for (const auto& word : words) expected |= word.starts_with(prefix);
    xgrammar::GrammarMatcher matcher(grammar);
    Q4T_CHECK(matcher.AcceptString(prefix) == expected);
  }
  const auto bounded = Compile(SchemaRegexEbnf(
      {R"((?s:.*)(?:^[^\n]*$)(?s:.*))"}));
  xgrammar::GrammarMatcher matcher(bounded);
  Q4T_CHECK(matcher.AcceptString("title"));
  Q4T_CHECK(!matcher.AcceptString("\n"));
  Q4T_CHECK(matcher.AcceptToken(0));
  return true;
}

Q4T_TEST(schema_language_number_exact_range_and_multiple) {
  std::fprintf(stderr, "numeric case: positive tenths\n");
  Q4T_CHECK(Language(
      SchemaNumberGrammar(Parse(R"({"minimum":0.1,"exclusiveMaximum":0.4,
                                    "multipleOf":0.1})"),
                          false),
      {"0.1", "0.10", "0.2", "0.3", "0.3000"},
      {"0", "0.4", "0.399", "0.30000000000000004", "-0.1"}));
  std::fprintf(stderr, "numeric case: signed quarters\n");
  Q4T_CHECK(Language(
      SchemaNumberGrammar(Parse(R"({"exclusiveMinimum":-1.25,"maximum":1.5,
                                    "multipleOf":0.25})"),
                          false),
      {"-1", "-0.25", "0", "0.25", "1.50"},
      {"-1.25", "-1.2501", "0.3", "1.50001", "2"}));
  std::fprintf(stderr, "numeric case: exact large integer\n");
  Q4T_CHECK(Language(SchemaNumberGrammar(Parse(R"({"minimum":9007199254740992,
                                    "maximum":9007199254740994,
                                    "multipleOf":3})"),
                                         true),
                     {"9007199254740993"},
                     {"9007199254740992", "9007199254740994"}));
  return true;
}

Q4T_TEST(schema_language_number_fractional_bounds_are_not_rounded) {
  Q4T_CHECK(
      Language(SchemaNumberGrammar(
                   Parse(R"({"minimum":0.0012,"maximum":0.0013})"), false),
               {"0.0012", "0.00120", "0.001201", "0.00129", "0.0013"},
               {"0", "0.001", "0.00119", "0.001301", "0.01"}));
  Q4T_CHECK(Language(SchemaNumberGrammar(Parse(R"({"exclusiveMinimum":-0.0013,
                                    "exclusiveMaximum":-0.0012})"),
                                         false),
                     {"-0.001201", "-0.00129"},
                     {"-0.0012", "-0.0013", "-0.001", "0", "0.00121"}));
  return true;
}

Q4T_TEST(schema_language_number_scaled_modulo_integer_oracle) {
  for (const auto& [step, scale] : std::vector<std::pair<std::string, int>>{
           {"3", 3}, {"30", 30}, {"0.3", 3}}) {
    const auto compiled = Compile(SchemaNumberGrammar(
        Parse("{\"multipleOf\":" + step + '}'), step != "0.3"));
    for (int value = -40; value <= 40; ++value) {
      const auto text = std::to_string(value);
      const bool expected =
          step == "0.3" ? (value * 10) % scale == 0 : value % scale == 0;
      Q4T_CHECK(Accepts(compiled, text) == expected);
    }
  }
  Q4T_CHECK(Language(SchemaNumberGrammar(Parse(R"({"multipleOf":300})"), false),
                     {"0", "-0", "300", "-600.00", "3000.0"},
                     {"3", "30", "330", "300.01", "0.3"}));
  return true;
}

Q4T_TEST(schema_language_number_integer_bounds_and_empty_range) {
  Q4T_CHECK(
      Language(SchemaNumberGrammar(
                   Parse(R"({"minimum":-1.5,"exclusiveMaximum":2})"), true),
               {"-1", "0", "1"}, {"-2", "2", "1.0", "-1.5"}));
  Q4T_CHECK(
      Language(SchemaNumberGrammar(Parse(R"({"minimum":1,"exclusiveMinimum":2,
                                    "maximum":3,"exclusiveMaximum":2})"),
                                   true),
               {}, {"0", "1", "2", "3", ""}));
  return true;
}

Q4T_TEST(schema_language_invalid_and_capacity_errors_are_distinct) {
  bool invalid = false, capacity = false;
  try {
    (void)SchemaRegexEbnf({"[broken"});
  } catch (const std::length_error&) {
    return false;
  } catch (const std::runtime_error&) {
    invalid = true;
  }
  try {
    (void)SchemaRegexEbnf({"a{1000000}"});
  } catch (const std::length_error&) {
    capacity = true;
  }
  Q4T_CHECK(invalid && capacity);
  return true;
}
