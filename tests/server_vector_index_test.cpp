#include "q4t/server/vector_index.h"

#include <bit>
#include <cstdlib>
#include <filesystem>
#include <numeric>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/server/http_response.h"
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
    if (!out) throw std::runtime_error("Missing index test output directory");
    auto path = (std::filesystem::path(out) / "index-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Index fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:index") {
  return {std::move(scope), Clock::now() + 15s, {}};
}
VectorIndexIdentity Identity(std::string_view text) {
  return {"file_document",
          "MODEL.md",
          Digest(text),
          Digest(text),
          VectorEmbeddingIdentity(32),
          32,
          {100, 50}};
}
std::vector<size_t> Boundaries(size_t bytes) {
  std::vector<size_t> result(bytes + 1);
  std::iota(result.begin(), result.end(), 0);
  return result;
}
ObjectReply Embed(std::span<const uint32_t> tokens, std::vector<float>* vector,
                  uint64_t* used) {
  if (tokens.empty() || tokens.back() != 151643)
    throw std::runtime_error("Missing trained EOS");
  *used = tokens.size();
  vector->assign(32, 0);
  (*vector)[0] = 1;
  (*vector)[1] = std::bit_cast<float>(uint32_t(0x80000000));  // Preserve -0.
  (*vector)[2] = std::bit_cast<float>(uint32_t(1));  // Preserve subnormal.
  return {};
}
VectorIndex Index() {
  const std::string text(251, 'a');
  const std::vector<uint32_t> tokens(text.size(), 64);
  const auto boundaries = Boundaries(text.size());
  VectorIndex index;
  uint64_t used = 0;
  if (!BuildVectorIndex(Identity(text), text, tokens, boundaries, Embed, {},
                        &index, &used)
           .ok())
    throw std::runtime_error("Index fixture build failed");
  return index;
}
bool Same(const VectorIndex& a, const VectorIndex& b) {
  if (a.text != b.text || a.input_tokens != b.input_tokens ||
      a.chunks.size() != b.chunks.size() ||
      VectorIndexBinding(a.identity) != VectorIndexBinding(b.identity))
    return false;
  for (size_t i = 0; i < a.chunks.size(); ++i) {
    if (a.chunks[i].range.byte_begin != b.chunks[i].range.byte_begin ||
        a.chunks[i].range.token_end != b.chunks[i].range.token_end ||
        a.chunks[i].embedding.size() != b.chunks[i].embedding.size())
      return false;
    for (size_t j = 0; j < a.chunks[i].embedding.size(); ++j)
      if (std::bit_cast<uint32_t>(a.chunks[i].embedding[j]) !=
          std::bit_cast<uint32_t>(b.chunks[i].embedding[j]))
        return false;
  }
  return true;
}
struct Injection {
  explicit Injection(std::string entry) {
    ObjectWorkerEntryForTesting(std::move(entry));
  }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
}  // namespace

Q4T_TEST(vector_index_token_chunks_preserve_utf8_and_bound_overlap) {
  const auto text = std::string(99, 'a') + "中文" + std::string(200, 'b');
  const auto boundaries = Boundaries(text.size());
  std::vector<VectorTokenRange> ranges;
  Q4T_CHECK(
      PlanVectorTokenChunks(text, boundaries, {100, 50}, {}, &ranges).ok());
  Q4T_CHECK(ranges.front().byte_end == 99 &&
            ranges.back().byte_end == text.size());
  for (size_t i = 0; i < ranges.size(); ++i) {
    const auto& range = ranges[i];
    const auto piece =
        text.substr(range.byte_begin, range.byte_end - range.byte_begin);
    Q4T_CHECK(detail::NormalizeUtf8(piece) == piece &&
              range.token_end - range.token_begin <= 100);
    if (i)
      Q4T_CHECK(range.token_begin <= ranges[i - 1].token_end &&
                ranges[i - 1].token_end - range.token_begin <= 50);
  }
  return true;
}

Q4T_TEST(vector_index_chunking_rejects_bad_maps_and_bounded_cancellation) {
  const std::string text(251, 'a');
  auto boundaries = Boundaries(text.size());
  std::vector<VectorTokenRange> ranges;
  boundaries[99] = boundaries[98];
  Q4T_CHECK(
      !PlanVectorTokenChunks(text, boundaries, {100, 50}, {}, &ranges).ok() &&
      ranges.empty());
  boundaries = Boundaries(text.size());
  size_t calls = 0;
  Q4T_CHECK(PlanVectorTokenChunks(
                text, boundaries, {100, 50},
                {Clock::now() + 1s, [&] { return ++calls == 3; }}, &ranges)
                    .http_status == 499 &&
            ranges.empty());
  const std::string large(210000, 'a');
  Q4T_CHECK(PlanVectorTokenChunks(large, Boundaries(large.size()), {100, 50},
                                  {}, &ranges)
                    .code == "vector_index_resource_exhausted" &&
            ranges.empty());
  return true;
}

