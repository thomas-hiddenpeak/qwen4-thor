#include "../src/server/schema_token_guard.h"

#include <cstring>
#include <stdexcept>

#include "q4t/server/output_schema.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Parse(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true,
                                         {64, 65536, 1024 * 1024, true, true});
  if (!status) throw std::runtime_error(status.message());
  return value;
}
std::string Object(const std::string& child) {
  return R"({"type":"object","properties":{"v":)" + child +
         R"(},"required":["v"],"additionalProperties":false})";
}
Json Content(const std::string& schema) {
  return Parse(R"({"content":{"schema":)" + schema + "}}");
}
bool Allowed(std::span<const int32_t> mask, size_t index) {
  return (static_cast<uint32_t>(mask[index / 32]) >> (index % 32)) & 1;
}
uint16_t Bf16(float value) {
  uint32_t bits;
  std::memcpy(&bits, &value, sizeof(bits));
  return static_cast<uint16_t>(bits >> 16);
}
OutputConstraints WorkerPlan(const std::string& schema) {
  OutputConstraints plan;
  plan.active = plan.json_output = true;
  plan.structural_tag =
      R"({"type":"structural_tag","format":{"type":"q4t_schema","schema":)" +
      schema + R"(},"q4t_guard":)" + OutputJson(Content(schema)) + '}';
  return plan;
}
}  // namespace

Q4T_TEST(schema_guard_filters_completed_format_tokens_and_receipts) {
  const auto schema = Object(R"({"type":"string","format":"time"})");
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(Content(schema)));
  Q4T_CHECK(guard.Accept(R"({"v":"23:59:60)", false));
  const std::vector<std::string> vocabulary{
      "", "Z\"}", "+01:00\"}", "-00:00\"}", "Z", "Z\"}trailing"};
  std::vector<int32_t> mask(1, -1);
  const std::vector<int32_t> stops{0};
  Q4T_CHECK(guard.Filter(vocabulary, stops, mask));
  Q4T_CHECK(!Allowed(mask, 0) && Allowed(mask, 1) && !Allowed(mask, 2));
  Q4T_CHECK(Allowed(mask, 3) && Allowed(mask, 4) && !Allowed(mask, 5));
  Q4T_CHECK(!guard.Accept(vocabulary[2], false));
  Q4T_CHECK(guard.Accept(vocabulary[1], false));
  Q4T_CHECK(guard.Accept("", true));
  SchemaReceipt expected;
  Q4T_CHECK(MakeSchemaReceipt("", Parse(schema), Parse(R"({"v":"23:59:60Z"})"),
                              &expected));
  Q4T_CHECK(guard.Receipts() == std::vector<SchemaReceipt>{expected});
  return true;
}

Q4T_TEST(schema_guard_thinking_tools_and_payload_marker_boundaries) {
  const auto schema = Object(R"({"type":"string"})");
  const auto config = Parse(
      R"({"thinking":true,"tools":[{"name":"read",)"
      R"("begin":"<tool_call>\n<function=read>\n<parameter=arguments>\n",)"
      R"("end":"\n</parameter>\n</function>\n</tool_call>","schema":)" +
      schema + "}]}");
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(config));
  const std::string stream =
      "thought <tool_call> bad JSON </think>\nBefore\n"
      "<tool_call>\n<function=read>\n<parameter=arguments>\n"
      "{\"v\":\"\\u003c/tool_call>中😀\"}\n</parameter>\n</function>\n</"
      "tool_call>\n"
      "<tool_call>\n<function=read>\n<parameter=arguments>\n"
      "{\"v\":\"second\"}\n</parameter>\n</function>\n</tool_call>";
  for (char c : stream) Q4T_CHECK(guard.Accept(std::string(1, c), false));
  Q4T_CHECK(guard.Accept("", true));
  Q4T_CHECK(guard.Receipts().size() == 2);
  SchemaReceipt expected;
  Q4T_CHECK(MakeSchemaReceipt("read", Parse(schema),
                              Parse(R"({"v":"</tool_call>中😀"})"), &expected));
  Q4T_CHECK(guard.Receipts()[0] == expected);
  return true;
}

