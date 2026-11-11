#include "q4t/server/embeddings_execution.h"

#include <bit>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/batch_execution.h"
#include "q4t/server/batch_input.h"
#include "q4t/server/batch_usage.h"
#include "q4t/server/embedding_metadata.h"
#include "q4t/server/finite_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using q4t::io::Json;
using q4t::io::ParseJson;
Json Read(const std::string& text) {
  Json value;
  if (!ParseJson(text, &value, true, kEmbeddingInputJsonLimits))
    throw std::runtime_error("embedding fixture JSON");
  return value;
}
ChatPlanConfig Config() {
  ChatPlanConfig result;
  result.model = "actual-qwen";
  result.model_aliases = {"alias"};
  result.token_vocab = 256;
  result.embedding_dimensions = 4;
  return result;
}
EmbeddingsRequestPlan Plan(const Json& input) {
  EmbeddingsRequestPlan plan;
  ChatPlanError error;
  if (!PlanEmbeddingsRequest(input, Config(), &plan, &error))
    throw std::runtime_error("embedding fixture plan: " + error.param);
  return plan;
}
Json Request(Json input) {
  return Object({{"model", String("alias")}, {"input", std::move(input)}});
}
GatewayClientPolicy Policy(uint64_t tpm = 100) {
  GatewayClientPolicy policy;
  policy.version = 3;
  policy.gateway_token = std::string(64, 'b');
  policy.sha256 = std::string(64, 'a');
  ClientLimits limits;
  limits.id = "alpha";
  limits.max_inflight = 2;
  limits.max_control_inflight = 2;
  limits.max_prompt_tokens = 8192;
  limits.max_output_tokens = 1;
  limits.max_body_bytes = 1 << 20;
  limits.requests_per_minute = 1;
  limits.tokens_per_minute = tpm;
  policy.clients.push_back(limits);
  return policy;
}
const std::vector<uint16_t> kHidden{0x4040, 0x4080, 0, 0};  // [3,4,0,0]
void Save(const std::string& name, const std::string& value) {
  const char* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!root) throw std::runtime_error("Missing fixture output directory");
  std::filesystem::create_directories(root);
  std::ofstream output(std::filesystem::path(root) / name);
  output << value;
  if (!output) throw std::runtime_error("Fixture output failed");
}
}  // namespace

Q4T_TEST(embeddings_input_families_defaults_and_served_alias) {
  for (const auto* input :
       {R"("raw text")", R"(["a","b"])", R"([1,2,3])", R"([[1,2],[3]])"}) {
    const auto request = Request(Read(input));
    const auto before = OutputJson(request);
    const auto plan = Plan(request);
    Q4T_CHECK(plan.model == "actual-qwen" && plan.dimensions == 4 &&
              plan.hidden_size == 4 && !plan.base64);
    Q4T_CHECK(OutputJson(request) == before);
  }
  const auto plan = Plan(Read(
      R"({"model":"alias","input":"x","dimensions":2,"encoding_format":"base64","user":"end-user"})"));
  Q4T_CHECK(plan.base64 && plan.dimensions == 2 && plan.inputs[0].text == "x");
  return true;
}

Q4T_TEST(embeddings_reject_invalid_ids_types_and_unserved_models) {
  for (const auto* input : {R"("")", "[]", "null", "true", "[1,-1]", "[1,256]",
                            "[1,2.5]", "[1,2.0000000000000001]", "[1,1e300]",
                            R"(["x",[]])", "[[1],2]", "[[],[1]]", "[true,1]"}) {
    EmbeddingsRequestPlan plan;
    ChatPlanError error;
    Q4T_CHECK(
        !PlanEmbeddingsRequest(Request(Read(input)), Config(), &plan, &error));
    Q4T_CHECK(plan.inputs.empty() && error.http_status == 400);
  }
  for (const auto* raw :
       {R"({"input":"x"})", R"({"model":null,"input":"x"})",
        R"({"model":"alias","input":"x","dimensions":0})",
        R"({"model":"alias","input":"x","dimensions":5})",
        R"({"model":"alias","input":"x","dimensions":null})",
        R"({"model":"alias","input":"x","encoding_format":null})",
        R"({"model":"alias","input":"x","user":4})",
        R"({"model":"alias","input":"x","stream":false})"}) {
    EmbeddingsRequestPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!PlanEmbeddingsRequest(Read(raw), Config(), &plan, &error));
  }
  EmbeddingsRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanEmbeddingsRequest(
      Read(R"({"model":"text-embedding-3-small","input":"x"})"), Config(),
      &plan, &error));
  Q4T_CHECK(error.http_status == 404 && error.code == "model_not_found");
  return true;
}

