#include "../src/server/schema_string_grammar.h"

#include <filesystem>
#include <fstream>
#include <stdexcept>

#include <xgrammar/compiler.h>
#include <xgrammar/matcher.h>

#include "../src/server/schema_string.h"
#include "q4t/server/custom_tools.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Parse(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
std::vector<std::string> Vocabulary() {
  std::vector<std::string> result(257);
  for (int c = 0; c < 256; ++c) result[c + 1] = char(c);
  return result;
}
bool Corpus(const std::string& format) {
  const auto schema =
      Parse("{\"type\":\"string\",\"format\":\"" + format + "\"}");
  const auto language = CompileSchemaStringSyntax({&schema});
  xgrammar::TokenizerInfo tokenizer(Vocabulary(), xgrammar::VocabType::RAW, 257,
                                    std::vector<int32_t>{0}, false);
  xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
  const auto grammar =
      compiler.CompileGrammar(xgrammar::Grammar::FromEBNF(language));
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/schema_formats" / (format + ".json");
  std::ifstream file(path);
  Q4T_CHECK(file.good());
  const auto corpus =
      Parse(std::string(std::istreambuf_iterator<char>(file), {}));
  size_t checked = 0;
  for (const auto& group : corpus.array) {
    for (const auto& test : group.GetArray("tests")->array) {
      const auto* data = test.Find("data");
      if (!data->IsString()) continue;
      ++checked;
      xgrammar::GrammarMatcher matcher(grammar);
      const bool syntax = matcher.AcceptString(EncodeCustomInput(data->str)) &&
                          matcher.AcceptToken(0) && matcher.IsTerminated();
      // Syntax can be broader for native IDNA/timezone constraints, but every
      // valid value must remain generatable and combined checks must agree.
      const bool accepted = syntax && SchemaFormatValid(format, data->str);
      if (accepted != test.GetBool("valid")) {
        std::fprintf(stderr, "%s: %s (syntax=%d)\n", format.c_str(),
                     test.GetString("description").c_str(), syntax);
        return false;
      }
    }
  }
  Q4T_CHECK(checked > 10);
  return true;
}
}  // namespace

Q4T_TEST(schema_string_syntax_date_and_datetime_external_corpus) {
  return Corpus("date") && Corpus("date-time");
}
Q4T_TEST(schema_string_syntax_time_and_duration_external_corpus) {
  return Corpus("time") && Corpus("duration");
}
Q4T_TEST(schema_string_syntax_ip_external_corpus) {
  return Corpus("ipv4") && Corpus("ipv6");
}
Q4T_TEST(schema_string_syntax_hostname_email_uuid_external_corpus) {
  return Corpus("hostname") && Corpus("email") && Corpus("uuid");
}
Q4T_TEST(schema_string_syntax_intersects_formats_patterns_and_lengths) {
  const auto one =
      Parse(R"({"type":"string","format":"date","pattern":"^2024-"})");
  const auto two = Parse(
      R"({"type":"string","minLength":10,"maxLength":10,"pattern":"-02-"})");
  const auto language = CompileSchemaStringSyntax({&one, &two});
  xgrammar::TokenizerInfo tokenizer(Vocabulary(), xgrammar::VocabType::RAW, 257,
                                    std::vector<int32_t>{0}, false);
  xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
  const auto grammar =
      compiler.CompileGrammar(xgrammar::Grammar::FromEBNF(language));
  for (const auto& [value, valid] :
       std::vector<std::pair<std::string, bool>>{{"2024-02-29", true},
                                                 {"2023-02-28", false},
                                                 {"2024-03-01", false},
                                                 {"2024-02-30", false},
                                                 {"2024-02-2", false}}) {
    xgrammar::GrammarMatcher matcher(grammar);
    const bool accepted = matcher.AcceptString(EncodeCustomInput(value)) &&
                          matcher.AcceptToken(0) && matcher.IsTerminated();
    Q4T_CHECK(accepted == valid);
  }
  return true;
}
