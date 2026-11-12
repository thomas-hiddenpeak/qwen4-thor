#include "q4t/server/vector_search_execution.h"

#include <algorithm>
#include <atomic>
#include <barrier>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <memory>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/files.h"
#include "q4t/server/vector_search_api.h"
#include "q4t/server/client_request.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/server/responses_reasoning.h"
#include "q4t/server/batch_preparation.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access(std::string scope = "client:search") {
  return {std::move(scope), Clock::now() + 20s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
void Require(const ObjectReply& reply) {
  if (!Ok(reply)) throw std::runtime_error("Search fixture preparation");
}
ObjectReply Bytes(std::string_view text, const VectorSearchControl& control,
                  VectorEncodedText* output) {
  return EncodeVectorText(
      text,
      [](auto source, auto* tokens) {
        for (unsigned char value : source) tokens->push_back(value);
        return q4t::Status{};
      },
      [](uint32_t token, auto* piece) {
        *piece = std::string(1, static_cast<char>(token));
        return q4t::Status{};
      },
      control, output);
}
ObjectReply Embed(const std::vector<int32_t>& tokens,
                  EmbeddingStepResult* result) {
  if (tokens.empty() || tokens.back() != q4t::model::EmbeddingConfig::kEos)
    return Error(500, "fixture_missing_eos", "Query EOS missing");
  result->hidden.assign(q4t::model::EmbeddingConfig::kHidden, 0);
  result->hidden[0] = 0x3f80;  // Exact BF16 1, explicit CPU math fixture.
  result->committed_input = tokens.size();
  return {};
}
struct Fixture {
  ObjectStoreOptions options;
  std::unique_ptr<ObjectStore> objects;
  std::string id;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!output) throw std::runtime_error("Missing search test output");
    auto path = (std::filesystem::path(output) / "search-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Search fixture path");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
    objects = std::make_unique<ObjectStore>(options);
    const auto root = VectorStores(*objects).Create(Access(), Object());
    Require(root);
    id = root.value.GetString("id");
  }
  std::string Add(std::string text = "PLE streams immutable tables from SSD.",
                  bool complete = true, size_t dimensions = 32) {
    const std::string wire =
        "--f\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
        "user_data\r\n--f\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"MODEL.md\"\r\nContent-Type: text/markdown\r\n\r\n" +
        text + "\r\n--f--\r\n";
    FileUpload upload(*objects, Access(), "multipart/form-data; boundary=f");
    Require(upload.Feed(wire, Access().deadline));
    const auto source = upload.Finish();
    Require(source);
    const auto file = source.value.GetString("id");
    return Index(file, complete, dimensions);
  }
  std::string Index(const std::string& file, bool complete = true,
                    size_t dimensions = 32, Json extra = Object()) {
    VectorStores stores(*objects);
    Set(&extra, "file_id", String(file));
    Require(stores.Attach(Access(), id, extra));
    if (!complete) return file;
    VectorIngestionClaim claim;
    Require(stores.Claim(Access(), id, file, "cpu-query-fixture", 60, &claim));
    VectorIngestionOptions options;
    options.dimensions = dimensions;
    VectorIngestionPrepared prepared;
    Require(PrepareVectorIngestion(*objects, Access(), claim, options, Bytes,
                                   &prepared));
    VectorIngestionResult ingested;
    Require(ExecuteVectorIngestion(
        *objects, Access(), prepared, [](auto) { return ObjectReply{}; },
        [dimensions](auto tokens, auto* vector, auto* used) {
          vector->assign(dimensions, 0);
          vector->front() = 1;
          *used = tokens.size();
          return ObjectReply{};
        },
        &ingested));
    return file;
  }
  ObjectReply Raw() {
    ObjectRequest request;
    request.kind = ObjectKind::kVectorStore;
    request.scope = Access().scope;
    request.id = id;
    return objects->Execute(request, Access().deadline);
  }
  void Write(Json record) {
    ObjectRequest request;
    request.operation = ObjectOperation::kPut;
    request.kind = ObjectKind::kVectorStore;
    request.scope = Access().scope;
    request.id = id;
    request.expected_revision = record.GetInt("revision");
    request.created_at = record.GetInt("created_at");
    request.expires_at = record.GetInt("expires_at");
    request.data = *record.Find("data");
    Require(objects->Execute(request, Access().deadline));
  }
};
struct Calls {
  int reserves = 0, embeds = 0, encodes = 0, rewrites = 0;
  uint64_t reserved = 0;
  std::vector<std::string> encoded;
  VectorSearchCallbacks callbacks;
  Calls() {
    callbacks.encode = [&](auto text, const auto& control, auto* result) {
      ++encodes;
      encoded.emplace_back(text);
      return Bytes(text, control, result);
    };
    callbacks.reserve = [&](auto count) {
      ++reserves;
      reserved = count;
      return ObjectReply{};
    };
    callbacks.embed = [&](const auto& tokens, auto* result) {
      ++embeds;
      return Embed(tokens, result);
    };
    callbacks.rewrite = [&](const auto& original, auto* rewritten) {
      ++rewrites;
      rewritten->assign(original.size(), "PLE SSD");
      return ObjectReply{};
    };
  }
};
Json Query() {
  return Object({{"query", String("Where are PLE tables stored?")},
                 {"ranking_options", Object({{"ranker", String("none")}})}});
}
ObjectReply Execute(Fixture& fixture, const Json& request, Calls& calls,
                    VectorSearchUsage* usage, BinaryAccess access = Access()) {
  return ExecuteVectorSearchRequest(*fixture.objects, access, fixture.id,
                                    request, 32, calls.callbacks, usage);
}
struct Api {
  ClientAdmission clients;
  ClientIdentity client;
  ActualUsageCounter usage;
  VectorNativeSearchCallbacks callbacks;
  int embeds = 0, rewrites = 0;
  explicit Api(uint64_t tpm = 10000, uint64_t prompt = 8192) {
    GatewayClientPolicy policy;
    const auto text = OutputJson(Object(
        {{"version", Number(3)},
         {"revision", Number(0)},
         {"gateway_token", String(std::string(64, 'a'))},
         {"fair_queue", Boolean(true)},
         {"clients", Array({Object({{"id", String("search")},
                                    {"max_inflight", Number(2)},
                                    {"max_control_inflight", Number(1)},
                                    {"max_prompt_tokens", Number(prompt)},
                                    {"max_output_tokens", Number(64)},
                                    {"max_body_bytes", Number(65536)},
                                    {"requests_per_minute", Number(1)},
                                    {"tokens_per_minute", Number(tpm)}})})}}));
    if (!ParseGatewayClientPolicy(text, &policy) ||
        !clients.Configure(std::move(policy)))
      throw std::runtime_error("Query policy fixture");
    client = clients.Authenticate("search", std::string(64, 'a'));
    callbacks.encode = Bytes;
    callbacks.embed = [&](const auto& tokens, auto* out) {
      ++embeds;
      return Embed(tokens, out);
    };
    callbacks.rewrite = [&](const auto& plan, auto& account, auto* out) {
      ++rewrites;
      auto reply = account.ReservePrompt(10);
      if (!reply.ok()) return reply;
      if (!account.StartStep()) throw std::runtime_error("Rewrite ownership");
      account.ObserveOutput(3);
      usage.Merge(ActualUsage{10, 3});
      Json input;
      if (!Parse(
              plan.request.GetArray("messages")->array[1].GetString("content"),
              &input))
        throw std::runtime_error("Rewrite fixture input");
      *out = {OutputJson(Object({{"queries", *input.GetArray("query")}})),
              "stop"};
      return ObjectReply{};
    };
  }
  ObjectReply Run(Fixture& fixture, Json request = Query(),
                  std::string method = "POST", std::string suffix = {},
                  bool configured = true, BinaryAccess access = Access()) {
    ClientRateLease rate;
    return HandleVectorSearchRequest(
        *fixture.objects, clients, configured, method,
        "/v1/vector_stores/" + fixture.id + "/search" + suffix,
        OutputJson(request), client, access, rate, usage, callbacks);
  }
};
}  // namespace

Q4T_TEST(vector_query_actual_files_text_vectors_and_exact_all_query_budget) {
  Fixture fixture;
  const auto file = fixture.Add();
  fixture.Add("Still pending", false);
  Calls calls;
  auto request = Query();
  Set(&request, "query", Array({String("PLE?"), String("SSD?")}));
  VectorSearchUsage usage;
  const auto result = Execute(fixture, request, calls, &usage);
  Q4T_CHECK(Ok(result) && calls.reserves == 1 && calls.embeds == 2 &&
            calls.encodes == 2 && calls.rewrites == 0);
  const auto expected = 2 * (kVectorQueryInstruction.size() + 4 + 1);
  Q4T_CHECK(calls.reserved == expected && usage.admitted_input == expected &&
            usage.committed_input == expected && !usage.unknown);
  Q4T_CHECK(calls.encoded[0] == std::string(kVectorQueryInstruction) + "PLE?");
  const auto& row = result.value.GetArray("data")->array.at(0);
  Q4T_CHECK(result.value.GetString("object") ==
                "vector_store.search_results.page" &&
            result.value.GetArray("data")->array.size() == 1 &&
            row.GetString("file_id") == file &&
            row.GetString("filename") == "MODEL.md" &&
            row.Find("score")->number == 1 &&
            row.GetArray("content")->array[0].GetString("text") ==
                "PLE streams immutable tables from SSD." &&
            !result.value.GetBool("has_more") &&
            result.value.Find("next_page")->IsNull());
  return true;
}