Q4T_TEST(embeddings_tokenization_is_raw_ordered_and_atomic) {
  const auto plan = Plan(Request(Read(R"(["first","second"])")));
  size_t called = 0;
  std::vector<std::vector<int32_t>> inputs;
  uint64_t total = 0;
  ChatPlanError error;
  Q4T_CHECK(PrepareEmbeddingInputs(
      plan, Config(),
      [&](std::string_view text, auto* out) {
        if (text != (called++ ? "second" : "first"))
          return q4t::Status::Fail("Template changed raw input");
        *out = called == 1 ? std::vector<uint32_t>{3, 4}
                           : std::vector<uint32_t>{9};
        return q4t::Status{};
      },
      &inputs, &total, &error));
  Q4T_CHECK(called == 2 && total == 3 &&
            inputs[0] == std::vector<int32_t>({3, 4}) &&
            inputs[1] == std::vector<int32_t>({9}));
  auto config = Config();
  config.capacities = {1};
  Q4T_CHECK(!PrepareEmbeddingInputs(
      plan, config,
      [](auto, auto* out) {
        *out = std::vector<uint32_t>{3, 4};
        return q4t::Status{};
      },
      &inputs, &total, &error));
  Q4T_CHECK(inputs.empty() && !total &&
            error.code == "context_length_exceeded");
  const auto ids = Plan(Request(Read("[[3,4],[9]]")));
  Q4T_CHECK(PrepareEmbeddingInputs(
      ids, Config(),
      [](auto, auto*) {
        return q4t::Status::Fail("Raw token IDs were decoded/re-encoded");
      },
      &inputs, &total, &error));
  Q4T_CHECK(total == 3 && inputs.size() == 2);
  ClientLimits limits;
  limits.max_prompt_tokens = 2;
  config = Config();
  config.client_limits = &limits;
  Q4T_CHECK(!PrepareEmbeddingInputs(
      ids, config, [](auto, auto*) { return q4t::Status{}; }, &inputs, &total,
      &error));
  Q4T_CHECK(inputs.empty() && !total &&
            error.code == "client_prompt_limit_exceeded");
  return true;
}

Q4T_TEST(embeddings_input_capacity_300000_ids_and_batch_identity) {
  auto input = Array();
  for (size_t i = 0; i < 37; ++i) {
    auto row = Array();
    row.array.assign(i == 36 ? 5088 : 8192, Number(1));
    input.array.push_back(std::move(row));
  }
  auto request = Request(std::move(input));
  const auto wire = OutputJson(request);
  Json parsed;
  Q4T_CHECK(!ParseJson(wire, &parsed, true, kApplicationJsonLimits));
  Q4T_CHECK(ParseJson(wire, &parsed, true, kEmbeddingInputJsonLimits));
  const auto plan = Plan(parsed);
  std::vector<std::vector<int32_t>> inputs;
  uint64_t total = 0;
  ChatPlanError error;
  Q4T_CHECK(PrepareEmbeddingInputs(
      plan, Config(), [](auto, auto*) { return q4t::Status{}; }, &inputs,
      &total, &error));
  Q4T_CHECK(total == 300000);
  std::string hash;
  Q4T_CHECK(!InputValueIdentity(parsed, &hash));
  Q4T_CHECK(InputValueIdentity(parsed, &hash, 400000) && hash.size() == 64);
  size_t staged = 0;
  BatchInput batch(
      "/v1/embeddings",
      [&](const auto& row, auto* out) {
        if (OutputJson(row.body) != wire)
          return Error(500, "input_changed", "input");
        out->canonical_model = "actual-qwen";
        out->embedding_inputs = 37;
        return ObjectReply{};
      },
      [&](auto&&) {
        ++staged;
        return ObjectReply{};
      });
  const auto line = OutputJson(Object({{"custom_id", String("large")},
                                       {"method", String("POST")},
                                       {"url", String("/v1/embeddings")},
                                       {"body", parsed}})) +
                    '\n';
  Q4T_CHECK(batch.Feed(line).ok() && batch.Finish().ok() && staged == 1);
  Field(request, "input").array.back().array.push_back(Number(1));
  EmbeddingsRequestPlan invalid;
  Q4T_CHECK(!PlanEmbeddingsRequest(request, Config(), &invalid, &error));
  auto many = Array();
  many.array.assign(2049, String("x"));
  Q4T_CHECK(!PlanEmbeddingsRequest(Request(std::move(many)), Config(), &invalid,
                                   &error));
  return true;
}

