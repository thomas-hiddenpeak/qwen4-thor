#include "q4t/server/vector_ingestion.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
ObjectReply Invalid() {
  return Error(503, "vector_ingestion_state_invalid",
               "Ingestion source or execution identity is inconsistent");
}
ObjectReply Active(const VectorSearchControl& control) {
  if (control.cancelled && control.cancelled())
    return Error(499, "request_cancelled", "Ingestion cancelled");
  if (Clock::now() >= control.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Ingestion deadline exceeded");
  return {};
}
BinaryAccess Bounded(const BinaryAccess& access,
                     const VectorIngestionClaim& claim) {
  auto result = access;
  const auto now = std::time(nullptr);
  const auto seconds = claim.deadline_at > now
                           ? std::min<int64_t>(3600, claim.deadline_at - now)
                           : 0;
  result.deadline =
      std::min(access.deadline, Clock::now() + std::chrono::seconds(seconds));
  return result;
}
ObjectReply Flatten(const FileDocument& document, uint64_t maximum,
                    const VectorSearchControl& control, std::string* text) {
  text->clear();
  if (!document.pending.empty()) return Invalid();
  std::string candidate;
  const auto append = [&](std::string_view piece) {
    if (candidate.size() > maximum || piece.size() > maximum - candidate.size())
      return false;
    candidate.append(piece);
    return true;
  };
  const auto capacity = [] {
    return Error(413, "vector_document_capacity",
                 "Complete document exceeds ingestion text or row capacity");
  };
  if (!append(document.text)) return capacity();
  for (const auto& sheet : document.sheets) {
    auto active = Active(control);
    if (!active.ok()) return active;
    // The shared spreadsheet parser has a bounded row window. Do not index
    // that prefix while presenting it as a complete source document.
    if (sheet.source_rows > sheet.rows.size()) return capacity();
    if (!append("\n[Sheet ") || !append(OutputJson(String(sheet.name))) ||
        !append("]\n"))
      return capacity();
    for (const auto& row : sheet.rows) {
      active = Active(control);
      if (!active.ok()) return active;
      auto cells = Array();
      for (const auto& cell : row) cells.array.push_back(String(cell));
      if (!append(OutputJson(cells)) || !append("\n")) return capacity();
    }
  }
  if (candidate.empty())
    return Error(400, "invalid_file", "File has no extractable text to index");
  if (detail::NormalizeUtf8(candidate) != candidate) return Invalid();
  *text = std::move(candidate);
  return Active(control);
}
ObjectReply Cost(const VectorIngestionPrepared& prepared,
                 const VectorSearchControl& control, uint64_t* cost) {
  *cost = 0;
  if (prepared.encoded.boundaries.size() !=
          prepared.encoded.tokens.size() + 1 ||
      VectorIndexBinding(prepared.identity).empty() ||
      Digest(prepared.encoded.text) != prepared.identity.text_sha256)
    return Invalid();
  std::vector<VectorTokenRange> chunks;
  auto reply =
      PlanVectorTokenChunks(prepared.encoded.text, prepared.encoded.boundaries,
                            prepared.identity.chunking, control, &chunks);
  if (!reply.ok()) return reply;
  for (const auto& chunk : chunks)
    *cost += chunk.token_end - chunk.token_begin + 1;
  return {};
}
}  // namespace

ObjectReply EncodeVectorText(std::string_view source,
                             const VectorRawEncoder& encode,
                             const VectorRawDecoder& decode,
                             const VectorSearchControl& control,
                             VectorEncodedText* output) {
  *output = {};
  if (!encode || !decode || source.empty() ||
      detail::NormalizeUtf8(source) != source)
    return Invalid();
  if (source.size() > kVectorSearchTextBytes)
    return Error(413, "vector_document_capacity",
                 "Text exceeds tokenizer budget");
  auto active = Active(control);
  if (!active.ok()) return active;
  VectorEncodedText result;
  auto status = encode(source, &result.tokens);
  active = Active(control);
  if (!active.ok()) return active;
  if (!status)
    return Error(400, "vector_tokenization_failed",
                 "Document tokenization failed");
  if (result.tokens.empty() || result.tokens.size() > (4ULL << 20))
    return Error(413, "vector_document_capacity",
                 "Text exceeds raw token budget");
  result.boundaries.reserve(result.tokens.size() + 1);
  result.boundaries.push_back(0);
  for (size_t i = 0; i < result.tokens.size(); ++i) {
    if (i % 256 == 0) {
      active = Active(control);
      if (!active.ok()) return active;
    }
    std::string piece;
    status = decode(result.tokens[i], &piece);
    if (!status || piece.empty()) return Invalid();
    if (piece.size() > kVectorSearchTextBytes - result.text.size())
      return Error(413, "vector_document_capacity",
                   "Decoded text exceeds budget");
    result.text += piece;
    result.boundaries.push_back(result.text.size());
  }
  if (detail::NormalizeUtf8(result.text) != result.text) return Invalid();
  active = Active(control);
  if (!active.ok()) return active;
  *output = std::move(result);
  return {};
}

