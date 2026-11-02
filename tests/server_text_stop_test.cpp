#include "q4t/server/text_stop.h"

#include <string>
#include <utility>
#include <vector>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/http_response.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using q4t::server::ChatResponseParser;
using q4t::server::ResponseDelta;
using q4t::server::TextStopStream;

// Independent whole-string oracle scans completion positions, not buffers.
std::pair<std::string, int> Oracle(const std::string& text,
                                   const std::vector<std::string>& stops) {
  for (size_t end = 1; end <= text.size(); ++end)
    for (size_t i = 0; i < stops.size(); ++i)
      if (end >= stops[i].size() &&
          text.substr(end - stops[i].size(), stops[i].size()) == stops[i])
        return {text.substr(0, end - stops[i].size()), static_cast<int>(i)};
  return {text, -1};
}

bool Contract(const std::string& wire, const std::string& expected = {}) {
  Json req;
  Q4T_CHECK(q4t::io::ParseJson(wire, &req, true));
  std::string param;
  const auto status =
      q4t::server::ValidateChatContract(req, "m", false, &param);
  Q4T_CHECK(status.ok() == expected.empty());
  Q4T_CHECK(param == expected);
  return true;
}
}  // namespace

Q4T_TEST(text_stop_input_contract) {
  for (const auto* value : {"null", "[]", R"("END")", R"(["a","中","a"])",
                            R"(["a","b","c","d"])", R"("\u0000")"})
    Q4T_CHECK(Contract(std::string(R"({"prompt":"q","stop":)") + value + '}'));
  for (const auto* value :
       {"true", "1", "{}", R"("")", R"(["a","b","c","d","e"])"})
    Q4T_CHECK(Contract(std::string(R"({"prompt":"q","stop":)") + value + '}',
                       "stop"));
  for (const auto* value :
       {R"(["a",null])", R"(["a",1])", R"(["a",""])", R"(["a",[]])"})
    Q4T_CHECK(Contract(std::string(R"({"prompt":"q","stop":)") + value + '}',
                       "stop[1]"));
  for (size_t count : {256, 257})
    Q4T_CHECK(Contract(
        R"({"prompt":"q","stop":")" + std::string(count, 'a') + R"("})",
        count == 256 ? "" : "stop"));
  // Bytes, not code points: 85 Chinese scalars fit, 86 do not.
  std::string chinese;
  for (int i = 0; i < 85; ++i) chinese += "中";
  Q4T_CHECK(Contract(R"({"prompt":"q","stop":")" + chinese + R"("})"));
  Q4T_CHECK(
      Contract(R"({"prompt":"q","stop":")" + chinese + "中" + R"("})", "stop"));
  return true;
}

Q4T_TEST(text_stop_combinations_fail_before_generation) {
  const std::string begin = R"({"messages":[{"role":"user","content":"q"}],)";
  for (const auto& extra : std::vector<std::string>{
           R"("response_format":{"type":"json_object"})",
           R"("tool_choice":"none")",
           R"("session_id":")" + std::string(64, 'a') + '"',
           R"("prefix_cache_scope":")" + std::string(64, 'a') + '"'}) {
    Q4T_CHECK(Contract(begin + extra + R"(,"stop":"x"})", "stop"));
    Q4T_CHECK(Contract(begin + extra + R"(,"stop":[]})"));
    Q4T_CHECK(Contract(begin + extra + R"(,"stop":null})"));
  }
  Q4T_CHECK(Contract(begin + R"("thinking_token_budget":0,"stop":"x"})"));
  Q4T_CHECK(Contract(begin + R"("parse_response":false,"stop":"x"})"));
  Q4T_CHECK(Contract(begin +
                     R"("tools":[{"type":"function","function":)"
                     R"({"name":"f"}}],"tool_choice":"auto","stop":"x"})"));
  return true;
}

Q4T_TEST(text_stop_invalid_utf8_and_clear_on_error) {
  Json req;
  Q4T_CHECK(q4t::io::ParseJson(R"({"stop":"x"})", &req));
  req.object[0].second.str = std::string("\xc0\xaf", 2);
  std::vector<std::string> stops{"stale"};
  std::string param;
  Q4T_CHECK(!q4t::server::ParseTextStop(req, false, &stops, &param));
  Q4T_CHECK(stops.empty() && param == "stop");
  return true;
}

