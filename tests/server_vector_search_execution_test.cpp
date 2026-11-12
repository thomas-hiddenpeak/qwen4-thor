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
                  bool complete = true) {
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
    VectorStores stores(*objects);
    Require(stores.Attach(Access(), id, Object({{"file_id", String(file)}})));
    if (!complete) return file;
    VectorIngestionClaim claim;
    Require(stores.Claim(Access(), id, file, "cpu-query-fixture", 60, &claim));
    VectorIngestionOptions options;
    options.dimensions = 32;
    VectorIngestionPrepared prepared;
    Require(PrepareVectorIngestion(*objects, Access(), claim, options, Bytes,
                                   &prepared));
    VectorIngestionResult ingested;
    Require(ExecuteVectorIngestion(
        *objects, Access(), prepared, [](auto) { return ObjectReply{}; },
        [](auto tokens, auto* vector, auto* used) {
          vector->assign(32, 0);
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