ObjectReply PrepareVectorIngestion(ObjectStore& objects,
                                   const BinaryAccess& access,
                                   const VectorIngestionClaim& claim,
                                   const VectorIngestionOptions& options,
                                   const VectorTextEncoder& encode,
                                   VectorIngestionPrepared* prepared) {
  *prepared = {};
  if (!encode || !options.source_bytes ||
      options.source_bytes > kFileInputBytes || !options.text_bytes ||
      options.text_bytes > kVectorSearchTextBytes ||
      VectorEmbeddingIdentity(options.dimensions).empty())
    return Invalid();
  auto bounded = Bounded(access, claim);
  const VectorSearchControl control{bounded.deadline, bounded.cancelled};
  auto active = Active(control);
  if (!active.ok()) return active;
  VectorStores stores(objects);
  auto member = stores.LoadClaim(bounded, claim);
  if (!member.ok()) return member;
  const auto* file = member.value.Find("data")->Find("file");
  VectorIngestionPrepared next;
  next.scope_sha256 = Digest(access.scope);
  next.claim = claim;
  ChatPlanError error;
  if (!ParseVectorChunking(*file->Find("chunking_strategy"),
                           &next.identity.chunking, &error))
    return Invalid();
  FileInputSource source;
  source.kind = FileInputSource::Kind::kStored;
  source.value = claim.file_id;
  source.param = "file_id";
  FileInputBudget budget{options.source_bytes, 0, 1, 0};
  FileInputContent content;
  auto reply = ReadLocalFileInput(&objects, bounded, source, &budget, &content);
  if (!reply.ok()) return reply;
  FileDocument document;
  auto extraction = options.extraction;
  extraction.require_complete_rows = true;
  extraction.document.max_text_bytes =
      std::min(extraction.document.max_text_bytes, options.text_bytes);
  reply = ExtractFileDocument(content, extraction, false, "low", bounded,
                              &document);
  if (!reply.ok()) return reply;
  if (content.file_id != claim.file_id ||
      content.sha256 != document.source_sha256 ||
      content.filename != member.value.Find("data")->GetString("filename"))
    return Invalid();
  next.identity.file_id = content.file_id;
  next.identity.filename = content.filename;
  next.identity.source_sha256 = content.sha256;
  next.identity.dimensions = options.dimensions;
  next.identity.backend_sha256 = VectorEmbeddingIdentity(options.dimensions);
  // Release potentially large source/page images before tokenizer and vectors.
  content = {};
  std::string text;
  reply = Flatten(document, options.text_bytes, control, &text);
  if (!reply.ok()) return reply;
  document = {};
  reply = encode(text, control, &next.encoded);
  if (!reply.ok()) return reply;
  next.identity.text_sha256 = Digest(next.encoded.text);
  reply = Cost(next, control, &next.input_tokens);
  if (!reply.ok()) return reply;
  reply = stores.LoadClaim(bounded, claim);
  if (!reply.ok()) return reply;
  *prepared = std::move(next);
  return {};
}

ObjectReply ExecuteVectorIngestion(
    ObjectStore& objects, const BinaryAccess& access,
    const VectorIngestionPrepared& prepared,
    const std::function<ObjectReply(uint64_t)>& reserve,
    const VectorIndexEmbedding& embed, VectorIngestionResult* result) {
  if (result->committed_input || !result->snapshot.id.empty() ||
      !result->hold.id.empty() || result->published)
    return Error(409, "vector_ingestion_already_executed",
                 "Reconcile the original ingestion result before retrying");
  *result = {};
  if (!reserve || !embed || prepared.scope_sha256 != Digest(access.scope) ||
      prepared.identity.file_id != prepared.claim.file_id)
    return Invalid();
  const auto bounded = Bounded(access, prepared.claim);
  const VectorSearchControl control{bounded.deadline, bounded.cancelled};
  uint64_t cost;
  auto reply = Cost(prepared, control, &cost);
  if (!reply.ok()) return reply;
  if (cost != prepared.input_tokens) return Invalid();
  VectorStores stores(objects);
  reply = stores.LoadClaim(bounded, prepared.claim);
  if (!reply.ok()) return reply;
  reply = reserve(cost);
  if (!reply.ok()) return reply;
  VectorIndex index;
  reply = BuildVectorIndex(prepared.identity, prepared.encoded.text,
                           prepared.encoded.tokens, prepared.encoded.boundaries,
                           embed, control, &index, &result->committed_input);
  if (!reply.ok()) return reply;
  const auto seconds =
      std::min<int64_t>(600, prepared.claim.deadline_at - std::time(nullptr));
  if (seconds <= 0)
    return Error(408, "request_deadline_exceeded", "Ingestion expired");
  InputSnapshotStore snapshots(objects);
  reply = SaveVectorIndex(snapshots, bounded, index, seconds, &result->snapshot,
                          &result->hold);
  if (!reply.ok()) return reply;
  index = {};
  reply = stores.Finish(bounded, prepared.claim, prepared.identity,
                        result->snapshot);
  if (reply.ok()) result->published = true;
  if (reply.commit_unknown) return reply;
  // A known publication/failure permits releasing the temporary hold. Cleanup
  // failure is auxiliary and cannot turn a committed index into a failure.
  try {
    auto cleanup = access;
    cleanup.deadline = Clock::now() + std::chrono::milliseconds(200);
    cleanup.cancelled = {};
    if (!snapshots.Release(cleanup, &result->hold).ok())
      objects.RecordAuxiliaryCleanupFailure();
  } catch (...) {
    objects.RecordAuxiliaryCleanupFailure();
  }
  return reply;
}
}  // namespace q4t::server