Q4T_TEST(schema_guard_anyof_large_exact_numbers_and_scope_identity) {
  const auto schema =
      Object(R"({"anyOf":[{"type":"number","enum":[1e400,1e-400]},
    {"type":"string","format":"ipv4"}]})");
  for (const auto& value :
       {R"({"v":1e400})", R"({"v":1e-400})", R"({"v":"192.0.2.1"})"}) {
    SchemaTokenGuard guard;
    Q4T_CHECK(guard.Init(Content(schema)));
    Q4T_CHECK(guard.Accept(value, false) && guard.Accept("", true));
    Q4T_CHECK(guard.Receipts().size() == 1);
  }
  for (const auto& value : {R"({"v":1e399})", R"({"v":"192.0.2.999"})"}) {
    SchemaTokenGuard guard;
    Q4T_CHECK(guard.Init(Content(schema)));
    Q4T_CHECK(!guard.Accept(value, false));
  }
  SchemaReceipt first, reordered, wrong_scope;
  const auto value = Parse(R"({"z":1e400,"x":0.30})");
  Q4T_CHECK(MakeSchemaReceipt("read", Parse(schema), value, &first));
  Q4T_CHECK(MakeSchemaReceipt("read", Parse(schema),
                              Parse(R"({"x":3e-1,"z":10e399})"), &reordered));
  Q4T_CHECK(MakeSchemaReceipt("write", Parse(schema), value, &wrong_scope));
  Q4T_CHECK(first == reordered && first != wrong_scope);
  return true;
}

Q4T_TEST(schema_guard_dependency_error_is_not_an_invalid_candidate) {
  SchemaTokenGuard guard;
  Q4T_CHECK(
      guard.Init(Content(Object(R"({"type":"string","format":"hostname"})")),
                 [](const Json&, std::string_view, size_t*, bool*) {
                   return q4t::Status::Fail(
                       "schema_dependency_unavailable: injected IDNA");
                 }));
  Q4T_CHECK(guard.Accept(R"({"v":"xn--bcher-kva)", false));
  const std::vector<std::string> vocabulary{"\"}"};
  std::vector<int32_t> mask(1, -1);
  const auto status = guard.Filter(vocabulary, {}, mask);
  Q4T_CHECK(!status &&
            status.message().starts_with("schema_dependency_unavailable:"));
  Q4T_CHECK(Allowed(mask, 0) && guard.Receipts().empty());
  return true;
}

Q4T_TEST(schema_guard_large_vocabulary_skips_uncompleted_string_checks) {
  SchemaTokenGuard guard;
  Q4T_CHECK(guard.Init(Content(Object(R"({"type":"string"})"))));
  Q4T_CHECK(guard.Accept(R"({"v":")" + std::string(4096, 'x'), false));
  const std::vector<std::string> vocabulary(300000, "中");
  std::vector<int32_t> mask((vocabulary.size() + 31) / 32, -1);
  Q4T_CHECK(guard.Filter(vocabulary, {}, mask));
  for (size_t i = 0; i < vocabulary.size(); ++i) Q4T_CHECK(Allowed(mask, i));
  Q4T_CHECK(guard.Accept("\"}", false) && guard.Accept("", true));
  return true;
}

Q4T_TEST(schema_worker_masks_native_semantics_and_returns_value_receipt) {
  const auto schema = Object(R"({"type":"string","format":"time"})");
  const std::vector<std::string> vocabulary{"", R"({"v":"23:59:60Z"})",
                                            R"({"v":"12:00:60Z"})",
                                            R"({"v":"00:59:60+01:00"})"};
  std::unique_ptr<StructuredDecoder> decoder;
  const auto created = StructuredDecoder::Create(WorkerPlan(schema), vocabulary,
                                                 4, {0}, &decoder);
  if (!created) std::fprintf(stderr, "%s\n", created.message().c_str());
  Q4T_CHECK(created);
  const std::vector<uint16_t> logits(4, Bf16(1));
  int32_t token = -1;
  Q4T_CHECK(!decoder->Select(logits, &token, 2));
  Q4T_CHECK(decoder->Select(logits, &token, 3) && token == 3);
  Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
  SchemaReceipt expected;
  Q4T_CHECK(
      MakeSchemaReceipt("", Parse(schema), Parse(vocabulary[3]), &expected));
  Q4T_CHECK(decoder->Receipts() == std::vector<SchemaReceipt>{expected});
  return true;
}

Q4T_TEST(schema_worker_idna_and_exact_number_constraints_use_same_path) {
  const auto schema = Object(R"({"anyOf":[{"type":"string","format":"hostname"},
    {"type":"number","enum":[1e400,1e-400]}]})");
  const std::vector<std::string> vocabulary{"",
                                            R"({"v":"xn--bcher-kva"})",
                                            R"({"v":"xn--a"})",
                                            R"({"v":1e400})",
                                            R"({"v":1e-400})",
                                            R"({"v":1e399})"};
  for (int selected : {1, 3, 4}) {
    std::unique_ptr<StructuredDecoder> decoder;
    const auto created = StructuredDecoder::Create(
        WorkerPlan(schema), vocabulary, 6, {0}, &decoder);
    if (!created) std::fprintf(stderr, "%s\n", created.message().c_str());
    Q4T_CHECK(created);
    const std::vector<uint16_t> logits(6, Bf16(1));
    int32_t token = -1;
    Q4T_CHECK(!decoder->Select(logits, &token, 2));
    Q4T_CHECK(!decoder->Select(logits, &token, 5));
    Q4T_CHECK(decoder->Select(logits, &token, selected));
    Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
    SchemaReceipt expected;
    Q4T_CHECK(MakeSchemaReceipt("", Parse(schema), Parse(vocabulary[selected]),
                                &expected));
    Q4T_CHECK(decoder->Receipts() == std::vector<SchemaReceipt>{expected});
  }
  return true;
}