Q4T_TEST(vector_query_scope_filter_backend_and_deleted_sources_precede_model) {
  Fixture fixture;
  const auto file = fixture.Add();
  Calls calls;
  VectorSearchUsage usage;
  Q4T_CHECK(Execute(fixture, Query(), calls, &usage, Access("client:other"))
                .http_status == 404);
  Q4T_CHECK(ExecuteVectorSearchRequest(*fixture.objects, Access(), fixture.id,
                                       Query(), 64, calls.callbacks, &usage)
                .code == "vector_search_backend_mismatch");
  auto filtered = Query();
  Set(&filtered, "filters",
      Object({{"type", String("eq")},
              {"key", String("type")},
              {"value", String("run")}}));
  const auto empty = Execute(fixture, filtered, calls, &usage);
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty() &&
            calls.reserves == 1 && calls.reserved == 0 && calls.embeds == 0 &&
            calls.encodes == 0);
  Require(Files(*fixture.objects).Erase(Access(), file));
  Q4T_CHECK(Execute(fixture, Query(), calls, &usage).http_status == 404 &&
            calls.reserves == 1 && calls.embeds == 0 && calls.encodes == 0);
  return true;
}

Q4T_TEST(vector_query_rewrite_is_explicit_validated_and_used_for_ranking) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  auto request = Query();
  Set(&request, "rewrite_query", Boolean(true));
  VectorSearchUsage usage;
  const auto result = Execute(fixture, request, calls, &usage);
  Q4T_CHECK(Ok(result) && calls.rewrites == 1 &&
            calls.encoded[0] ==
                std::string(kVectorQueryInstruction) + "PLE SSD" &&
            result.value.GetArray("search_query")->array[0].str == "PLE SSD");
  calls.callbacks.rewrite = [](const auto&, auto* queries) {
    *queries = {"first", "extra"};
    return ObjectReply{};
  };
  Q4T_CHECK(Execute(fixture, request, calls, &usage).code ==
                "vector_query_rewrite_invalid" &&
            calls.reserves == 1 && calls.embeds == 1);
  calls.callbacks.rewrite = {};
  Q4T_CHECK(Execute(fixture, request, calls, &usage).code ==
            "vector_search_backend_unavailable");
  return true;
}

Q4T_TEST(vector_query_token_limit_and_quota_rejection_do_not_execute) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  VectorSearchUsage usage;
  auto request = Query();
  Set(&request, "query", String(std::string(8192, 'q')));
  Q4T_CHECK(Execute(fixture, request, calls, &usage).code ==
                "vector_query_token_limit_exceeded" &&
            calls.reserves == 0 && calls.embeds == 0);
  calls.callbacks.reserve = [](auto) {
    return Error(429, "client_tpm_exhausted", "Fixture TPM rejection");
  };
  Q4T_CHECK(Execute(fixture, Query(), calls, &usage).http_status == 429 &&
            calls.embeds == 0 && usage.admitted_input == 0 &&
            usage.committed_input == 0);
  return true;
}

Q4T_TEST(vector_query_failed_second_step_retains_usage_without_partial_hits) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  uint64_t expected = 0;
  calls.callbacks.embed = [&](const auto& tokens, auto* result) {
    ++calls.embeds;
    if (calls.embeds == 1) {
      expected += tokens.size();
      return Embed(tokens, result);
    }
    result->committed_input = 2;
    expected += 2;
    return Error(503, "embedding_unavailable", "Fixture failure");
  };
  auto request = Query();
  Set(&request, "query", Array({String("PLE"), String("SSD")}));
  VectorSearchUsage usage;
  const auto result = Execute(fixture, request, calls, &usage);
  Q4T_CHECK(result.http_status == 503 && !result.value.Has("data") &&
            calls.embeds == 2 && calls.reserves == 1 &&
            usage.committed_input == expected && !usage.unknown &&
            usage.committed_input < usage.admitted_input);
  return true;
}

Q4T_TEST(vector_query_exception_and_invalid_receipt_preserve_accounting) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  calls.callbacks.embed = [](const auto&, auto* result) -> ObjectReply {
    result->committed_input = 5;
    throw std::runtime_error("CPU fixture interrupted");
  };
  VectorSearchUsage usage;
  bool caught = false;
  try {
    (void)Execute(fixture, Query(), calls, &usage);
  } catch (const std::runtime_error&) {
    caught = true;
  }
  Q4T_CHECK(caught && usage.committed_input == 5 && !usage.unknown &&
            usage.admitted_input > 5);
  calls.callbacks.embed = [](const auto& tokens, auto* result) {
    auto reply = Embed(tokens, result);
    ++result->committed_input;
    return reply;
  };
  Q4T_CHECK(Execute(fixture, Query(), calls, &usage).code ==
                "embedding_result_invalid" &&
            usage.unknown && usage.admitted_input > 0);
  return true;
}

Q4T_TEST(vector_query_cancel_after_model_retains_usage_and_hides_results) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  bool cancelled = false;
  calls.callbacks.embed = [&](const auto& tokens, auto* result) {
    auto reply = Embed(tokens, result);
    cancelled = true;
    return reply;
  };
  auto access = Access();
  access.cancelled = [&] { return cancelled; };
  VectorSearchUsage usage;
  const auto result = Execute(fixture, Query(), calls, &usage, access);
  Q4T_CHECK(result.code == "request_cancelled" && !result.value.Has("data") &&
            usage.committed_input == usage.admitted_input &&
            usage.committed_input > 0);
  return true;
}

Q4T_TEST(vector_query_fractional_score_and_invalid_hidden_never_fake_success) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  calls.callbacks.embed = [](const auto& tokens, auto* result) {
    auto reply = Embed(tokens, result);
    result->hidden[1] = 0x3f80;
    return reply;
  };
  VectorSearchUsage usage;
  auto result = Execute(fixture, Query(), calls, &usage);
  Q4T_CHECK(Ok(result));
  const auto score = result.value.GetArray("data")->array[0].Find("score");
  Q4T_CHECK(score->number > 0.70 && score->number < 0.71 &&
            OutputJson(*score).starts_with("0.70"));
  for (const uint16_t value : {uint16_t{0}, uint16_t{0x7fc0}}) {
    calls.callbacks.embed = [&](const auto& tokens, auto* step) {
      auto reply = Embed(tokens, step);
      step->hidden[0] = value;
      return reply;
    };
    result = Execute(fixture, Query(), calls, &usage);
    Q4T_CHECK(result.code == "embedding_result_invalid" &&
              !result.value.Has("data") && usage.committed_input > 0 &&
              !usage.unknown);
  }
  return true;
}

Q4T_TEST(vector_query_expired_deadline_and_invalid_token_fail_before_model) {
  Fixture fixture;
  fixture.Add();
  Calls calls;
  VectorSearchUsage usage;
  auto access = Access();
  access.deadline = Clock::now() - 1ms;
  Q4T_CHECK(Execute(fixture, Query(), calls, &usage, access).code ==
                "request_deadline_exceeded" &&
            calls.reserves == 0 && calls.encodes == 0 && calls.embeds == 0);
  calls.callbacks.encode = [](auto, const auto&, auto* result) {
    result->tokens = {q4t::model::EmbeddingConfig::kVocab};
    return ObjectReply{};
  };
  Q4T_CHECK(Execute(fixture, Query(), calls, &usage).code ==
                "vector_query_encoding_invalid" &&
            calls.reserves == 0 && calls.embeds == 0);
  return true;
}

Q4T_TEST(vector_query_mutation_rejects_snapshot_without_replaying_model) {
  Fixture fixture;
  const auto file = fixture.Add();
  Calls calls;
  calls.callbacks.embed = [&](const auto& tokens, auto* result) {
    ++calls.embeds;
    Require(VectorStores(*fixture.objects)
                .UpdateFile(Access(), fixture.id, file,
                            Object({{"attributes",
                                     Object({{"revision", Number(2)}})}})));
    return Embed(tokens, result);
  };
  VectorSearchUsage usage;
  const auto result = Execute(fixture, Query(), calls, &usage);
  Q4T_CHECK(result.code == "vector_search_snapshot_changed" &&
            !result.value.Has("data") && calls.embeds == 1 &&
            calls.reserves == 1 && usage.committed_input > 0);
  return true;
}

Q4T_TEST(vector_query_source_deletion_during_model_hides_retained_index) {
  Fixture fixture;
  const auto file = fixture.Add();
  Calls calls;
  calls.callbacks.embed = [&](const auto& tokens, auto* result) {
    Require(Files(*fixture.objects).Erase(Access(), file));
    return Embed(tokens, result);
  };
  VectorSearchUsage usage;
  const auto result = Execute(fixture, Query(), calls, &usage);
  Q4T_CHECK(result.http_status == 404 && !result.value.Has("data") &&
            usage.committed_input > 0);
  return true;
}

Q4T_TEST(vector_corpus_parallel_touches_and_metadata_preserve_snapshot) {
  Fixture fixture;
  fixture.Add();
  VectorStores stores(*fixture.objects);
  VectorSearchCorpus corpus;
  Require(stores.ReadSearchCorpus(Access(), fixture.id, {}, 32, &corpus));
  Require(stores.Update(Access(), fixture.id,
                        Object({{"name", String("Updated label")}})));
  std::barrier barrier(2);
  ObjectReply a, b;
  std::thread one([&] {
    barrier.arrive_and_wait();
    a = stores.TouchSearch(Access(), corpus);
  });
  std::thread two([&] {
    barrier.arrive_and_wait();
    b = stores.TouchSearch(Access(), corpus);
  });
  one.join();
  two.join();
  Q4T_CHECK(Ok(a) && Ok(b));
  VectorSearchCorpus current;
  Require(stores.ReadSearchCorpus(Access(), fixture.id, {}, 32, &current));
  Q4T_CHECK(current.search_revision == corpus.search_revision &&
            current.chunks.size() == 1);
  Q4T_CHECK(stores.TouchSearch(Access("client:other"), corpus).http_status ==
            403);
  return true;
}