Q4T_TEST(embeddings_actual_bf16_normalization_and_finite_failures) {
  std::vector<float> values;
  Q4T_CHECK(NormalizeEmbeddingHidden(kHidden, 4, &values));
  Q4T_CHECK(values == std::vector<float>({0.6f, 0.8f, 0, 0}));
  Q4T_CHECK(NormalizeEmbeddingHidden(kHidden, 1, &values) && values[0] == 1);
  Q4T_CHECK(!NormalizeEmbeddingHidden(kHidden, 5, &values) && values.empty());
  Q4T_CHECK(!NormalizeEmbeddingHidden(std::vector<uint16_t>(4), 4, &values));
  auto invalid = kHidden;
  invalid.back() = 0x7f80;
  Q4T_CHECK(!NormalizeEmbeddingHidden(invalid, 1, &values));
  invalid.back() = 0x7fc0;
  Q4T_CHECK(!NormalizeEmbeddingHidden(invalid, 4, &values));
  Q4T_CHECK(NormalizeEmbeddingHidden(std::vector<uint16_t>{1}, 1, &values) &&
            values[0] == 1.0f);
  return true;
}

Q4T_TEST(embeddings_fp32_wire_roundtrip_and_base64_byte_order) {
  auto plan = Plan(Request(Read(R"(["first","second"])")));
  std::vector<std::vector<float>> vectors{
      {0.6f, 0.8f, 0, 0},
      {std::numeric_limits<float>::denorm_min(), -0.0f, -0.123456789f, 1.0f}};
  std::string body;
  Q4T_CHECK(BuildEmbeddingsResponse(plan, vectors, 3, &body));
  Q4T_CHECK(body.find("-0.0") != std::string::npos);
  const auto value = Read(body);
  for (size_t i = 0; i < 2; ++i)
    for (size_t j = 0; j < 4; ++j) {
      const auto actual = static_cast<float>(
          value.Find("data")->array[i].Find("embedding")->array[j].number);
      Q4T_CHECK(std::bit_cast<uint32_t>(actual) ==
                std::bit_cast<uint32_t>(vectors[i][j]));
    }
  Q4T_CHECK(value.Find("usage")->GetInt("prompt_tokens") == 3 &&
            value.Find("usage")->GetInt("total_tokens") == 3);
  Save("embeddings-float.json", body);
  plan.base64 = true;
  Q4T_CHECK(BuildEmbeddingsResponse(plan, vectors, 3, &body));
  Save("embeddings-base64.json", body);
  plan.inputs.resize(1);
  plan.dimensions = 1;
  Q4T_CHECK(BuildEmbeddingsResponse(plan, std::vector<std::vector<float>>{{1}},
                                    1, &body));
  Q4T_CHECK(Read(body).Find("data")->array[0].GetString("embedding") ==
            "AACAPw==");
  Q4T_CHECK(!BuildEmbeddingsResponse(plan, vectors, 3, &body) && body.empty());
  return true;
}

