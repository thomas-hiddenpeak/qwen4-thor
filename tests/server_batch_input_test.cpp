#include "q4t/server/batch_input.h"

#include <stdexcept>
#include <vector>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
constexpr const char* kEndpoint = "/v1/responses";
Json Create(std::string endpoint = kEndpoint) {
  return Object({{"input_file_id", String("file_fixture")},
                 {"endpoint", String(std::move(endpoint))},
                 {"completion_window", String("24h")}});
}
Json Row(std::string id = "row", std::string model = "fixture") {
  return Object({{"custom_id", String(std::move(id))},
                 {"method", String("POST")},
                 {"url", String(kEndpoint)},
                 {"body", Object({{"model", String(std::move(model))},
                                  {"input", String("资料\n原文")}})}});
}
// Pure parser fixture. This does not claim to validate endpoint semantics or
// execute a model; the real endpoint-specific checker is wired separately.
ObjectReply Check(const BatchInputRow& row, BatchRowCheck* output) {
  output->canonical_model = row.body.GetString("model", "fixture");
  if (output->canonical_model == "alias") output->canonical_model = "fixture";
  return {};
}
struct Capture {
  std::vector<BatchInputRow> rows;
  BatchInput::Stage Stage() {
    return [this](BatchInputRow&& row) {
      rows.push_back(std::move(row));
      return ObjectReply{};
    };
  }
};
}  // namespace

Q4T_TEST(batch_create_all_endpoints_defaults_and_output_expiry) {
  for (const auto* endpoint :
       {"/v1/responses", "/v1/chat/completions", "/v1/embeddings",
        "/v1/completions", "/v1/moderations", "/v1/images/generations",
        "/v1/images/edits", "/v1/videos"}) {
    BatchCreate result;
    auto request = Create(endpoint);
    Q4T_CHECK(ParseBatchCreate(request, &result).ok());
    Q4T_CHECK(result.endpoint == endpoint && result.metadata.IsNull() &&
              result.output_ttl_seconds == 2592000);
    for (const auto seconds : {3600, 2592000}) {
      Set(&request, "output_expires_after",
          Object({{"anchor", String("created_at")},
                  {"seconds", Number(seconds)}}));
      Set(&request, "metadata", Object({{"键", String("值")}}));
      Q4T_CHECK(ParseBatchCreate(request, &result).ok() &&
                result.output_ttl_seconds == seconds &&
                result.metadata.GetString("键") == "值");
    }
  }
  return true;
}

Q4T_TEST(batch_create_invalid_fields_are_transactional) {
  std::vector<Json> requests;
  for (const auto* key : {"input_file_id", "endpoint", "completion_window",
                          "output_expires_after"}) {
    auto request = Create();
    Set(&request, key, Json());
    requests.push_back(request);
  }
  for (const auto& seconds :
       {Number(3599), Number(2592001), Boolean(true), String("3600")}) {
    auto request = Create();
    Set(&request, "output_expires_after",
        Object({{"anchor", String("created_at")}, {"seconds", seconds}}));
    requests.push_back(request);
  }
  auto unknown = Create();
  Set(&unknown, "owner", String("other-client"));
  requests.push_back(unknown);
  auto bad_metadata = Create();
  Set(&bad_metadata, "metadata", Object({{"key", Number(1)}}));
  requests.push_back(bad_metadata);
  for (const auto& request : requests) {
    BatchCreate result;
    result.input_file_id = "preserved";
    const auto reply = ParseBatchCreate(request, &result);
    Q4T_CHECK(!reply.ok() && reply.http_status == 400 &&
              !reply.value.GetString("param").empty() &&
              result.input_file_id == "preserved");
  }
  return true;
}