Q4T_TEST(text_stop_all_chunk_partitions_against_oracle) {
  const std::vector<std::pair<std::string, std::vector<std::string>>> cases = {
      {"xabcZ", {"abc", "b"}},
      {"xabcZ", {"bc", "abc"}},
      {"xabcZ", {"abc", "bc"}},
      {"xaaaaaaZ", {"aaab", "aaa"}},
      {"xababacZ", {"ababa", "bac"}},
      {"ENDtail", {"END"}},
      {"xENxEN", {"END"}},
      {"prefix", {}},
      {"xabcZ", {"abc", "abc"}}};
  for (const auto& [text, stops] : cases) {
    const auto [wanted, index] = Oracle(text, stops);
    for (size_t cuts = 0; cuts < (size_t{1} << (text.size() - 1)); ++cuts) {
      TextStopStream stream(stops);
      std::string actual;
      size_t begin = 0;
      for (size_t end = 1; end <= text.size(); ++end) {
        if (end != text.size() && !(cuts & (size_t{1} << (end - 1)))) continue;
        actual +=
            stream.Append(std::string_view(text).substr(begin, end - begin));
        Q4T_CHECK(
            wanted.starts_with(actual));  // Every published prefix is safe.
        Q4T_CHECK(stream.BufferedBytes() <= 255);
        begin = end;
      }
      actual += stream.Finish();
      Q4T_CHECK(actual == wanted && stream.MatchedIndex() == index);
      Q4T_CHECK(stream.Append("late").empty() && stream.Finish().empty());
    }
  }
  return true;
}

Q4T_TEST(text_stop_utf8_every_byte_split_and_replacement) {
  for (const std::string& raw :
       {std::string("开始中文🛑尾巴"), std::string("开始\xf0\x9f", 8)}) {
    const std::vector<std::string> stops{"中文🛑", "�"};
    const auto [wanted, index] =
        Oracle(q4t::server::detail::NormalizeUtf8(raw), stops);
    for (size_t width = 1; width <= raw.size(); ++width) {
      TextStopStream stream(stops);
      q4t::server::detail::Utf8Stream utf8;
      std::string actual;
      for (size_t i = 0; i < raw.size(); i += width) {
        actual +=
            stream.Append(utf8.Append(std::string_view(raw).substr(i, width)));
        Q4T_CHECK(wanted.starts_with(actual));
      }
      actual += stream.Append(utf8.Finish());
      actual += stream.Finish();
      Q4T_CHECK(actual == wanted && stream.MatchedIndex() == index);
    }
  }
  return true;
}

Q4T_TEST(text_stop_reasoning_is_intentional_but_eos_is_not) {
  for (bool hidden : {false, true}) {
    Json req;
    Q4T_CHECK(q4t::io::ParseJson(
        std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                    R"("include_reasoning":)") +
            (hidden ? "false}" : "true}"),
        &req));
    for (bool stopped : {false, true}) {
      ChatResponseParser parser;
      Q4T_CHECK(
          parser.Init(req, "<|im_start|>assistant\n<think>\n", "id", true));
      TextStopStream stream({"END"});
      std::vector<ResponseDelta> deltas;
      Q4T_CHECK(parser.Append(stream.Append("thoughtEN"), &deltas));
      Q4T_CHECK(parser.Append(stream.Append("Danswer-tail"), &deltas));
      Q4T_CHECK(stream.Matched());
      const auto status = parser.Finish(false, &deltas, stopped);
      Q4T_CHECK(status.ok() == stopped);
      if (stopped) {
        Q4T_CHECK(parser.MessageJson().find("answer-tail") ==
                  std::string::npos);
        Q4T_CHECK((parser.MessageJson().find("thought") != std::string::npos) ==
                  !hidden);
      }
    }
  }
  return true;
}

Q4T_TEST(text_stop_incomplete_tool_never_succeeds) {
  Json req;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"messages":[{"role":"user","content":"q"}],"tools":[)"
      R"({"type":"function","function":{"name":"f","parameters":)"
      R"({"type":"object","properties":{"x":{"type":"string"}}}}}]})",
      &req));
  const std::string text =
      "<tool_call><function=f><parameter=x>hello"
      "</parameter></function></tool_call>tail";
  for (const char* stop : {"hello", "</parameter>", "</tool_call>"}) {
    for (bool collect : {false, true}) {
      TextStopStream stream({stop});
      ChatResponseParser parser;
      Q4T_CHECK(parser.Init(req,
                            "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                            "id", collect));
      std::vector<ResponseDelta> deltas;
      for (char c : text)
        Q4T_CHECK(parser.Append(stream.Append(std::string(1, c)), &deltas));
      Q4T_CHECK(stream.Matched() && !parser.HasToolCalls());
      Q4T_CHECK(!parser.Finish(false, &deltas, true));
      for (const auto& delta : deltas)
        Q4T_CHECK(delta.kind != ResponseDelta::Kind::kTool);
    }
  }
  return true;
}

Q4T_TEST(text_stop_unmatched_prefix_flush_and_memory_bound) {
  TextStopStream stream({std::string(255, 'a') + 'Z'});
  std::string output;
  for (int i = 0; i < 8192; ++i) {
    output += stream.Append("a");
    Q4T_CHECK(stream.BufferedBytes() <= 255);
  }
  Q4T_CHECK(!stream.Matched() && output.size() == 8192 - 255);
  output += stream.Finish();
  Q4T_CHECK(output == std::string(8192, 'a'));
  return true;
}