Q4T_TEST(vector_corpus_legacy_revision_migration_and_malformed_revision) {
  Fixture fixture;
  fixture.Add();
  auto record = fixture.Raw().value;
  auto& fields = Field(record, "data").object;
  std::erase_if(fields, [](const auto& field) {
    return field.first == "search_revision";
  });
  fixture.Write(record);
  VectorStores stores(*fixture.objects);
  VectorSearchCorpus corpus;
  Require(stores.ReadSearchCorpus(Access(), fixture.id, {}, 32, &corpus));
  Q4T_CHECK(corpus.search_revision == fixture.Raw().value.GetInt("revision"));
  Require(stores.TouchSearch(Access(), corpus));
  Require(stores.TouchSearch(Access(), corpus));
  Q4T_CHECK(fixture.Raw().value.Find("data")->GetInt("search_revision") ==
            corpus.search_revision);
  record = fixture.Raw().value;
  Set(&Field(record, "data"), "search_revision", Number(0));
  fixture.Write(record);
  Q4T_CHECK(stores.ReadSearchCorpus(Access(), fixture.id, {}, 32, &corpus)
                    .http_status == 503 &&
            corpus.chunks.empty());
  return true;
}

Q4T_TEST(vector_rewrite_request_uses_constrained_real_generation_contract) {
  VectorRewritePlan plan;
  const std::vector<std::string> queries{"Where is PLE?", "What is MTP?"};
  Q4T_CHECK(
      Ok(PlanVectorRewrite(queries, &plan)) && plan.output_tokens == 320 &&
      plan.request.GetInt("max_completion_tokens") == 320 &&
      !plan.request.Find("chat_template_kwargs")->GetBool("enable_thinking") &&
      !plan.request.Has("tools"));
  const auto& schema =
      *plan.request.Find("response_format")->Find("json_schema");
  Q4T_CHECK(schema.GetBool("strict") && schema.Find("schema")
                                                ->Find("properties")
                                                ->Find("queries")
                                                ->GetInt("minItems") == 2);
  const auto messages = plan.request.GetArray("messages");
  Q4T_CHECK(messages->array.size() == 2 &&
            messages->array[1].GetString("role") == "user" &&
            messages->array[1].GetString("content").find("Where is PLE?") !=
                std::string::npos);
  Q4T_CHECK(!PlanVectorRewrite({}, &plan).ok());
  return true;
}

Q4T_TEST(vector_rewrite_parser_rejects_truncation_shape_and_count_errors) {
  const std::vector<std::string> original{"first", "second"};
  std::vector<std::string> queries;
  const std::string valid = R"({"queries":["PLE SSD","MTP draft"]})";
  Q4T_CHECK(Ok(ParseVectorRewrite(original, valid, "stop", &queries)) &&
            queries == std::vector<std::string>({"PLE SSD", "MTP draft"}));
  for (const auto reason : {"length", "tool_calls", "content_filter", ""})
    Q4T_CHECK(!ParseVectorRewrite(original, valid, reason, &queries).ok() &&
              queries.empty());
  for (const auto* invalid :
       {R"({"queries":["one"]})", R"({"queries":["one",""]})",
        R"({"queries":["one",2]})", R"({"queries":[],"queries":[]})",
        R"({"queries":["one","two"],"extra":true})", "not JSON"})
    Q4T_CHECK(!ParseVectorRewrite(original, invalid, "stop", &queries).ok() &&
              queries.empty());
  return true;
}

Q4T_TEST(vector_search_api_exact_wire_scope_and_data_lane) {
  Fixture fixture;
  Api api;
  const auto path = "/v1/vector_stores/" + fixture.id + "/search";
  Q4T_CHECK(VectorStoreMethodAllowed("POST", path) &&
            !VectorStoreMethodAllowed("GET", path) &&
            !VectorStoreMethodAllowed("POST", path + "/extra") &&
            !VectorStoreMethodAllowed("POST", path + "/") &&
            !IsVectorStoreObservation("POST", path) &&
            ClassifyHttpConnection("POST", path) ==
                HttpConnectionClass::kBusiness);
  Q4T_CHECK(api.Run(fixture, Query(), "GET").http_status == 405 &&
            api.Run(fixture, Query(), "GET").value.GetString("allow") ==
                "POST");
  Q4T_CHECK(api.Run(fixture, Query(), "POST", "?owner=forged").http_status ==
            400);
  Q4T_CHECK(api.Run(fixture, Query(), "POST", {}, true, Access("client:other"))
                .http_status == 403);
  auto request = Query();
  Set(&request, "owner", String("other"));
  Q4T_CHECK(api.Run(fixture, request).http_status == 400 &&
            api.clients.RateUsage(0).requests == 0 && api.embeds == 0);
  return true;
}

Q4T_TEST(vector_search_api_rewrite_and_all_queries_share_one_rpm_actual_tpm) {
  Fixture fixture;
  fixture.Add("PLE SSD document", true, 1024);
  Api api;
  auto request = Query();
  Set(&request, "query", Array({String("PLE"), String("SSD")}));
  Set(&request, "rewrite_query", Boolean(true));
  auto result = api.Run(fixture, request);
  const uint64_t input = 10 + 2 * (kVectorQueryInstruction.size() + 3 + 1);
  const auto used = api.usage.Total();
  const auto rates = api.clients.RateUsage(0);
  Q4T_CHECK(Ok(result) && api.embeds == 2 && api.rewrites == 1 && used &&
            used->input == input && used->output == 3 && rates.requests == 1 &&
            rates.admitted == 1 && rates.settled_tokens == input + 3 &&
            rates.reserved == 0);
  Q4T_CHECK(api.Run(fixture).code == "client_rpm_exhausted" && api.embeds == 2);
  return true;
}

Q4T_TEST(vector_search_api_empty_corpus_still_admits_one_external_request) {
  Fixture fixture;
  Api api;
  const auto reply = api.Run(fixture);
  Q4T_CHECK(Ok(reply) && reply.value.GetArray("data")->array.empty() &&
            api.embeds == 0 && api.rewrites == 0 && api.usage.Total() &&
            api.usage.Total()->input == 0 &&
            api.clients.RateUsage(0).requests == 1 &&
            api.clients.RateUsage(0).reserved == 0);
  return true;
}

Q4T_TEST(vector_search_api_failures_and_exceptions_settle_partial_input) {
  Fixture fixture;
  fixture.Add("PLE SSD document", true, 1024);
  for (const bool exception : {false, true}) {
    Api api;
    api.callbacks.embed = [exception](const auto&, auto* out) -> ObjectReply {
      out->committed_input = 7;
      if (exception) throw std::runtime_error("CPU model failure");
      return Error(503, "embedding_compute_failed", "CPU model failure");
    };
    bool failed = false;
    try {
      failed = api.Run(fixture).code == "embedding_compute_failed";
    } catch (const std::runtime_error&) {
      failed = exception;
    }
    const auto rates = api.clients.RateUsage(0);
    Q4T_CHECK(failed && rates.settled_tokens == 7 && rates.reserved == 0 &&
              rates.requests == 1 && api.usage.Total() &&
              api.usage.Total()->input == 7);
  }
  return true;
}

Q4T_TEST(vector_search_api_unknown_usage_keeps_entire_admitted_input) {
  Fixture fixture;
  fixture.Add("PLE SSD document", true, 1024);
  Api api;
  uint64_t reserved = 0;
  api.callbacks.embed = [&](const auto& tokens, auto* out) {
    auto reply = Embed(tokens, out);
    reserved = tokens.size();
    ++out->committed_input;
    return reply;
  };
  Q4T_CHECK(api.Run(fixture).code == "embedding_result_invalid" &&
            !api.usage.Total() && reserved > 0 &&
            api.clients.RateUsage(0).settled_tokens == reserved &&
            api.clients.RateUsage(0).reserved == 0);
  return true;
}

Q4T_TEST(vector_search_api_rewrite_error_never_embeds_and_keeps_actual_usage) {
  Fixture fixture;
  fixture.Add("PLE SSD document", true, 1024);
  Api api;
  auto execute = api.callbacks.rewrite;
  api.callbacks.rewrite = [&](const auto& plan, auto& account, auto* out) {
    auto reply = execute(plan, account, out);
    out->finish_reason = "length";
    return reply;
  };
  auto request = Query();
  Set(&request, "rewrite_query", Boolean(true));
  Q4T_CHECK(api.Run(fixture, request).code == "vector_query_rewrite_invalid" &&
            api.embeds == 0 && api.rewrites == 1 && api.usage.Total() &&
            api.usage.Total()->input == 10 && api.usage.Total()->output == 3 &&
            api.clients.RateUsage(0).settled_tokens == 13 &&
            api.clients.RateUsage(0).reserved == 0);
  return true;
}

Q4T_TEST(vector_search_api_tpm_extension_and_cumulative_prompt_limit) {
  Fixture fixture;
  fixture.Add("PLE SSD document", true, 1024);
  auto request = Query();
  Set(&request, "rewrite_query", Boolean(true));
  for (const bool prompt_limit : {false, true}) {
    Api api(prompt_limit ? 10000 : 250, prompt_limit ? 64 : 8192);
    const auto reply = api.Run(fixture, request);
    Q4T_CHECK(reply.code == (prompt_limit ? "client_prompt_limit_exceeded"
                                          : "client_tpm_exhausted") &&
              api.rewrites == 1 && api.embeds == 0 &&
              api.clients.RateUsage(0).requests == 1 &&
              api.clients.RateUsage(0).settled_tokens == 13 &&
              api.clients.RateUsage(0).reserved == 0);
  }
  return true;
}

Q4T_TEST(vector_search_api_missing_backend_preserves_rate_and_other_requests) {
  Fixture fixture;
  Api api;
  Q4T_CHECK(api.Run(fixture, Query(), "POST", {}, false).code ==
                "vector_search_backend_unavailable" &&
            api.clients.RateUsage(0).requests == 0);
  Q4T_CHECK(Ok(api.Run(fixture)) && api.clients.RateUsage(0).requests == 1);
  return true;
}