Q4T_TEST(batch_jsonl_fragmentation_preserves_ids_body_and_decimal_text) {
  const std::string first =
      R"({"custom_id":"资料\n1","method":"POST","url":"/v1/responses","body":{"model":"fixture","input":"a\nb","value":9007199254740993,"small":1e-999}})";
  const auto second = OutputJson(Row("two"));
  const auto wire = first + "\n" + second + "\n";
  for (size_t chunk = 1; chunk <= wire.size(); ++chunk) {
    Capture captured;
    BatchInput input(kEndpoint, Check, captured.Stage());
    for (size_t offset = 0; offset < wire.size(); offset += chunk)
      Q4T_CHECK(input.Feed(std::string_view(wire).substr(offset, chunk)).ok());
    Q4T_CHECK(input.Finish().ok() && input.Rows() == 2 &&
              input.Bytes() == wire.size() && input.BufferedBytes() == 0);
    Q4T_CHECK(captured.rows[0].custom_id == "资料\n1" &&
              captured.rows[0].body.Find("value")->str == "9007199254740993" &&
              captured.rows[0].body.Find("small")->str == "1e-999" &&
              captured.rows[0].body.GetString("input") == "a\nb" &&
              captured.rows[1].offset == first.size() + 1);
  }
  return true;
}

Q4T_TEST(batch_jsonl_crlf_offsets_and_unterminated_final_line) {
  const auto first = OutputJson(Row("first"));
  const auto second = OutputJson(Row("second"));
  Capture captured;
  BatchInput input(kEndpoint, Check, captured.Stage());
  Q4T_CHECK(input.Feed(first + "\r").ok() && input.Rows() == 0);
  Q4T_CHECK(input.Feed("\n" + second).ok() && input.Rows() == 1);
  Q4T_CHECK(input.Finish().ok() && input.Rows() == 2);
  Q4T_CHECK(captured.rows[0].offset == 0 &&
            captured.rows[0].wire_bytes == first.size() + 2 &&
            captured.rows[1].offset == first.size() + 2 &&
            captured.rows[1].wire_bytes == second.size());
  Q4T_CHECK(!input.Finish().ok() && !input.Feed("extra").ok());
  return true;
}

Q4T_TEST(batch_jsonl_rejects_blank_bom_invalid_utf8_and_truncated_json) {
  for (const auto& wire : std::vector<std::string>{
           "", "\n", " \r\n", "\xef\xbb\xbf" + OutputJson(Row()),
           "{\"custom_id\":\"\xff\"}", "{\"custom_id\":", "[]\n",
           OutputJson(Row()) + "\n\n"}) {
    Capture captured;
    BatchInput input(kEndpoint, Check, captured.Stage());
    auto reply = input.Feed(wire);
    if (reply.ok()) reply = input.Finish();
    Q4T_CHECK(!reply.ok() && reply.http_status == 400 &&
              reply.value.GetInt("line") > 0);
  }
  return true;
}

Q4T_TEST(batch_jsonl_duplicate_decoded_ids_and_duplicate_keys_rejected) {
  for (
      const auto& bad : std::vector<std::string>{
          R"({"custom_id":"\u0072ow","method":"POST","url":"/v1/responses","body":{"model":"fixture"}})",
          R"({"custom_id":"second","custom_id":"third","method":"POST","url":"/v1/responses","body":{}})",
          R"({"custom_id":"second","method":"POST","url":"/v1/responses","body":{"model":"fixture","model":"other"}})"}) {
    Capture captured;
    BatchInput input(kEndpoint, Check, captured.Stage());
    auto reply = input.Feed(OutputJson(Row()) + "\n" + bad + "\n");
    Q4T_CHECK(!reply.ok() && reply.value.GetInt("line") == 2 &&
              input.Rows() == 1 && captured.rows.size() == 1);
    Q4T_CHECK(!input.Finish().ok());
  }
  return true;
}

