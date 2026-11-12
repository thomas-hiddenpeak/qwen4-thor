#include "q4t/server/vector_ingestion.h"

#include <cstdlib>
#include <filesystem>
#include <numeric>

#include "../src/server/object_protocol.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/server/files.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!out) throw std::runtime_error("Missing ingestion output directory");
    auto path = (std::filesystem::path(out) / "ingestion-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Ingestion fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:ingestion") {
  return {std::move(scope), Clock::now() + 20s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
VectorIngestionClaim Seed(ObjectStore& objects, std::string text,
                          std::string filename = "MODEL.md") {
  const auto wire =
      "--f\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nuser_data\r\n"
      "--f\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" +
      filename + "\"\r\nContent-Type: application/octet-stream\r\n\r\n" + text +
      "\r\n--f--\r\n";
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=f");
  if (!Ok(upload.Feed(wire, Access().deadline)))
    throw std::runtime_error("Feed");
  auto source = upload.Finish();
  if (!Ok(source)) throw std::runtime_error("Finish");
  VectorStores stores(objects);
  auto root = stores.Create(Access(), Object());
  if (!Ok(root)) throw std::runtime_error("Store");
  auto attached = stores.Attach(
      Access(), root.value.GetString("id"),
      Object({{"file_id", String(source.value.GetString("id"))},
              {"chunking_strategy",
               Object({{"type", String("static")},
                       {"static",
                        Object({{"max_chunk_size_tokens", Number(100)},
                                {"chunk_overlap_tokens", Number(50)}})}})}}));
  if (!Ok(attached)) throw std::runtime_error("Attach");
  VectorIngestionClaim claim;
  if (!Ok(stores.Claim(Access(), root.value.GetString("id"),
                       source.value.GetString("id"), "owned-executor", 60,
                       &claim)))
    throw std::runtime_error("Claim");
  return claim;
}
// Explicit CPU byte-token fixture, never a native model/tokenizer claim.
ObjectReply Bytes(std::string_view text, const VectorSearchControl& control,
                  VectorEncodedText* out) {
  return EncodeVectorText(
      text,
      [](std::string_view source, std::vector<uint32_t>* tokens) {
        tokens->clear();
        for (unsigned char c : source) tokens->push_back(c);
        return q4t::Status{};
      },
      [](uint32_t token, std::string* piece) {
        *piece = std::string(1, static_cast<char>(token));
        return q4t::Status{};
      },
      control, out);
}
VectorIngestionOptions Options() {
  VectorIngestionOptions options;
  options.dimensions = 32;
  return options;
}
ObjectReply Embed(std::span<const uint32_t> tokens, std::vector<float>* vector,
                  uint64_t* count) {
  if (tokens.empty() || tokens.back() != 151643)
    return Error(500, "fixture_eos", "Missing EOS");
  vector->assign(32, 0);
  vector->front() = 1;
  *count = tokens.size();
  return {};
}
struct Injection {
  explicit Injection(std::string entry) { ObjectWorkerEntryForTesting(entry); }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
}  // namespace

#ifdef Q4T_EMBEDDING_ASSET_WORKER_ENTRY
Q4T_TEST(
    vector_ingestion_real_tokenizer_preserves_source_identity_and_exact_eos_cost) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  std::string source;
  for (int i = 0; i < 50; ++i)
    source += "项目资料 cafe\xcc\x81 PLE 与工具调用。\n";
  const auto claim = Seed(objects, source);
  const auto* configured = std::getenv("Q4T_EMBEDDING_ASSETS");
  const auto directory = std::filesystem::absolute(
      configured ? configured : ".q4t-work/native-embedding-20261111/assets");
  EmbeddingHostAssets assets;
  GrammarControl control;
  control.compile_deadline = Clock::now() + 15s;
  Q4T_CHECK(LoadEmbeddingAssets(directory, &assets, control, nullptr, false));
  VectorIngestionPrepared prepared;
  Q4T_CHECK(Ok(PrepareVectorIngestion(
      objects, Access(), claim, Options(),
      [&](auto text, const auto& budget, auto* output) {
        return EncodeVectorText(
            text,
            [&](auto raw, auto* tokens) {
              q4t::text::EncodeOptions options;
              options.add_special_tokens = false;
              options.cancelled = budget.cancelled;
              return assets.tokenizer->Encode(raw, tokens, options);
            },
            [&](uint32_t token, auto* piece) {
              return assets.tokenizer->Decode({token}, false, piece);
            },
            budget, output);
      },
      &prepared)));
  Q4T_CHECK(prepared.identity.source_sha256 == Digest(source) &&
            prepared.identity.text_sha256 == Digest(prepared.encoded.text) &&
            prepared.identity.source_sha256 != prepared.identity.text_sha256 &&
            prepared.encoded.text.find("caf\xc3\xa9") != std::string::npos);
  uint64_t reserved = 0, consumed = 0;
  VectorIngestionResult result;
  Q4T_CHECK(Ok(ExecuteVectorIngestion(
      objects, Access(), prepared,
      [&](uint64_t count) {
        reserved = count;
        return ObjectReply{};
      },
      [&](auto tokens, auto* vector, auto* count) {
        auto reply = Embed(tokens, vector, count);
        consumed += *count;
        return reply;
      },
      &result)));
  Q4T_CHECK(result.published && reserved == consumed &&
            consumed == prepared.input_tokens &&
            consumed == result.committed_input &&
            consumed > prepared.encoded.tokens.size() && result.hold.released);
  VectorStores stores(objects);
  Q4T_CHECK(stores.RetrieveFile(Access(), claim.store_id, claim.file_id)
                .value.GetString("status") == "completed");
  return true;
}
#endif

Q4T_TEST(vector_ingestion_actual_utf16_csv_and_signature_extraction) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const std::string source("\xff\xfeP\0L\0E\0", 8);
  const auto claim = Seed(objects, source, "../../source.txt");
  VectorIngestionPrepared prepared;
  Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), claim, Options(),
                                      Bytes, &prepared)));
  Q4T_CHECK(prepared.encoded.text == "PLE" &&
            prepared.identity.source_sha256 == Digest(source));
  const auto csv = Seed(objects, "column,value\nname,\"a,b\"\n", "data.csv");
  Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), csv, Options(), Bytes,
                                      &prepared)));
  Q4T_CHECK(prepared.encoded.text.find("\"a,b\"") != std::string::npos &&
            prepared.encoded.text.find("column") != std::string::npos);
  return true;
}