Q4T_TEST(vector_search_api_rewrite_uses_actual_primary_chat_planning) {
  VectorRewritePlan rewrite;
  Require(PlanVectorRewrite({"PLE source?", "MTP draft?"}, &rewrite));
  ChatPlanConfig config;
  config.model = "native-qwen";
  config.structured = true;
  ChatRequestPlan plan;
  ChatPlanError error;
  const auto status = PlanChatRequest(rewrite.request, config, &plan, &error);
  Q4T_CHECK(status && plan.constraints.json_output &&
            !rewrite.request.Has("tools"));
  config.structured = false;
  Q4T_CHECK(!PlanChatRequest(rewrite.request, config, &plan, &error));
  return true;
}

namespace {
std::string NewSearchStore(Fixture& fixture, BinaryAccess access = Access()) {
  const auto reply = VectorStores(*fixture.objects).Create(access, Object());
  Require(reply);
  return reply.value.GetString("id");
}
VectorSearchPlan MultiPlan() {
  VectorSearchPlan plan;
  ChatPlanError error;
  if (!PlanVectorSearch(Query(), &plan, &error))
    throw std::runtime_error("Multi-store plan fixture");
  return plan;
}
ObjectReply Multi(Fixture& fixture, const std::vector<std::string>& ids,
                  Calls& calls, VectorSearchUsage* usage,
                  VectorSearchPlan plan = MultiPlan(),
                  BinaryAccess access = Access()) {
  return ExecuteVectorSearchPlan(*fixture.objects, access, ids, plan, 32,
                                 calls.callbacks, usage);
}
}  // namespace

Q4T_TEST(vector_multi_store_global_weighted_rank_and_single_query_admission) {
  Fixture fixture;
  const auto first = fixture.id;
  fixture.Add("PLE tables live on SSD.");
  fixture.id = NewSearchStore(fixture);
  const auto second_file = fixture.Add("quantum physics experiments");
  auto plan = MultiPlan();
  plan.queries = {"quantum", "physics"};
  plan.ranker = "auto";
  plan.embedding_weight = 0;
  plan.text_weight = 1;
  plan.max_results = 1;
  Calls calls;
  VectorSearchUsage usage;
  const auto result = Multi(fixture, {first, fixture.id}, calls, &usage, plan);
  Q4T_CHECK(Ok(result) && calls.reserves == 1 && calls.embeds == 2 &&
            calls.encodes == 2 && usage.committed_input == calls.reserved);
  const auto* data = result.value.GetArray("data");
  Q4T_CHECK(data && data->array.size() == 1 &&
            data->array.front().GetString("file_id") == second_file &&
            data->array.front().GetNumber("score") == 1);
  return true;
}

Q4T_TEST(vector_multi_store_duplicate_chunks_use_first_matching_membership) {
  Fixture fixture;
  const auto first = fixture.id;
  const auto file = fixture.Add();
  fixture.id = NewSearchStore(fixture);
  fixture.Index(file, true, 32,
                Object({{"attributes", Object({{"kind", String("run")}})}}));
  Calls calls;
  VectorSearchUsage usage;
  auto result = Multi(fixture, {first, fixture.id}, calls, &usage);
  Q4T_CHECK(Ok(result));
  const auto* rows = result.value.GetArray("data");
  Q4T_CHECK(rows && rows->array.size() == 1 &&
            rows->array[0].Find("attributes")->object.empty());
  auto plan = MultiPlan();
  plan.filters = Object({{"key", String("kind")}, {"value", String("run")}});
  result = Multi(fixture, {first, fixture.id}, calls, &usage, plan);
  Q4T_CHECK(Ok(result));
  rows = result.value.GetArray("data");
  Q4T_CHECK(rows && rows->array.size() == 1 &&
            rows->array[0].Find("attributes")->GetString("kind") == "run");
  return true;
}

Q4T_TEST(vector_multi_store_missing_or_other_scope_precedes_any_model) {
  Fixture fixture;
  fixture.Add();
  const auto other = NewSearchStore(fixture, Access("client:other"));
  Calls calls;
  VectorSearchUsage usage;
  for (const auto& id : {std::string("vs_absent"), other}) {
    auto plan = MultiPlan();
    plan.rewrite_query = true;
    const auto reply = Multi(fixture, {fixture.id, id}, calls, &usage, plan);
    Q4T_CHECK(reply.http_status == 404 && !reply.value.Has("data"));
  }
  Q4T_CHECK(!calls.encodes && !calls.embeds && !calls.rewrites &&
            !calls.reserves && !usage.admitted_input);
  return true;
}

Q4T_TEST(vector_multi_store_second_backend_mismatch_has_no_model_side_effect) {
  Fixture fixture;
  const auto first = fixture.id;
  fixture.Add();
  fixture.id = NewSearchStore(fixture);
  fixture.Add("Other backend", true, 64);
  Calls calls;
  VectorSearchUsage usage;
  Q4T_CHECK(Multi(fixture, {first, fixture.id}, calls, &usage).code ==
                "vector_search_backend_mismatch" &&
            !calls.embeds && !calls.encodes && !calls.reserves);
  return true;
}

Q4T_TEST(vector_multi_store_invalid_ids_and_plans_do_not_bypass_empty_corpus) {
  Fixture fixture;
  Calls calls;
  VectorSearchUsage usage;
  for (const auto& ids : {std::vector<std::string>{},
                          {fixture.id, fixture.id},
                          {"vs_../x"},
                          {"file_wrong"}}) {
    const auto reply = Multi(fixture, ids, calls, &usage);
    Q4T_CHECK(reply.http_status == 400 && !reply.value.Has("data"));
  }
  std::vector<std::string> ids(kVectorSearchStores + 1, fixture.id);
  Q4T_CHECK(Multi(fixture, ids, calls, &usage).code ==
            "vector_search_resource_exhausted");
  for (int variant = 0; variant < 4; ++variant) {
    auto plan = MultiPlan();
    if (variant == 0) plan.queries.clear();
    if (variant == 1) plan.embedding_weight = -1;
    if (variant == 2) plan.max_results = 0;
    if (variant == 3) plan.score_threshold = 2;
    Q4T_CHECK(!Multi(fixture, {fixture.id}, calls, &usage, plan).ok());
  }
  Q4T_CHECK(!calls.encodes && !calls.embeds && !calls.reserves);
  return true;
}

Q4T_TEST(vector_multi_store_later_mutation_hides_all_results_without_replay) {
  Fixture fixture;
  const auto first = fixture.id;
  fixture.Add();
  fixture.id = NewSearchStore(fixture);
  const auto second_file = fixture.Add("Release notes");
  Calls calls;
  calls.callbacks.embed = [&](const auto& tokens, auto* step) {
    ++calls.embeds;
    Require(VectorStores(*fixture.objects)
                .UpdateFile(Access(), fixture.id, second_file,
                            Object({{"attributes",
                                     Object({{"revision", Number(2)}})}})));
    return Embed(tokens, step);
  };
  VectorSearchUsage usage;
  const auto reply = Multi(fixture, {first, fixture.id}, calls, &usage);
  Q4T_CHECK(reply.code == "vector_search_snapshot_changed" &&
            !reply.value.Has("data") && calls.embeds == 1 &&
            calls.reserves == 1 && usage.committed_input > 0);
  return true;
}

Q4T_TEST(vector_multi_store_later_source_deleted_hides_retained_index) {
  Fixture fixture;
  const auto first = fixture.id;
  fixture.Add();
  fixture.id = NewSearchStore(fixture);
  const auto second_file = fixture.Add("Release notes");
  Calls calls;
  calls.callbacks.embed = [&](const auto& tokens, auto* step) {
    ++calls.embeds;
    Require(Files(*fixture.objects).Erase(Access(), second_file));
    return Embed(tokens, step);
  };
  VectorSearchUsage usage;
  const auto reply = Multi(fixture, {first, fixture.id}, calls, &usage);
  Q4T_CHECK(reply.http_status == 404 && !reply.value.Has("data") &&
            calls.embeds == 1 && usage.committed_input > 0);
  return true;
}

Q4T_TEST(vector_multi_store_cancel_or_deadline_never_returns_partial_hits) {
  Fixture fixture;
  const auto first = fixture.id;
  fixture.Add();
  fixture.id = NewSearchStore(fixture);
  fixture.Add("Run results");
  Calls calls;
  bool cancelled = false;
  auto access = Access();
  access.cancelled = [&] { return cancelled; };
  calls.callbacks.embed = [&](const auto& tokens, auto* step) {
    ++calls.embeds;
    cancelled = true;
    return Embed(tokens, step);
  };
  VectorSearchUsage usage;
  auto reply =
      Multi(fixture, {first, fixture.id}, calls, &usage, MultiPlan(), access);
  Q4T_CHECK(reply.http_status == 499 && !reply.value.Has("data") &&
            calls.embeds == 1 && usage.committed_input > 0);
  access.cancelled = {};
  access.deadline = Clock::now() - 1ms;
  reply =
      Multi(fixture, {first, fixture.id}, calls, &usage, MultiPlan(), access);
  Q4T_CHECK(reply.http_status == 408 && calls.embeds == 1 &&
            !usage.committed_input);
  return true;
}

Q4T_TEST(vector_multi_store_metadata_writes_preserve_all_search_snapshots) {
  Fixture fixture;
  const auto first = fixture.id;
  fixture.Add();
  fixture.id = NewSearchStore(fixture);
  fixture.Add("Run results");
  Calls calls;
  calls.callbacks.embed = [&](const auto& tokens, auto* step) {
    ++calls.embeds;
    for (const auto& id : {first, fixture.id})
      Require(VectorStores(*fixture.objects)
                  .Update(Access(), id, Object({{"name", String("renamed")}})));
    return Embed(tokens, step);
  };
  VectorSearchUsage usage;
  const auto reply = Multi(fixture, {first, fixture.id}, calls, &usage);
  Q4T_CHECK(Ok(reply) && reply.value.GetArray("data")->array.size() == 2 &&
            calls.embeds == 1 && calls.reserves == 1);
  return true;
}