Q4T_TEST(vector_index_build_preserves_usage_and_no_partial_on_failure) {
  const auto expected = Index();
  Q4T_CHECK(expected.input_tokens == 456 && expected.chunks.size() == 5);
  const std::vector<uint32_t> tokens(expected.text.size(), 64);
  const auto boundaries = Boundaries(tokens.size());
  VectorIndex output = expected;
  uint64_t used = 0;
  size_t calls = 0;
  const auto failing = [&](auto input, auto* vector, uint64_t* committed) {
    ++calls;
    if (calls == 2) {
      *committed = 17;
      return Error(503, "embedding_resource_exhausted",
                   "Injected fixture failure");
    }
    return Embed(input, vector, committed);
  };
  Q4T_CHECK(!BuildVectorIndex(expected.identity, expected.text, tokens,
                              boundaries, failing, {}, &output, &used)
                 .ok() &&
            used == 118 && calls == 2 && output.chunks.empty() &&
            output.text.empty());
  const auto throwing = [&](auto, auto*, uint64_t* committed) -> ObjectReply {
    *committed = 5;
    throw std::runtime_error("Injected backend exception");
  };
  bool threw = false;
  try {
    (void)BuildVectorIndex(expected.identity, expected.text, tokens, boundaries,
                           throwing, {}, &output, &used);
  } catch (const std::runtime_error&) {
    threw = true;
  }
  Q4T_CHECK(threw && used == 5 && output.chunks.empty());
  return true;
}

Q4T_TEST(vector_index_backend_and_file_identity_change_binding) {
  const auto original = Index();
  const auto binding = VectorIndexBinding(original.identity);
  Q4T_CHECK(binding.size() == 64);
  for (int change = 0; change < 5; ++change) {
    auto identity = original.identity;
    if (change == 0) identity.file_id = "file_other";
    if (change == 1) identity.source_sha256 = Digest("other bytes");
    if (change == 2) identity.text_sha256 = Digest("other extraction");
    if (change == 3) identity.chunking.overlap_tokens = 0;
    if (change == 4) {
      identity.dimensions = 64;
      identity.backend_sha256 = VectorEmbeddingIdentity(64);
    }
    Q4T_CHECK(!VectorIndexBinding(identity).empty() &&
              VectorIndexBinding(identity) != binding);
  }
  auto changed = original.identity;
  changed.backend_sha256 = Digest("unknown model");
  Q4T_CHECK(VectorIndexBinding(changed).empty());
  return true;
}

#ifdef Q4T_EMBEDDING_ASSET_WORKER_ENTRY
Q4T_TEST(vector_index_real_tokenizer_raw_boundaries_and_eos_are_preserved) {
  const char* root = std::getenv("Q4T_EMBEDDING_ASSETS");
  const auto directory = std::filesystem::absolute(
      root ? root : ".q4t-work/native-embedding-20261111/assets");
  EmbeddingHostAssets assets;
  GrammarControl control;
  control.compile_deadline = Clock::now() + 15s;
  Q4T_CHECK(LoadEmbeddingAssets(directory, &assets, control, nullptr, false));
  std::string text;
  for (int i = 0; i < 80; ++i) text += "原生入口与 PLE，cafe\xcc\x81。";
  std::vector<uint32_t> tokens;
  q4t::text::EncodeOptions options;
  options.add_special_tokens = false;
  Q4T_CHECK(assets.tokenizer->Encode(text, &tokens, options));
  std::string decoded;
  std::vector<size_t> boundaries{0};
  for (const auto token : tokens) {
    std::string piece;
    Q4T_CHECK(assets.tokenizer->Decode({token}, false, &piece) &&
              !piece.empty());
    decoded += piece;
    boundaries.push_back(decoded.size());
  }
  VectorIndex index;
  uint64_t used = 0;
  auto identity = Identity(decoded);
  identity.source_sha256 = Digest(text);
  Q4T_CHECK(BuildVectorIndex(identity, decoded, tokens, boundaries, Embed,
                             {Clock::now() + 15s, {}}, &index, &used)
                .ok());
  Q4T_CHECK(index.chunks.size() > 2 && used > tokens.size() &&
            index.chunks.back().range.token_end == tokens.size());
  return true;
}
#endif