Q4T_TEST(schema_worker_binding_mismatch_cannot_bypass_semantic_checks) {
  const auto schema = Object(R"({"type":"string","format":"time"})");
  auto plan = WorkerPlan(schema);
  auto document = Parse(plan.structural_tag);
  auto wrong = Content(Object(R"({"type":"string"})"));
  for (auto& [key, value] : document.object)
    if (key == "q4t_guard") value = wrong;
  plan.structural_tag = OutputJson(document);
  const std::vector<std::string> vocabulary{"", R"({"v":"12:00:60Z"})"};
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(!StructuredDecoder::Create(plan, vocabulary, 2, {0}, &decoder));
  Q4T_CHECK(!decoder);
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(schema_worker_dependency_and_resource_errors_recover_per_request) {
  const auto schema = Object(R"({"type":"string","format":"hostname"})");
  const std::vector<std::string> vocabulary{"", R"({"v":"xn--bcher-kva"})"};
  for (const auto& [fault, expected] :
       std::vector<std::pair<std::string, GrammarFailure>>{
           {"dependency", GrammarFailure::kDependency},
           {"resource", GrammarFailure::kResource}}) {
    auto plan = WorkerPlan(schema);
    plan.structural_tag.pop_back();
    plan.structural_tag += ",\"q4t_test_schema_failure\":\"" + fault + "\"}";
    std::unique_ptr<StructuredDecoder> decoder;
    GrammarFailure failure = GrammarFailure::kNone;
    Q4T_CHECK(!StructuredDecoder::Create(plan, vocabulary, 2, {0}, &decoder, {},
                                         &failure));
    Q4T_CHECK(failure == expected && !decoder);
  }
  // A new request creates a fresh bounded checker. No model/controller
  // process is restarted to recover validation capacity or dependencies.
  std::unique_ptr<StructuredDecoder> decoder;
  Q4T_CHECK(StructuredDecoder::Create(WorkerPlan(schema), vocabulary, 2, {0},
                                      &decoder));
  std::vector<uint16_t> logits(2, Bf16(1));
  int32_t token = -1;
  Q4T_CHECK(decoder->Select(logits, &token, 1));
  Q4T_CHECK(decoder->Select(logits, &token, 0) && decoder->Terminated());
  Q4T_CHECK(decoder->Receipts().size() == 1);
  return true;
}
#endif
