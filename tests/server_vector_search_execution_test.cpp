#include "q4t/server/vector_search_execution.h"

#include <algorithm>
#include <barrier>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/files.h"
#include "q4t/server/vector_search_api.h"
#include "q4t/server/client_request.h"
#include "q4t/server/http_connection.h"
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
  for (const auto& ids :
       {std::vector<std::string>{}, {fixture.id, fixture.id}, {"vs_../x"},
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
  auto reply = Multi(fixture, {first, fixture.id}, calls, &usage, MultiPlan(),
                     access);
  Q4T_CHECK(reply.http_status == 499 && !reply.value.Has("data") &&
            calls.embeds == 1 && usage.committed_input > 0);
  access.cancelled = {};
  access.deadline = Clock::now() - 1ms;
  reply = Multi(fixture, {first, fixture.id}, calls, &usage, MultiPlan(), access);
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