Q4T_TEST(vector_multi_store_empty_corpora_rewrite_once_and_reserve_zero) {
  Fixture fixture;
  const auto second = NewSearchStore(fixture);
  Calls calls;
  auto plan = MultiPlan();
  plan.rewrite_query = true;
  VectorSearchUsage usage;
  const auto reply = Multi(fixture, {fixture.id, second}, calls, &usage, plan);
  Q4T_CHECK(Ok(reply) && reply.value.GetArray("data")->array.empty() &&
            reply.value.GetArray("search_query")->array[0].str == "PLE SSD" &&
            calls.rewrites == 1 && calls.reserves == 1 && !calls.reserved &&
            !calls.embeds && !calls.encodes && !usage.committed_input);
  return true;
}

Q4T_TEST(vector_multi_store_aggregate_work_bound_counts_duplicate_memberships) {
  Fixture fixture;
  const auto first = fixture.id;
  const auto file = fixture.Add(std::string(830000, 'x'));
  fixture.id = NewSearchStore(fixture);
  fixture.Index(file);
  Calls calls;
  VectorSearchUsage usage;
  const auto reply = Multi(fixture, {first, fixture.id}, calls, &usage);
  Q4T_CHECK(reply.code == "vector_search_resource_exhausted" &&
            !reply.value.Has("data") && !calls.encodes && !calls.embeds &&
            !calls.reserves && !usage.committed_input);
  return true;
}

namespace {
ChatPlanConfig NativeConfig() {
  ChatPlanConfig config;
  config.model = "cpu";
  config.structured = true;
  return config;
}
StateAccess NativeAccess() {
  const auto access = Access();
  return {access.scope, access.deadline, access.cancelled};
}
Json NativeRequest(const Fixture& fixture, int limit = 3, bool include = true) {
  auto request = Object(
      {{"input", String("Find PLE storage details")},
       {"stream", Boolean(true)},
       {"reasoning", Object({{"effort", String("none")}})},
       {"max_output_tokens", Number(128)},
       {"max_tool_calls", Number(limit)},
       {"tools",
        Array({Object({{"type", String("file_search")},
                       {"vector_store_ids", Array({String(fixture.id)})}})})},
       {"tool_choice", Object({{"type", String("file_search")}})},
       {"stream_options", Object({{"include_obfuscation", Boolean(false)}})}});
  if (include)
    Set(&request, "include", Array({String("file_search_call.results")}));
  return request;
}
std::string NativeBegin(ResponsesExecution& run, const Json& request) {
  Require(run.Prepare(request, NativeConfig(), NativeAccess(), 42));
  std::string wire;
  Require(run.Begin(&wire));
  return wire;
}
ResponseDelta NativeDelta(const ResponsesExecution& run, size_t index = 0,
                          std::string args = R"({"queries":["PLE SSD"]})") {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call.index = index;
  delta.call.id = "native_call_" + std::to_string(index);
  delta.call.name = run.Generation()
                        .generation_request.GetArray("tools")
                        ->array[0]
                        .Find("function")
                        ->GetString("name");
  delta.call.arguments = std::move(args);
  return delta;
}
ResponseDelta NativeText(std::string text) {
  ResponseDelta delta;
  delta.text = std::move(text);
  return delta;
}
bool Silent(const std::string&) { return true; }
}  // namespace

Q4T_TEST(responses_native_search_executes_actual_index_and_continues_answer) {
  Fixture fixture;
  const auto file = fixture.Add("PLE streams tables from SSD.", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  Q4T_CHECK(run.HasFileSearch() && run.VariableInputSteps());
  std::string wire, events;
  Q4T_CHECK(run.Append(NativeDelta(run), "null", &wire));
  Q4T_CHECK(wire.find("function_call_arguments") == wire.npos &&
            wire.find("q4t_file_search") == wire.npos);
  Calls calls;
  VectorSearchUsage usage;
  bool answer = false;
  Q4T_CHECK(Ok(run.RunFileSearches(1, calls.callbacks, &usage, &answer,
                                   [&](const std::string& text) {
                                     events += text;
                                     return true;
                                   })) &&
            answer && !run.PendingFileSearch() && calls.embeds == 1 &&
            calls.reserves == 1 && usage.committed_input == calls.reserved);
  Q4T_CHECK(events.find("file_search_call.searching") != events.npos &&
            events.find("file_search_call.completed") != events.npos);
  const auto& item = run.Output().Snapshot().GetArray("output")->array[0];
  Q4T_CHECK(item.GetArray("results")->array[0].GetString("file_id") == file);
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(7, NativeConfig(), {}, &next)));
  Q4T_CHECK(next.generation_request.GetInt("max_completion_tokens") == 121 &&
            OutputJson(next.generation_request).find("PLE streams tables") !=
                std::string::npos &&
            next.generation_request.Find("tool_choice")
                    ->Find("allowed_tools")
                    ->GetString("mode") == "auto");
  Q4T_CHECK(run.Append(NativeText("PLE uses SSD."), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {64, 0, 12}, 43, &wire)));
  Q4T_CHECK(run.ResultJson().find(file) != std::string::npos);
  return true;
}

Q4T_TEST(responses_native_search_projection_keeps_full_durable_evidence) {
  Fixture fixture;
  fixture.Add("PRIVATE_RETRIEVED_PASSAGE", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture, 1, false));
  std::string wire, internal;
  Q4T_CHECK(run.Append(NativeDelta(run), "null", &wire));
  Calls calls;
  VectorSearchUsage usage;
  bool answer;
  Q4T_CHECK(Ok(run.RunFileSearches(1, calls.callbacks, &usage, &answer,
                                   [&](const std::string& text) {
                                     internal += text;
                                     return true;
                                   })));
  Q4T_CHECK(internal.find("PRIVATE_RETRIEVED_PASSAGE") != internal.npos &&
            run.PublicWire(internal).find("PRIVATE_RETRIEVED_PASSAGE") ==
                std::string::npos);
  Q4T_CHECK(Ok(run.Finish(false, 0, {40, 0, 6}, 43, &wire)));
  Q4T_CHECK(run.ResultJson().find("PRIVATE_RETRIEVED_PASSAGE") ==
                std::string::npos &&
            run.PublicWire(wire).find("PRIVATE_RETRIEVED_PASSAGE") ==
                std::string::npos);
  const auto stored =
      ResponsesState(*fixture.objects).Retrieve(run.Id(), NativeAccess());
  Q4T_CHECK(Ok(stored) &&
            OutputJson(stored.value).find("PRIVATE_RETRIEVED_PASSAGE") !=
                std::string::npos);
  return true;
}

Q4T_TEST(responses_native_search_truncated_or_invalid_arguments_never_execute) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  for (const bool truncated : {false, true}) {
    ResponsesExecution run(*fixture.objects);
    NativeBegin(run, NativeRequest(fixture));
    std::string wire;
    Q4T_CHECK(run.Append(
        NativeDelta(
            run, 0,
            truncated
                ? R"({"queries":["PLE)"
                : R"({"queries":["PLE"],"vector_store_ids":["vs_other"]})"),
        "null", &wire));
    Calls calls;
    VectorSearchUsage usage;
    bool answer;
    const auto result = run.RunFileSearches(truncated ? 0 : 1, calls.callbacks,
                                            &usage, &answer, Silent);
    Q4T_CHECK(result.code == (truncated ? "file_search_incomplete"
                                        : "file_search_arguments_invalid") &&
              !calls.embeds && !calls.reserves && !answer);
    if (truncated) {
      Q4T_CHECK(Ok(run.Finish(true, 0, {10, 0, 4}, 43, &wire)));
      Q4T_CHECK(wire.find("file_search_call.completed") == wire.npos);
    }
  }
  return true;
}

Q4T_TEST(
    responses_native_search_max_tool_calls_ignores_excess_and_disables_more) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture, 1));
  std::string wire;
  Q4T_CHECK(run.Append(NativeDelta(run, 0), "null", &wire) &&
            run.Append(NativeDelta(run, 1), "null", &wire));
  Calls calls;
  VectorSearchUsage usage;
  bool answer;
  Q4T_CHECK(
      Ok(run.RunFileSearches(2, calls.callbacks, &usage, &answer, Silent)) &&
      answer && calls.embeds == 1 && calls.reserves == 1);
  const auto& items = run.Output().Snapshot().GetArray("output")->array;
  Q4T_CHECK(items[0].GetString("status") == "completed" &&
            items[1].GetString("status") == "incomplete" &&
            items[1].Find("results")->IsNull());
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(10, NativeConfig(), {}, &next)) &&
            next.generation_request.GetString("tool_choice") == "none" &&
            next.generation_request.GetInt("max_completion_tokens") == 118);
  return true;
}