Q4T_TEST(batch_jsonl_envelope_method_url_and_stream_rejected_before_checker) {
  std::vector<Json> requests;
  for (const auto& [key, value] : std::vector<std::pair<std::string, Json>>{
           {"custom_id", String("")},
           {"method", String("GET")},
           {"url", String("https://example.test/v1/responses")},
           {"url", String("/v1/responses?owner=other")},
           {"body", Array()},
           {"authorization", String("secret")}}) {
    auto row = Row();
    Set(&row, key, value);
    requests.push_back(row);
  }
  for (const auto& stream : {Boolean(true), Number(0), String("false")}) {
    auto row = Row();
    Set(&Field(row, "body"), "stream", stream);
    requests.push_back(row);
  }
  for (const auto& request : requests) {
    size_t checked = 0;
    Capture captured;
    BatchInput input(
        kEndpoint,
        [&](const auto& row, auto* out) {
          ++checked;
          return Check(row, out);
        },
        captured.Stage());
    Q4T_CHECK(!input.Feed(OutputJson(request) + "\n").ok() && checked == 0 &&
              captured.rows.empty());
  }
  return true;
}

Q4T_TEST(batch_jsonl_canonical_model_alias_default_and_mismatch) {
  Capture captured;
  BatchInput input(kEndpoint, Check, captured.Stage());
  auto missing = Row("default");
  Field(missing, "body") = Object({{"input", String("source")}});
  Q4T_CHECK(input
                .Feed(OutputJson(missing) + "\n" +
                      OutputJson(Row("alias", "alias")) + "\n")
                .ok());
  Q4T_CHECK(input.Model() == "fixture");
  auto reply = input.Feed(OutputJson(Row("different", "other")) + "\n");
  Q4T_CHECK(!reply.ok() && reply.value.GetInt("line") == 3 &&
            reply.value.GetString("param") == "body.model" &&
            captured.rows.size() == 2);
  return true;
}

Q4T_TEST(batch_jsonl_endpoint_failure_and_stage_unknown_are_sticky) {
  for (const bool unknown : {false, true}) {
    size_t checks = 0, stages = 0;
    auto failure = Error(unknown ? 503 : 400,
                         unknown ? "object_io_timeout" : "invalid_request",
                         "Original error", unknown);
    failure.value = Object(
        {{"id", String("known-stage")}, {"param", String("body.input")}});
    BatchInput input(
        kEndpoint,
        [&](const auto& row, auto* output) {
          ++checks;
          return unknown ? Check(row, output) : failure;
        },
        [&](auto&&) {
          ++stages;
          return failure;
        });
    auto reply = input.Feed(OutputJson(Row("one")) + "\n" +
                            OutputJson(Row("two")) + "\n");
    Q4T_CHECK(!reply.ok() && reply.commit_unknown == unknown &&
              reply.value.GetString("id") == "known-stage" &&
              reply.value.GetInt("line") == 1 && input.Rows() == 0 &&
              checks == 1 && stages == (unknown ? 1U : 0U));
    Q4T_CHECK(input.Finish().code == reply.code &&
              input.Feed("third").code == reply.code && checks == 1);
  }
  return true;
}

Q4T_TEST(batch_jsonl_cancellation_before_feed_and_after_validation) {
  for (const bool before : {true, false}) {
    bool cancel = before;
    size_t checks = 0;
    Capture captured;
    BatchInput input(
        kEndpoint,
        [&](const auto& row, auto* output) {
          ++checks;
          cancel = true;
          return Check(row, output);
        },
        captured.Stage(), {}, [&] { return cancel; });
    const auto reply = input.Feed(OutputJson(Row()) + "\n");
    Q4T_CHECK(reply.http_status == 499 && captured.rows.empty() &&
              checks == (before ? 0U : 1U));
    cancel = false;
    Q4T_CHECK(input.Finish().code == "batch_cancelled");
  }
  return true;
}