Q4T_TEST(
    vector_ingestion_rejects_incomplete_rows_and_empty_content_before_encoding) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  std::string csv = "header\n";
  for (int i = 0; i < 1001; ++i) csv += std::to_string(i) + "\n";
  FileInputContent ordinary;
  ordinary.bytes = csv;
  ordinary.filename = "long.csv";
  FileDocument window;
  Q4T_CHECK(
      Ok(ExtractFileDocument(ordinary, {}, false, "low", Access(), &window)));
  Q4T_CHECK(window.sheets.size() == 1 && window.sheets.front().source_rows >
                                             window.sheets.front().rows.size());
  size_t encoded = 0;
  auto encode = [&](auto text, const auto& control, auto* out) {
    ++encoded;
    return Bytes(text, control, out);
  };
  VectorIngestionPrepared prepared;
  const auto claim = Seed(objects, csv, "long.csv");
  Q4T_CHECK(PrepareVectorIngestion(objects, Access(), claim, Options(), encode,
                                   &prepared)
                .http_status == 413);
  Q4T_CHECK(encoded == 0 && prepared.encoded.tokens.empty());
  const auto empty = Seed(objects, "", "empty.txt");
  Q4T_CHECK(!PrepareVectorIngestion(objects, Access(), empty, Options(), encode,
                                    &prepared)
                 .ok());
  Q4T_CHECK(encoded == 0 && prepared.encoded.tokens.empty());
  return true;
}

Q4T_TEST(vector_ingestion_scope_source_and_cancel_checks_precede_embedding) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto claim = Seed(objects, std::string(250, 'a'));
  VectorIngestionPrepared prepared;
  Q4T_CHECK(PrepareVectorIngestion(objects, Access("other"), claim, Options(),
                                   Bytes, &prepared)
                .http_status == 404);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(PrepareVectorIngestion(objects, cancelled, claim, Options(), Bytes,
                                   &prepared)
                .http_status == 499);
  Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), claim, Options(),
                                      Bytes, &prepared)));
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), claim.file_id)));
  size_t reservations = 0;
  VectorIngestionResult result;
  Q4T_CHECK(ExecuteVectorIngestion(
                objects, Access(), prepared,
                [&](uint64_t) {
                  ++reservations;
                  return ObjectReply{};
                },
                Embed, &result)
                .http_status == 404);
  Q4T_CHECK(reservations == 0 && result.committed_input == 0 &&
            result.snapshot.id.empty());
  return true;
}