Q4T_TEST(
    responses_native_search_mixed_client_tool_waits_for_real_client_output) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  auto request = NativeRequest(fixture);
  Field(request, "tools")
      .array.push_back(
          Object({{"type", String("function")},
                  {"name", String("lookup")},
                  {"parameters",
                   Object({{"type", String("object")},
                           {"properties", Object()},
                           {"additionalProperties", Boolean(false)}})}}));
  Set(&request, "tool_choice", String("auto"));
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, request);
  std::string wire;
  Q4T_CHECK(run.Append(NativeDelta(run), "null", &wire));
  auto external = NativeDelta(run, 1, "{}");
  external.call.name = "lookup";
  Q4T_CHECK(run.Append(external, "null", &wire));
  Calls calls;
  VectorSearchUsage usage;
  bool answer;
  Q4T_CHECK(
      Ok(run.RunFileSearches(2, calls.callbacks, &usage, &answer, Silent)) &&
      !answer && calls.embeds == 1);
  Q4T_CHECK(Ok(run.Finish(false, 2, {40, 0, 10}, 43, &wire)));
  auto follow = Object(
      {{"input", Array({Object({{"type", String("function_call_output")},
                                {"call_id", String("native_call_1")},
                                {"output", String("actual client output")}})})},
       {"previous_response_id", String(run.Id())},
       {"reasoning", Object({{"effort", String("none")}})}});
  ResponsesExecution resumed(*fixture.objects);
  Q4T_CHECK(Ok(resumed.Prepare(follow, NativeConfig(), NativeAccess(), 44)) &&
            !resumed.PendingFileSearch() && !resumed.HasFileSearch() &&
            OutputJson(resumed.Generation().generation_request)
                    .find("actual client output") != std::string::npos &&
            calls.embeds == 1);
  return true;
}

Q4T_TEST(responses_native_search_failed_embedding_preserves_actual_usage) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  std::string wire;
  Q4T_CHECK(run.Append(NativeDelta(run, 0, R"({"queries":["PLE","SSD"]})"),
                       "null", &wire));
  Calls calls;
  calls.callbacks.embed = [&](const auto& tokens, auto* result) {
    if (++calls.embeds == 2) {
      result->committed_input = 3;
      return Error(503, "embedding_compute_failed", "CPU fault fixture");
    }
    return Embed(tokens, result);
  };
  VectorSearchUsage usage;
  bool answer;
  const auto result =
      run.RunFileSearches(1, calls.callbacks, &usage, &answer, Silent);
  Q4T_CHECK(result.code == "embedding_compute_failed" && calls.embeds == 2 &&
            usage.committed_input ==
                kVectorQueryInstruction.size() + 3 + 1 + 3 &&
            !usage.unknown && !answer);
  const auto& item = run.Output().Snapshot().GetArray("output")->array[0];
  Q4T_CHECK(item.GetString("status") == "failed" &&
            item.Find("results")->IsNull());
  return true;
}

Q4T_TEST(responses_native_search_exception_keeps_receipt_without_replay) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  std::string wire;
  Q4T_CHECK(run.Append(NativeDelta(run), "null", &wire));
  Calls calls;
  calls.callbacks.embed = [&](const auto&, auto* result) -> ObjectReply {
    ++calls.embeds;
    result->committed_input = 4;
    throw std::runtime_error("Injected embedding exception");
  };
  VectorSearchUsage usage;
  bool answer, thrown = false;
  try {
    (void)run.RunFileSearches(1, calls.callbacks, &usage, &answer, Silent);
  } catch (const std::runtime_error&) {
    thrown = true;
  }
  Q4T_CHECK(thrown && calls.embeds == 1 && usage.committed_input == 4 &&
            !usage.unknown && !answer);
  Q4T_CHECK(
      run.RunFileSearches(1, calls.callbacks, &usage, &answer, Silent).code ==
          "file_search_already_dispatched" &&
      calls.embeds == 1);
  return true;
}

Q4T_TEST(responses_native_search_subscriber_cancel_precedes_embedding) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  std::string wire;
  Q4T_CHECK(run.Append(NativeDelta(run), "null", &wire));
  Calls calls;
  VectorSearchUsage usage;
  bool answer;
  Q4T_CHECK(run.RunFileSearches(1, calls.callbacks, &usage, &answer,
                                [](const std::string&) { return false; })
                    .http_status == 499 &&
            !calls.embeds && !calls.reserves && !answer);
  return true;
}

Q4T_TEST(
    responses_native_search_fragments_and_identity_never_escape_as_function) {
  Fixture fixture;
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  std::string wire;
  auto delta = NativeDelta(run, 0, R"({"queries":)");
  Q4T_CHECK(run.Append(delta, "null", &wire));
  const auto before = run.RetainedInputBytes();
  delta.tool_start = false;
  delta.call.id.clear();
  delta.call.name.clear();
  delta.call.arguments = R"(["PLE"]})";
  Q4T_CHECK(run.Append(delta, "null", &wire) && wire.empty() &&
            run.RetainedInputBytes() > before);
  delta.call.name = "lookup";
  Q4T_CHECK(!run.Append(delta, "null", &wire) && wire.empty());
  return true;
}