Q4T_TEST(batch_jsonl_exact_configured_file_line_and_row_limits) {
  const auto wire = OutputJson(Row());
  for (const size_t line_limit : {wire.size(), wire.size() - 1}) {
    Capture captured;
    BatchInputLimits limits;
    limits.max_bytes = wire.size() + 1;
    limits.max_line_bytes = line_limit;
    limits.max_requests = 1;
    BatchInput input(kEndpoint, Check, captured.Stage(), limits);
    auto reply = input.Feed(wire + "\n");
    Q4T_CHECK(reply.ok() == (line_limit == wire.size()));
    if (reply.ok()) {
      Q4T_CHECK(input.Finish().ok());
    } else {
      Q4T_CHECK(captured.rows.empty());
    }
  }
  Capture captured;
  BatchInputLimits limits;
  limits.max_bytes = wire.size();
  BatchInput input(kEndpoint, Check, captured.Stage(), limits);
  Q4T_CHECK(!input.Feed(wire + "\n").ok() && captured.rows.empty());
  limits.max_bytes = 10000;
  limits.max_requests = 1;
  BatchInput one(kEndpoint, Check, captured.Stage(), limits);
  Q4T_CHECK(one.Feed(wire + "\n").ok());
  Q4T_CHECK(!one.Feed(OutputJson(Row("second")) + "\n").ok() &&
            captured.rows.size() == 1);
  return true;
}

Q4T_TEST(batch_jsonl_embedding_total_is_separate_from_request_count) {
  Capture captured;
  BatchInput input(
      "/v1/embeddings",
      [](const auto& row, auto* out) {
        out->canonical_model = "embedding-fixture";
        out->embedding_inputs = row.body.GetInt("count");
        return ObjectReply{};
      },
      captured.Stage());
  for (const int count : {25000, 25000, 1}) {
    auto row = Row(std::to_string(captured.rows.size()));
    Set(&row, "url", String("/v1/embeddings"));
    Set(&Field(row, "body"), "count", Number(count));
    const auto reply = input.Feed(OutputJson(row) + "\n");
    Q4T_CHECK(reply.ok() == (count != 1));
  }
  Q4T_CHECK(input.Rows() == 2 && captured.rows.size() == 2);
  return true;
}

Q4T_TEST(batch_jsonl_actual_200mb_50000_rows_keep_one_line_buffer) {
  size_t staged = 0;
  BatchInput input(kEndpoint, Check, [&](auto&& row) {
    ++staged;
    if (row.offset != (staged - 1) * 4000 || row.wire_bytes != 4000)
      return Error(500, "fixture_offset", "Incorrect original byte offset");
    return ObjectReply{};
  });
  for (size_t i = 0; i < kBatchRequests; ++i) {
    auto row = Row(std::to_string(i));
    Set(&Field(row, "body"), "padding", String(""));
    const size_t empty_size = OutputJson(row).size();
    Set(&Field(row, "body"), "padding",
        String(std::string(3999 - empty_size, 'x')));
    const auto wire = OutputJson(row) + "\n";
    Q4T_CHECK(wire.size() == 4000 && input.Feed(wire).ok() &&
              input.BufferedBytes() == 0);
  }
  Q4T_CHECK(input.Rows() == kBatchRequests && staged == kBatchRequests &&
            input.Bytes() == kBatchInputBytes && input.Finish().ok());
  return true;
}

Q4T_TEST(batch_jsonl_invalid_internal_limits_and_missing_resolver_rejected) {
  Capture captured;
  for (const int kind : {0, 1, 2}) {
    BatchInputLimits limits;
    if (kind == 0) limits.max_bytes = kBatchInputBytes + 1;
    if (kind == 1) limits.max_requests = kBatchRequests + 1;
    if (kind == 2) limits.max_line_bytes = 0;
    bool threw = false;
    try {
      BatchInput input(kEndpoint, Check, captured.Stage(), limits);
    } catch (const std::invalid_argument&) {
      threw = true;
    }
    Q4T_CHECK(threw);
  }
  BatchInput input(
      kEndpoint, [](const auto&, auto*) { return ObjectReply{}; },
      captured.Stage());
  Q4T_CHECK(input.Feed(OutputJson(Row()) + "\n").code ==
                "batch_validator_invalid" &&
            captured.rows.empty());
  return true;
}