Q4T_TEST(embeddings_max_vectors_metadata_and_finite_capture) {
  auto config = Config();
  config.embedding_dimensions = 2560;
  auto input = Array();
  input.array.assign(2048, String("one"));
  EmbeddingsRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanEmbeddingsRequest(Request(input), config, &plan, &error));
  std::vector<std::vector<float>> vectors(2048, std::vector<float>(2560));
  for (auto& row : vectors) row[0] = 1;
  std::string body;
  Q4T_CHECK(BuildEmbeddingsResponse(plan, vectors, 2048, &body));
  Json metadata;
  Q4T_CHECK(ParseEmbeddingMetadata(body, &metadata));
  Q4T_CHECK(metadata.Find("data")->IsNull() &&
            metadata.Find("usage")->GetInt("total_tokens") == 2048);
  FiniteResponse generic;
  Q4T_CHECK(!generic.Record(200, "application/json", body));
  FiniteResponse native(kChatBodyBytes, FiniteResponseFormat::kEmbeddings);
  Q4T_CHECK(native.Record(200, "application/json", body) && native.Complete() &&
            native.Body() == body);
  const auto usage = BatchResponseUsage("/v1/embeddings", metadata);
  Q4T_CHECK(usage && usage->input == 2048 && usage->output == 0);
  ClientAdmission clients;
  Q4T_CHECK(clients.Configure(Policy()));
  const auto identity = clients.Authenticate("alpha", std::string(64, 'b'));
  ClientLease permit;
  Q4T_CHECK(permit.Acquire(clients, identity, ClientLane::kData));
  const auto control = std::make_shared<RequestControl>(
      "req_tool_run_embedding", "",
      ClientClock::now() + std::chrono::seconds(10), "alpha");
  const auto captured = ExecuteBatchEndpoint(
      control->id(), identity, permit, control,
      [&] { detail::SendSimple(-1, 200, "OK", body, "application/json"); }, {},
      "/v1/embeddings");
  Q4T_CHECK(captured.error.ok() && captured.http_status == 200 &&
            captured.response_body == body);
  return true;
}

Q4T_TEST(embeddings_projection_checks_discarded_syntax_and_wrapper_usage) {
  Json metadata;
  for (const auto* bad :
       {R"({"data":[1,]})", R"({"data":[{"x":1,"x":2}]})", R"({"data":[01]})",
        R"({"data":["\ud800"]})", R"({"data":["a\q"]})",
        R"({"data":[]})"
        " trailing"})
    Q4T_CHECK(!ParseEmbeddingMetadata(bad, &metadata));
  std::string compact;
  const std::string original =
      "{ \"custom_id\":\"a \\\" b\", "
      "\"response\":{\"body\":{\"data\":[1.234567890123456789,2],\n"
      "\"usage\":{\"prompt_tokens\":2,\"total_tokens\":2}}}}\n";
  Q4T_CHECK(ParseEmbeddingMetadata(original, &metadata, true, &compact));
  Q4T_CHECK(metadata.GetString("custom_id") == "a \" b" &&
            metadata.Find("response")->Find("body")->Find("data")->IsNull());
  Q4T_CHECK(compact.find("1.234567890123456789") != compact.npos &&
            compact.find('\n') == compact.npos);
  Q4T_CHECK(Read(compact)
                .Find("response")
                ->Find("body")
                ->Find("data")
                ->array.size() == 2);
  std::string huge = R"({"unexpected":[)";
  for (size_t i = 0; i < 65536; ++i) huge += i ? ",1" : "1";
  huge += "]}";
  Q4T_CHECK(!ParseEmbeddingMetadata(huge, &metadata));
  return true;
}

Q4T_TEST(embeddings_execution_one_rpm_actual_usage_and_batch_precharge) {
  for (const bool batch : {false, true}) {
    ClientAdmission clients;
    Q4T_CHECK(clients.Configure(Policy()));
    auto identity = clients.Authenticate("alpha", std::string(64, 'b'));
    ClientRateLease rate(batch ? ClientRateDomain::kBatch
                               : ClientRateDomain::kOnline);
    if (batch)
      Q4T_CHECK(rate.PrechargeBatch(clients, identity, 5, 0) ==
                ClientRateResult::kAllowed);
    const auto plan = Plan(Request(Read("[[1,2,3],[4,5]]")));
    ActualUsageCounter usage;
    usage.Enable();
    size_t calls = 0;
    std::string body;
    auto reply = ExecuteEmbeddingInputs(
        plan, {{1, 2, 3}, {4, 5}}, clients, identity, rate, usage,
        [&](const auto& input, auto* out) {
          ++calls;
          *out = {kHidden, input.size()};
          return ObjectReply{};
        },
        [] { return false; }, &body);
    Q4T_CHECK(reply.ok() && calls == 2 && usage.Total()->input == 5 &&
              usage.Total()->output == 0 && !body.empty());
    const auto counted = clients.RateUsage(
        0, ClientClock::now(),
        batch ? ClientRateDomain::kBatch : ClientRateDomain::kOnline);
    Q4T_CHECK(counted.requests == 1 && counted.settled_tokens == 5 &&
              !counted.reserved);
  }
  return true;
}