Q4T_TEST(vector_index_snapshot_bytes_and_scope_survive_worker_restart) {
  Fixture fixture;
  const auto original = Index();
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  {
    ObjectStore objects(fixture.options);
    InputSnapshotStore snapshots(objects);
    Q4T_CHECK(
        SaveVectorIndex(snapshots, Access(), original, 60, &reference, &hold)
            .ok());
  }
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  VectorIndex restored;
  Q4T_CHECK(ReadVectorIndex(snapshots, Access(), reference, original.identity,
                            &restored)
                .ok() &&
            Same(original, restored));
  Q4T_CHECK(ReadVectorIndex(snapshots, Access("other-client"), reference,
                            original.identity, &restored)
                    .http_status == 404 &&
            restored.chunks.empty());
  auto wrong = original.identity;
  wrong.source_sha256 = Digest("different source");
  Q4T_CHECK(
      !ReadVectorIndex(snapshots, Access(), reference, wrong, &restored).ok() &&
      restored.chunks.empty());
  auto wrong_reference = reference;
  wrong_reference.sha256 = Digest("tampered root");
  Q4T_CHECK(!ReadVectorIndex(snapshots, Access(), wrong_reference,
                             original.identity, &restored)
                 .ok());
  Q4T_CHECK(ReadVectorIndex(snapshots, Access(), reference, original.identity,
                            &restored)
                .ok() &&
            Same(original, restored));
  Q4T_CHECK(snapshots.Release(Access(), &hold).ok());
  return true;
}

Q4T_TEST(vector_index_snapshot_unknown_commit_keeps_original_identity) {
  Fixture fixture;
  fixture.options.io_timeout_ms = 250;
  Injection injection("--q4t-object-worker-snapshot-lost-ack");
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  const auto original = Index();
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  const auto result =
      SaveVectorIndex(snapshots, Access(), original, 60, &reference, &hold);
  Q4T_CHECK(result.commit_unknown && hold.commit_unknown &&
            !reference.id.empty());
  const auto identity = reference.id;
  Q4T_CHECK(
      SaveVectorIndex(snapshots, Access(), original, 60, &reference, &hold)
          .commit_unknown &&
      reference.id == identity);
  Q4T_CHECK(snapshots.Release(Access(), &hold).commit_unknown);
  VectorIndex restored;
  Q4T_CHECK(ReadVectorIndex(snapshots, Access(), reference, original.identity,
                            &restored)
                .ok() &&
            Same(original, restored));
  return true;
}

Q4T_TEST(vector_index_invalid_or_cancelled_write_publishes_nothing) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  auto index = Index();
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  index.chunks.back().embedding.assign(32, 0);
  Q4T_CHECK(!SaveVectorIndex(snapshots, Access(), index, 60, &reference, &hold)
                 .ok() &&
            reference.id.empty() && hold.id.empty());
  index = Index();
  auto access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(SaveVectorIndex(snapshots, access, index, 60, &reference, &hold)
                    .http_status == 499 &&
            reference.id.empty() && hold.id.empty());
  ObjectRequest health;
  health.scope = "client:index";
  health.operation = ObjectOperation::kHealth;
  const auto result = objects.Execute(health, Clock::now() + 2s);
  Q4T_CHECK(result.ok() && result.value.GetInt("objects") == 0);
  return true;
}

Q4T_TEST(vector_index_search_projection_keeps_source_and_mutable_attributes) {
  const auto index = Index();
  std::vector<VectorSearchChunk> first, second;
  Q4T_CHECK(VectorIndexSearchChunks(index, Object({{"tag", String("old")}}), {},
                                    &first)
                .ok());
  Q4T_CHECK(VectorIndexSearchChunks(index, Object({{"tag", String("new")}}), {},
                                    &second)
                .ok());
  Q4T_CHECK(first.size() == index.chunks.size() &&
            first[0].chunk_id == second[0].chunk_id &&
            second[0].attributes.GetString("tag") == "new" &&
            second[0].file_id == index.identity.file_id &&
            second.back().text ==
                index.text.substr(index.chunks.back().range.byte_begin));
  Q4T_CHECK(
      VectorIndexSearchChunks(index, Object(), {Clock::now(), {}}, &second)
              .http_status == 408 &&
      second.empty());
  return true;
}