Q4T_TEST(responses_native_search_parser_receipt_precedes_actual_execution) {
  Fixture fixture;
  fixture.Add("Source", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  const auto& request = run.Generation().generation_request;
  const auto* fn = request.GetArray("tools")->array[0].Find("function");
  ChatResponseParser parser;
  Q4T_CHECK(parser.Init(request,
                        "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                        "cpu_native_parser", false));
  Json args;
  Q4T_CHECK(Parse(R"({"queries":["PLE SSD"]})", &args));
  SchemaReceipt receipt;
  Q4T_CHECK(MakeSchemaReceipt(fn->GetString("name"), *fn->Find("parameters"),
                              args, &receipt));
  std::vector<SchemaReceipt> receipts{receipt};
  parser.SetSchemaReceipts(&receipts);
  const auto text = "<tool_call>\n<function=" + fn->GetString("name") +
                    ">\n<parameter=arguments>\n" + OutputJson(args) +
                    "\n</parameter>\n</function>\n</tool_call>";
  std::vector<ResponseDelta> deltas;
  std::string wire, all;
  for (const auto c : text) {
    deltas.clear();
    Q4T_CHECK(parser.Append(std::string(1, c), &deltas));
    for (const auto& delta : deltas) {
      Q4T_CHECK(run.Append(delta, "null", &wire));
      all += wire;
    }
  }
  deltas.clear();
  Q4T_CHECK(parser.Finish(false, &deltas) && parser.CompletedToolCalls() == 1);
  for (const auto& delta : deltas) Q4T_CHECK(run.Append(delta, "null", &wire));
  Q4T_CHECK(run.PendingFileSearch() &&
            all.find("function_call_arguments") == all.npos &&
            all.find(fn->GetString("name")) == all.npos);
  Calls calls;
  VectorSearchUsage usage;
  bool answer;
  Q4T_CHECK(Ok(run.RunFileSearches(parser.CompletedToolCalls(), calls.callbacks,
                                   &usage, &answer, Silent)) &&
            answer && calls.embeds == 1);
  return true;
}

Q4T_TEST(responses_native_search_citation_wire_and_previous_response_reuse) {
  for (const bool include : {false, true}) {
    Fixture fixture;
    const auto file =
        fixture.Add("PLE streams immutable tables from SSD.", true, 1024);
    const auto request = NativeRequest(fixture, 1, include);
    ResponsesExecution run(*fixture.objects);
    auto all = NativeBegin(run, request);
    std::string wire;
    Q4T_CHECK(run.Append(NativeDelta(run), "null", &wire));
    all += wire;
    Calls calls;
    VectorSearchUsage usage;
    bool answer;
    Q4T_CHECK(Ok(run.RunFileSearches(1, calls.callbacks, &usage, &answer,
                                     [&](const std::string& part) {
                                       all += part;
                                       return true;
                                     })) &&
              answer);
    Q4T_CHECK(run.Append(NativeText("PLE 从 SSD 流式读取。[file:" + file + ']'),
                         "null", &wire));
    all += wire;
    Q4T_CHECK(run.CiteFileSearchSources(&wire));
    all += wire;
    Q4T_CHECK(Ok(run.Finish(
        false, 0, {static_cast<int64_t>(usage.committed_input + 20), 0, 10}, 43,
        &wire)));
    all += wire;
    const auto& message = run.Output().Snapshot().GetArray("output")->array[1];
    Q4T_CHECK(message.GetArray("content")
                  ->array[0]
                  .GetArray("annotations")
                  ->array[0]
                  .GetString("file_id") == file);
    auto follow = Object({{"input", String("What was retrieved?")},
                          {"previous_response_id", String(run.Id())},
                          {"reasoning", Object({{"effort", String("none")}})}});
    ResponsesExecution resumed(*fixture.objects);
    Q4T_CHECK(Ok(resumed.Prepare(follow, NativeConfig(), NativeAccess(), 44)) &&
              !resumed.PendingFileSearch() && !resumed.HasFileSearch() &&
              OutputJson(resumed.Generation().generation_request)
                      .find("citation_marker") != std::string::npos &&
              calls.embeds == 1);
    const auto directory =
        std::filesystem::path(std::getenv("Q4T_TEST_OUTPUT_DIR")) /
        "native-file-search-sdk";
    std::filesystem::create_directories(directory);
    const auto prefix = directory / (include ? "included" : "hidden");
    std::ofstream json(prefix.string() + ".json");
    json << run.ResultJson() << '\n';
    std::ofstream events(prefix.string() + ".wire", std::ios::binary);
    events << run.PublicWire(all);
    std::ofstream asked(prefix.string() + ".request.json");
    asked << OutputJson(request) << '\n';
    Q4T_CHECK(json.good() && events.good() && asked.good());
  }
  return true;
}

Q4T_TEST(responses_native_search_prewarm_has_no_variable_tool_execution) {
  Fixture fixture;
  auto request = NativeRequest(fixture);
  Set(&request, "prompt_cache_key", String("project"));
  Set(&request, "prompt_cache_options", Object({{"prewarm", Boolean(true)}}));
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, request);
  Q4T_CHECK(run.HasFileSearch() && run.Envelope().cache.prewarm &&
            !run.VariableInputSteps() && !run.PendingFileSearch());
  std::string wire;
  Q4T_CHECK(Ok(run.Finish(false, 0, {20, 0, 0}, 43, &wire)) &&
            run.Output().Snapshot().GetArray("output")->array.empty());
  return true;
}

namespace {
void Must(q4t::Status status) {
  if (!status) throw std::runtime_error(status.message());
}
Json NativeReview(bool flagged) {
  ModerationPlan plan;
  plan.model = "cpu";
  plan.text = true;
  Json review;
  Must(ParseModerationResult(flagged
                                 ? R"({"scores":[1,0,0,0,0,0,0,0,0,0,0,0,0]})"
                                 : R"({"scores":[0,0,0,0,0,0,0,0,0,0,0,0,0]})",
                             plan, .5, &review));
  return review;
}
void NativeSearch(ResponsesExecution& run, Calls* calls,
                  std::string* published = nullptr) {
  std::string wire;
  Must(run.Append(NativeDelta(run), "null", &wire));
  if (published) *published += wire;
  bool answer = false;
  VectorSearchUsage usage;
  Require(run.RunFileSearches(1, calls->callbacks, &usage, &answer,
                              [&](const std::string& part) {
                                if (published) *published += part;
                                return true;
                              }));
  if (!answer) throw std::runtime_error("Native continuation fixture");
}
bool NativeWait(const std::function<bool()>& condition) {
  const auto until = Clock::now() + 5s;
  while (!condition() && Clock::now() < until) std::this_thread::sleep_for(1ms);
  return condition();
}
struct NativeClients {
  ClientAdmission clients;
  std::string token = std::string(64, 'a');
  NativeClients() {
    GatewayClientPolicy policy;
    Must(ParseGatewayClientPolicy(
        R"({"version":1,"gateway_token":")" + token +
            R"(","clients":[{"id":"search","max_inflight":2,)"
            R"("max_control_inflight":2,"max_body_bytes":1048576,)"
            R"("max_prompt_tokens":32768,"max_output_tokens":256}]})",
        &policy));
    Must(clients.Configure(std::move(policy)));
  }
  ObjectReply Submit(ResponsesJobs& jobs, Fixture& fixture) {
    const auto identity = clients.Authenticate("search", token);
    ClientLease lease;
    if (!lease.Acquire(clients, identity, ClientLane::kData))
      throw std::runtime_error("Native search job permit");
    auto request = NativeRequest(fixture);
    Set(&request, "background", Boolean(true));
    auto run = std::make_unique<ResponsesExecution>(*fixture.objects);
    Require(run->Prepare(request, NativeConfig(), NativeAccess(),
                         std::time(nullptr), true));
    return jobs.Submit(std::move(run), identity, [&](ClientLease& target) {
      return lease.TransferTo(target);
    });
  }
};
BatchTokenCodec NativeCodec() {
  BatchTokenCodec codec;
  codec.encode = [](std::string_view text, std::vector<uint32_t>* tokens) {
    tokens->assign(text.begin(), text.end());
    return q4t::Status{};
  };
  codec.decode = [](const std::vector<uint32_t>& tokens, std::string* text) {
    text->assign(tokens.begin(), tokens.end());
    return q4t::Status{};
  };
  return codec;
}
}  // namespace

Q4T_TEST(
    responses_search_moderation_uses_public_queries_and_result_projection) {
  for (const bool include : {false, true}) {
    Fixture fixture;
    fixture.Add("ACTUAL_PRIVATE_SOURCE", true, 1024);
    ResponsesExecution run(*fixture.objects);
    NativeBegin(run, NativeRequest(fixture, 1, include));
    Calls calls;
    NativeSearch(run, &calls);
    std::string wire;
    Q4T_CHECK(run.Append(NativeText("Actual answer"), "null", &wire));
    ModerationContent content;
    ChatPlanError error;
    Q4T_CHECK(CollectModerationOutput(*run.PublicResponse().GetArray("output"),
                                      &content, &error));
    const auto text = OutputJson(content.records);
    Q4T_CHECK(text.find("PLE SSD") != text.npos &&
              text.find("Actual answer") != text.npos &&
              (text.find("ACTUAL_PRIVATE_SOURCE") != text.npos) == include);
  }
  return true;
}

Q4T_TEST(responses_search_moderation_rejects_malformed_or_unbounded_passages) {
  auto item = Object({{"type", String("file_search_call")},
                      {"id", String("fs_actual")},
                      {"status", String("completed")},
                      {"queries", Array({String("query")})},
                      {"results", Array()}});
  ModerationContent content;
  ChatPlanError error;
  auto bad = item;
  Set(&bad, "queries", Array({Number(1)}));
  Q4T_CHECK(!CollectModerationOutput(Array({bad}), &content, &error));
  Set(&item, "results",
      Array({Object({{"text", String(std::string(1024 * 1024 + 1, 'x'))}})}));
  Q4T_CHECK(!CollectModerationOutput(Array({item}), &content, &error) &&
            content.records.IsNull());
  return true;
}

Q4T_TEST(responses_search_review_hold_hides_retrieval_until_actual_approval) {
  Fixture fixture;
  fixture.Add("PRIVATE_SOURCE", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture, 1, false));
  Q4T_CHECK(Ok(run.HoldUnreviewedOutput()));
  Calls calls;
  std::string wire;
  NativeSearch(run, &calls, &wire);
  Q4T_CHECK(run.Append(NativeText("Actual answer"), "null", &wire) &&
            wire.empty());
  const auto before =
      ResponsesState(*fixture.objects).Retrieve(run.Id(), NativeAccess());
  Q4T_CHECK(Ok(before) && before.value.GetArray("output")->array.empty());
  Q4T_CHECK(Ok(run.PublishReviewedOutput(NativeReview(false), &wire)) &&
            wire.find("PRIVATE_SOURCE") != wire.npos &&
            run.PublicWire(wire).find("PRIVATE_SOURCE") == wire.npos &&
            run.PublicWire(wire).find("Actual answer") != wire.npos);
  Q4T_CHECK(Ok(run.Finish(false, 0, {40, 0, 5}, 43, &wire)));
  return true;
}

Q4T_TEST(
    responses_search_review_block_discards_private_results_without_replay) {
  Fixture fixture;
  fixture.Add("PRIVATE_BLOCKED_SOURCE", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture));
  Q4T_CHECK(Ok(run.HoldUnreviewedOutput()));
  Calls calls;
  std::string wire;
  NativeSearch(run, &calls, &wire);
  Q4T_CHECK(wire.empty() &&
            run.PublishReviewedOutput(NativeReview(true), &wire).http_status ==
                403);
  run.DiscardUnreviewedOutput();
  Q4T_CHECK(Ok(run.Finish(true, 0, {40, 0, 5}, 43, &wire, true)) &&
            run.Output().Snapshot().GetArray("output")->array.empty() &&
            wire.find("PRIVATE_BLOCKED_SOURCE") == wire.npos &&
            calls.embeds == 1);
  return true;
}

Q4T_TEST(
    responses_search_queued_restore_executes_once_and_started_cannot_resume) {
  Fixture fixture;
  fixture.Add("Actual queued source", true, 1024);
  std::string id;
  {
    ResponsesExecution queued(*fixture.objects);
    auto request = NativeRequest(fixture);
    Set(&request, "background", Boolean(true));
    Q4T_CHECK(
        Ok(queued.Prepare(request, NativeConfig(), NativeAccess(), 42, true)));
    std::string wire;
    Q4T_CHECK(Ok(queued.Begin(&wire)));
    id = queued.Id();
  }
  fixture.objects.reset();
  fixture.objects = std::make_unique<ObjectStore>(fixture.options);
  ResponsesExecution restored(*fixture.objects);
  Q4T_CHECK(Ok(restored.RestoreQueued(id, NativeConfig(), NativeAccess())) &&
            restored.HasFileSearch() && !restored.PendingFileSearch());
  std::string wire;
  Q4T_CHECK(Ok(restored.StartRunning(&wire)));
  Calls calls;
  NativeSearch(restored, &calls);
  Q4T_CHECK(Ok(restored.Checkpoint()));
  ResponsesExecution refused(*fixture.objects);
  Q4T_CHECK(refused.RestoreQueued(id, NativeConfig(), NativeAccess()).code ==
                "response_not_queued" &&
            calls.embeds == 1);
  Q4T_CHECK(restored.Append(NativeText("Answered"), "null", &wire) &&
            Ok(restored.Finish(false, 0, {40, 0, 5}, 44, &wire)));
  return true;
}

Q4T_TEST(
    responses_search_background_cancel_closes_pending_tool_without_execute) {
  Fixture fixture;
  NativeClients clients;
  std::atomic<bool> pending = false;
  ResponsesJobs jobs(*fixture.objects, {}, [&](auto& job) {
    std::string wire;
    Must(job.run->Append(NativeDelta(*job.run), "null", &wire));
    Require(job.run->Checkpoint());
    pending = true;
    if (!NativeWait([&] { return job.control->Cancelled(); }))
      throw std::runtime_error("Expected explicit native search cancellation");
  });
  const auto submitted = clients.Submit(jobs, fixture);
  Q4T_CHECK(Ok(submitted) && NativeWait([&] { return pending.load(); }));
  const auto cancelled =
      jobs.Cancel(submitted.value.GetString("id"), NativeAccess());
  Q4T_CHECK(Ok(cancelled) &&
            cancelled.value.GetString("status") == "cancelled");
  const auto& item = cancelled.value.GetArray("output")->array[0];
  Q4T_CHECK(item.GetString("status") == "incomplete" &&
            item.Find("results")->IsNull() &&
            NativeWait([&] { return jobs.Health().owned == 0; }));
  return true;
}