Q4T_TEST(
    embeddings_execution_cancel_failure_and_exception_count_committed_only) {
  for (int failure = 0; failure < 3; ++failure) {
    ClientAdmission clients;
    Q4T_CHECK(clients.Configure(Policy()));
    auto identity = clients.Authenticate("alpha", std::string(64, 'b'));
    ClientRateLease rate;
    const auto plan = Plan(Request(Read("[[1,2,3],[4,5]]")));
    ActualUsageCounter usage;
    usage.Enable();
    std::string body;
    size_t calls = 0;
    bool cancelled = false, threw = false;
    ObjectReply reply;
    try {
      reply = ExecuteEmbeddingInputs(
          plan, {{1, 2, 3}, {4, 5}}, clients, identity, rate, usage,
          [&](const auto& input, auto* out) {
            ++calls;
            *out = {kHidden, input.size()};
            if (calls == 1) return ObjectReply{};
            out->committed_input = 1;
            if (failure == 2) throw std::runtime_error("actual executor fault");
            if (failure == 1) cancelled = true;
            return Error(503, "step_failed",
                         "step failed after partial commit");
          },
          [&] { return cancelled; }, &body);
    } catch (const std::runtime_error&) {
      threw = true;
    }
    Q4T_CHECK(calls == 2 && body.empty() &&
              (failure == 2 ? threw : !reply.ok()));
    Q4T_CHECK(usage.Total()->input == 4 && usage.Total()->output == 0);
    const auto counted = clients.RateUsage(0);
    Q4T_CHECK(counted.requests == 1 && counted.settled_tokens == 4 &&
              !counted.reserved);
  }
  return true;
}

Q4T_TEST(embeddings_quota_and_preexecution_cancel_do_not_run_model) {
  for (bool cancelled : {false, true}) {
    ClientAdmission clients;
    Q4T_CHECK(clients.Configure(Policy(2)));
    auto identity = clients.Authenticate("alpha", std::string(64, 'b'));
    ClientRateLease rate;
    const auto plan = Plan(Request(Read("[1,2,3]")));
    ActualUsageCounter usage;
    usage.Enable();
    std::string body;
    size_t calls = 0;
    const auto reply = ExecuteEmbeddingInputs(
        plan, {{1, 2, 3}}, clients, identity, rate, usage,
        [&](const auto&, auto*) {
          ++calls;
          return ObjectReply{};
        },
        [&] { return cancelled; }, &body);
    Q4T_CHECK(reply.http_status == (cancelled ? 409 : 429) && calls == 0 &&
              body.empty() && usage.Total()->input == 0);
    Q4T_CHECK(clients.RateUsage(0).requests == 0 &&
              clients.RateUsage(0).reserved == 0);
  }
  return true;
}

Q4T_TEST(embeddings_batch_usage_rejects_missing_or_inconsistent_tokens) {
  for (const auto* raw :
       {"{}", R"({"usage":{"prompt_tokens":1}})",
        R"({"usage":{"prompt_tokens":1,"total_tokens":2}})",
        R"({"usage":{"prompt_tokens":-1,"total_tokens":-1}})",
        R"({"usage":{"prompt_tokens":1e20,"total_tokens":1e20}})"})
    Q4T_CHECK(!BatchResponseUsage("/v1/embeddings", Read(raw)));
  const auto usage = BatchResponseUsage(
      "/v1/embeddings",
      Read(R"({"usage":{"prompt_tokens":3,"total_tokens":3}})"));
  Q4T_CHECK(usage && usage->input == 3 && usage->output == 0 &&
            usage->cached == 0 && usage->reasoning == 0);
  return true;
}