Q4T_TEST(
    vector_ingestion_reserves_full_cost_before_any_model_and_preserves_partial_usage) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto claim = Seed(objects, std::string(251, 'a'));
  VectorIngestionPrepared prepared;
  Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), claim, Options(),
                                      Bytes, &prepared)));
  size_t steps = 0;
  uint64_t cost = 0;
  auto partial = [&](auto input, auto* vector, auto* count) {
    ++steps;
    auto reply = Embed(input, vector, count);
    return steps == 2 ? Error(503, "fixture_device_oom", "Explicit CPU failure")
                      : reply;
  };
  VectorIngestionResult result;
  Q4T_CHECK(ExecuteVectorIngestion(
                objects, Access(), prepared,
                [&](uint64_t tokens) {
                  cost = tokens;
                  return Error(429, "client_tpm_exhausted", "Quota");
                },
                partial, &result)
                .http_status == 429);
  Q4T_CHECK(cost == prepared.input_tokens && steps == 0 && !result.published);
  Q4T_CHECK(ExecuteVectorIngestion(
                objects, Access(), prepared,
                [](uint64_t) { return ObjectReply{}; }, partial, &result)
                .code == "fixture_device_oom");
  Q4T_CHECK(steps == 2 && result.committed_input == 202 &&
            result.snapshot.id.empty());
  Q4T_CHECK(ExecuteVectorIngestion(
                objects, Access(), prepared,
                [](uint64_t) { return ObjectReply{}; }, partial, &result)
                .code == "vector_ingestion_already_executed");
  Q4T_CHECK(steps == 2 && result.committed_input == 202);
  Q4T_CHECK(VectorStores(objects)
                .RetrieveFile(Access(), claim.store_id, claim.file_id)
                .value.GetString("status") == "in_progress");
  return true;
}

Q4T_TEST(
    vector_ingestion_cancel_after_committed_model_keeps_usage_without_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto claim = Seed(objects, std::string(251, 'a'));
  VectorIngestionPrepared prepared;
  Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), claim, Options(),
                                      Bytes, &prepared)));
  bool cancelled = false;
  auto access = Access();
  access.cancelled = [&] { return cancelled; };
  VectorIngestionResult result;
  auto reply = ExecuteVectorIngestion(
      objects, access, prepared, [](uint64_t) { return ObjectReply{}; },
      [&](auto tokens, auto* vector, auto* used) {
        auto step = Embed(tokens, vector, used);
        cancelled = true;
        return step;
      },
      &result);
  Q4T_CHECK(reply.http_status == 499 && result.committed_input == 101 &&
            !result.published && result.snapshot.id.empty());
  Q4T_CHECK(Ok(VectorStores(objects).Cancel(Access(), claim)));
  return true;
}

Q4T_TEST(
    vector_ingestion_unknown_snapshot_commit_is_reconciled_without_model_replay) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  VectorIngestionPrepared prepared;
  {
    ObjectStore objects(fixture.options);
    const auto claim = Seed(objects, std::string(251, 'a'));
    Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), claim, Options(),
                                        Bytes, &prepared)));
  }
  Injection injection("--q4t-object-worker-snapshot-lost-ack");
  ObjectStore objects(fixture.options);
  size_t steps = 0;
  VectorIngestionResult result;
  const auto reply = ExecuteVectorIngestion(
      objects, Access(), prepared, [](uint64_t) { return ObjectReply{}; },
      [&](auto tokens, auto* vector, auto* used) {
        ++steps;
        return Embed(tokens, vector, used);
      },
      &result);
  Q4T_CHECK(reply.commit_unknown && !result.snapshot.id.empty() &&
            result.hold.commit_unknown && !result.published);
  const auto before = steps;
  Q4T_CHECK(Ok(VectorStores(objects).Finish(
      Access(), prepared.claim, prepared.identity, result.snapshot)));
  Q4T_CHECK(steps == before && result.committed_input == prepared.input_tokens);
  return true;
}

Q4T_TEST(
    vector_ingestion_unknown_parent_commit_retains_original_hold_and_published_index) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  VectorIngestionPrepared prepared;
  {
    ObjectStore objects(fixture.options);
    const auto claim = Seed(objects, std::string(251, 'a'));
    Q4T_CHECK(Ok(PrepareVectorIngestion(objects, Access(), claim, Options(),
                                        Bytes, &prepared)));
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(fixture.options);
  VectorIngestionResult result;
  const auto reply = ExecuteVectorIngestion(
      objects, Access(), prepared, [](uint64_t) { return ObjectReply{}; },
      Embed, &result);
  Q4T_CHECK(reply.commit_unknown && !result.snapshot.id.empty() &&
            !result.hold.released && !result.published);
  VectorStores stores(objects);
  Q4T_CHECK(stores
                .RetrieveFile(Access(), prepared.claim.store_id,
                              prepared.claim.file_id)
                .value.GetString("status") == "completed");
  Q4T_CHECK(result.committed_input == prepared.input_tokens);
  Q4T_CHECK(Ok(InputSnapshotStore(objects).Release(Access(), &result.hold)));
  return true;
}