Q4T_TEST(responses_search_background_owner_completes_actual_single_search) {
  Fixture fixture;
  fixture.Add("Actual background source", true, 1024);
  NativeClients clients;
  std::atomic<int> executed = 0;
  ResponsesJobs jobs(*fixture.objects, {}, [&](auto& job) {
    Calls calls;
    NativeSearch(*job.run, &calls);
    executed += calls.embeds;
    std::string wire;
    Must(job.run->Append(NativeText("Background answer"), "null", &wire));
    Require(job.run->Finish(false, 0, {40, 0, 5}, std::time(nullptr), &wire));
  });
  const auto submitted = clients.Submit(jobs, fixture);
  Q4T_CHECK(Ok(submitted) &&
            NativeWait([&] { return jobs.Health().owned == 0; }));
  const auto reply =
      ResponsesState(*fixture.objects)
          .Retrieve(submitted.value.GetString("id"), NativeAccess());
  Q4T_CHECK(Ok(reply) && reply.value.GetString("status") == "completed" &&
            reply.value.GetArray("output")->array[0].GetString("status") ==
                "completed" &&
            executed == 1);
  return true;
}

Q4T_TEST(
    responses_search_batch_frozen_input_preserves_control_without_execution) {
  Fixture fixture;
  fixture.Add("Actual Batch source", true, 1024);
  auto request = NativeRequest(fixture, 2);
  Set(&request, "stream", Boolean(false));
  std::erase_if(request.object, [](const auto& pair) {
    return pair.first == "stream_options";
  });
  BatchInputRow row;
  row.line = 1;
  row.custom_id = "native-query";
  row.body = request;
  BatchPreparedInput prepared;
  BatchPreparation preparation(*fixture.objects);
  Q4T_CHECK(
      Ok(preparation.Prepare(Access(), "batch_native_fixture", "/v1/responses",
                             row, NativeConfig(), &prepared)) &&
      !prepared.inline_input.IsNull());
  Json frozen;
  Q4T_CHECK(Ok(preparation.Read(
      Access(), "batch_native_fixture", "/v1/responses", row,
      Object({{"prepared_inline", prepared.inline_input}}), &frozen)));
  ResponsesExecution run(*fixture.objects);
  Q4T_CHECK(Ok(run.RestoreInputs(frozen, NativeConfig(), NativeAccess(), 43)) &&
            run.HasFileSearch() && run.VariableInputSteps() &&
            !run.PendingFileSearch() && !run.Begun() &&
            run.Envelope().options.GetInt("max_tool_calls") == 2);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Calls calls;
  NativeSearch(run, &calls);
  Q4T_CHECK(calls.embeds == 1 &&
            run.Append(NativeText("Batch answer"), "null", &wire) &&
            Ok(run.Finish(false, 0, {40, 0, 5}, 44, &wire)));
  return true;
}

Q4T_TEST(
    responses_search_anonymous_batch_budget_covers_queries_and_continuations) {
  Fixture fixture;
  auto config = NativeConfig();
  config.max_len = 8192;
  for (const bool prewarm : {false, true}) {
    auto request = NativeRequest(fixture, 2);
    if (prewarm) {
      Set(&request, "prompt_cache_key", String("project"));
      Set(&request, "prompt_cache_options",
          Object({{"prewarm", Boolean(true)}}));
    }
    ResponsesExecution run(*fixture.objects);
    Q4T_CHECK(Ok(run.Prepare(request, config, NativeAccess(), 42)));
    BatchRateBudget rate;
    Q4T_CHECK(Ok(MeasureBatchResponseRate(run, config, Access(), NativeCodec(),
                                          &rate)) &&
              !run.Begun() && !run.PendingFileSearch());
    Q4T_CHECK(rate.input_exact == prewarm &&
              rate.output == (prewarm ? 0 : 128));
    if (!prewarm)
      Q4T_CHECK(rate.prompt_capacity == 8 * 8192 + 2 * 16 * 8192);
    else
      Q4T_CHECK(rate.prompt_capacity < 8192);
  }
  return true;
}

Q4T_TEST(responses_search_client_batch_keeps_shared_cumulative_input_ceiling) {
  Fixture fixture;
  ClientLimits limits;
  limits.max_prompt_tokens = 32768;
  limits.max_output_tokens = 256;
  limits.max_body_bytes = 1 << 20;
  auto config = NativeConfig();
  config.max_len = 8192;
  config.client_limits = &limits;
  ResponsesExecution run(*fixture.objects);
  Q4T_CHECK(
      Ok(run.Prepare(NativeRequest(fixture, 2), config, NativeAccess(), 42)));
  BatchRateBudget rate;
  Q4T_CHECK(Ok(MeasureBatchResponseRate(run, config, Access(), NativeCodec(),
                                        &rate)) &&
            !rate.input_exact && rate.prompt_capacity == 32768 &&
            rate.output == 128 && rate.auxiliary_output == 0 && !run.Begun());
  return true;
}

Q4T_TEST(responses_search_encrypted_history_keeps_sources_without_reexecution) {
  Fixture fixture;
  fixture.Add("Actual encrypted context source", true, 1024);
  auto request = NativeRequest(fixture);
  Set(&request, "store", Boolean(false));
  Set(&request, "reasoning", Object({{"effort", String("low")}}));
  Set(&request, "include",
      Array({String("reasoning.encrypted_content"),
             String("file_search_call.results")}));
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, request);
  auto notes = NativeText("Actual reasoning before search");
  notes.kind = ResponseDelta::Kind::kReasoning;
  std::string wire;
  Q4T_CHECK(run.Append(notes, "null", &wire));
  Calls calls;
  NativeSearch(run, &calls);
  Q4T_CHECK(run.Append(NativeText("Answer from source"), "null", &wire) &&
            Ok(run.Finish(false, 0, {40, 0, 10, 3}, 43, &wire)));
  auto input = *run.Output().Snapshot().GetArray("output");
  Q4T_CHECK(!input.array[0].GetString("encrypted_content").empty());
  std::erase_if(input.array[0].object,
                [](const auto& field) { return field.first == "content"; });
  input.array.push_back(
      Object({{"role", String("user")},
              {"content", String("Use the retrieved source")}}));
  Set(&request, "input", input);
  ResponsesExecution follow(*fixture.objects);
  Q4T_CHECK(Ok(follow.Prepare(request, NativeConfig(), NativeAccess(), 44)) &&
            !follow.PendingFileSearch() && !follow.Begun());
  const auto actual = OutputJson(follow.Generation().generation_request);
  Q4T_CHECK(actual.find("Actual reasoning before search") != actual.npos &&
            actual.find("Actual encrypted context source") != actual.npos &&
            calls.embeds == 1);
  return true;
}

Q4T_TEST(responses_search_pro_summary_and_schema_keep_actual_notes_and_limits) {
  Fixture fixture;
  fixture.Add("Actual structured source", true, 1024);
  auto request = NativeRequest(fixture, 1);
  Set(&request, "reasoning",
      Object({{"effort", String("low")},
              {"mode", String("pro")},
              {"summary", String("concise")}}));
  Set(&request, "text",
      Object(
          {{"format",
            Object({{"type", String("json_schema")},
                    {"name", String("answer")},
                    {"strict", Boolean(true)},
                    {"schema",
                     Object({{"type", String("object")},
                             {"properties",
                              Object({{"answer",
                                       Object({{"type", String("string")}})}})},
                             {"required", Array({String("answer")})},
                             {"additionalProperties", Boolean(false)}})}})}}));
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, request);
  ResponsesSummaryPlan pro;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesDeliberation(run.Generation(), 128, 0, NativeConfig(),
                                      &pro, &error) &&
            pro.request.GetString("tool_choice") == "none");
  auto notes = NativeText("Actual notes must survive search");
  notes.kind = ResponseDelta::Kind::kReasoning;
  std::string wire;
  Q4T_CHECK(run.Append(notes, "null", &wire));
  Calls calls;
  NativeSearch(run, &calls);
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(12, NativeConfig(), {}, &next)) &&
            next.generation_request.GetInt("max_completion_tokens") == 116 &&
            next.generation_request.GetString("tool_choice") == "none");
  const auto actual = OutputJson(next.generation_request);
  Q4T_CHECK(
      actual.find("Actual notes must survive search") != actual.npos &&
      actual.find("Actual structured source") != actual.npos &&
      next.generation_request.Find("response_format")->GetString("type") ==
          "json_schema");
  Q4T_CHECK(run.Append(NativeText(R"({"answer":"SSD"})"), "null", &wire));
  ResponsesSummaryPlan summary;
  Q4T_CHECK(PlanResponsesSummary(run.Envelope(), run.Output().Snapshot(), 128,
                                 20, NativeConfig(), &summary, &error) &&
            summary.needed);
  Q4T_CHECK(run.AppendSummary("Used actual source", &wire) &&
            Ok(run.Finish(false, 0, {40, 0, 24, 5}, 43, &wire)));
  return true;
}

Q4T_TEST(
    responses_search_multiple_rounds_reuse_model_indices_without_tool_replay) {
  Fixture fixture;
  fixture.Add("Actual multi-round source", true, 1024);
  ResponsesExecution run(*fixture.objects);
  NativeBegin(run, NativeRequest(fixture, 2));
  Calls calls;
  NativeSearch(run, &calls);
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(5, NativeConfig(), {}, &next)));
  NativeSearch(run, &calls);
  Q4T_CHECK(Ok(run.ContinueFileSearch(10, NativeConfig(), {}, &next)) &&
            next.generation_request.GetString("tool_choice") == "none" &&
            next.generation_request.GetInt("max_completion_tokens") == 118);
  std::string wire;
  Q4T_CHECK(run.Append(NativeText("Two actual searches"), "null", &wire) &&
            Ok(run.Finish(false, 0, {80, 0, 15}, 43, &wire)) &&
            calls.embeds == 2);
  const auto& output = run.Output().Snapshot().GetArray("output")->array;
  Q4T_CHECK(output.size() == 3 &&
            output[0].GetString("status") == "completed" &&
            output[1].GetString("status") == "completed" &&
            output[0].GetString("id") != output[1].GetString("id"));
  return true;
}
