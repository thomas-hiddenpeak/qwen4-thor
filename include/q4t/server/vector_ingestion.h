// Native Files -> isolated document extraction -> exact trained token input.
#pragma once

#include "q4t/server/file_extract.h"
#include "q4t/server/vector_stores.h"

namespace q4t::server {
struct VectorEncodedText {
  // The tokenizer's actual decoded NFC form; original uploaded bytes remain
  // in Files and their digest remains the immutable source identity.
  std::string text;
  std::vector<uint32_t> tokens;
  std::vector<size_t> boundaries;
};
using VectorRawEncoder =
    std::function<Status(std::string_view, std::vector<uint32_t>*)>;
using VectorRawDecoder = std::function<Status(uint32_t, std::string*)>;
// Caller serializes the actual embedding tokenizer for this entire call.
// Encoder omits EOS, decoder preserves special tokens. Index building adds
// exactly one EOS per chunk, not one at the end of the entire document.
ObjectReply EncodeVectorText(std::string_view source,
                             const VectorRawEncoder& encode,
                             const VectorRawDecoder& decode,
                             const VectorSearchControl& control,
                             VectorEncodedText* output);
using VectorTextEncoder = std::function<ObjectReply(
    std::string_view, const VectorSearchControl&, VectorEncodedText*)>;

struct VectorIngestionOptions {
  uint64_t source_bytes = kFileInputBytes;
  uint64_t text_bytes = 4000000;
  size_t dimensions = 1024;
  FileExtractOptions extraction;
};
struct VectorIngestionPrepared {
  std::string scope_sha256;
  VectorIngestionClaim claim;
  VectorIndexIdentity identity;
  VectorEncodedText encoded;
  uint64_t input_tokens = 0;
};
// No model or quota is consumed. Returns a complete, source-bound document and
// exact overlapping chunk+EOS token cost before any embedding admission.
ObjectReply PrepareVectorIngestion(ObjectStore& objects,
                                   const BinaryAccess& access,
                                   const VectorIngestionClaim& claim,
                                   const VectorIngestionOptions& options,
                                   const VectorTextEncoder& encode,
                                   VectorIngestionPrepared* prepared);

struct VectorIngestionResult {
  uint64_t committed_input = 0;
  InputSnapshotRef snapshot;
  InputSnapshotHold hold;
  bool published = false;
};
// reserve must admit the entire exact cost against the authenticated client's
// existing rate owner (no recursive HTTP or extra external RPM). embed must
// use the real native model and drain its resources before returning. Caller
// settles actual quota on all exits and reconciles terminal failures/owner
// loss. Unknown snapshot/parent commits retain their IDs and hold; never
// blindly repeat this method. A known snapshot can be published via Finish
// without model execution. No implicit model/service restart occurs here.
ObjectReply ExecuteVectorIngestion(
    ObjectStore& objects, const BinaryAccess& access,
    const VectorIngestionPrepared& prepared,
    const std::function<ObjectReply(uint64_t)>& reserve,
    const VectorIndexEmbedding& embed, VectorIngestionResult* result);
}  // namespace q4t::server
