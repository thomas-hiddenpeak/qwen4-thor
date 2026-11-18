#include "q4t/server/voice_registry.h"

#include <algorithm>
#include <cmath>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr auto kConditionModel = "Qwen3-TTS-12Hz-1.7B-Base";
constexpr auto kConditionRevision = "fd4b254389122332181a7c3db7f27e918eec64e3";
// Same-speaker reference threshold already used by native diarization. Real
// voice registration still requires a positive/different-speaker business gate.
constexpr double kIdentityCosine = .65;
ObjectReply Invalid(std::string message) {
  return Error(400, "invalid_voice_evidence", std::move(message));
}
ObjectReply Corrupt() {
  return Error(503, "voice_state_invalid", "Voice record is inconsistent");
}
bool Text(std::string_view s, size_t chars) {
  return !s.empty() && s.find('\0') == s.npos &&
         detail::NormalizeUtf8(s) == s &&
         size_t(std::count_if(
             s.begin(), s.end(),
             [](unsigned char c) { return (c & 0xc0) != 0x80; })) <= chars &&
         s.find_first_not_of(" \t\r\n\f\v") != s.npos;
}
bool Hash(std::string_view s) {
  return s.size() == 64 && std::all_of(s.begin(), s.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
bool Vector(std::span<const float> values, size_t dimensions) {
  if (values.size() != dimensions) return false;
  double norm = 0;
  for (const float value : values) {
    if (!std::isfinite(value)) return false;
    norm += double(value) * value;
  }
  return std::isfinite(norm) && norm > 0;
}
Json VectorJson(std::span<const float> vector) {
  auto value = Array();
  for (const auto x : vector) {
    Json number;
    number.type = Json::Type::kNumber;
    number.number = x;
    value.array.push_back(std::move(number));
  }
  return value;
}
bool ReadVector(const Json& data, const char* key, size_t size,
                std::vector<float>* out) {
  const auto* value = data.GetArray(key);
  if (!value || value->array.size() != size) return false;
  std::vector<float> next;
  next.reserve(size);
  for (const auto& number : value->array) {
    if (!number.IsNumber()) return false;
    const float scalar = number.number;
    if (!std::isfinite(scalar) || double(scalar) != number.number) return false;
    next.push_back(scalar);
  }
  if (!Vector(next, size)) return false;
  *out = std::move(next);
  return true;
}
bool Evidence(std::string_view bytes, const VoiceSampleEvidence& evidence,
              bool condition) {
  return !bytes.empty() && bytes.size() <= kVoiceAudioBytes &&
         evidence.source_sha256 == Digest(bytes) &&
         evidence.duration_ns >= 53333333 &&
         evidence.duration_ns <= 60000000000ULL &&
         Text(evidence.transcript, 16384) && Vector(evidence.identity, 192) &&
         (!condition || Vector(evidence.condition, 2048));
}
bool SameVoice(std::span<const float> a, std::span<const float> b) {
  if (!Vector(a, 192) || !Vector(b, 192)) return false;
  double aa = 0, bb = 0, dot = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    aa += double(a[i]) * a[i];
    bb += double(b[i]) * b[i];
    dot += double(a[i]) * b[i];
  }
  return dot / std::sqrt(aa * bb) >= kIdentityCosine;
}
Json Metadata(const Json& record, ObjectKind kind) {
  const auto* data = record.Find("data");
  if (!data) return {};
  auto result = Object(
      {{"id", String(record.GetString("id"))},
       {"object", String(kind == ObjectKind::kVoice ? "audio.voice"
                                                    : "audio.voice_consent")},
       {"created_at", Number(record.GetInt("created_at"))},
       {"name", String(data->GetString("name"))}});
  if (kind == ObjectKind::kVoice)
    Set(&result, "type", String("audio_sample"));
  else
    Set(&result, "language", String(data->GetString("language")));
  return result;
}
ObjectRequest Request(ObjectKind kind, const std::string& id,
                      const BinaryAccess& access,
                      ObjectOperation operation = ObjectOperation::kGet) {
  ObjectRequest request;
  request.operation = operation;
  request.kind = kind;
  request.scope = access.scope;
  request.id = id;
  request.ttl_seconds = 0;
  return request;
}
Json EvidenceJson(const VoiceSampleEvidence& evidence) {
  return Object({{"source_sha256", String(evidence.source_sha256)},
                 {"duration_ns", Number(evidence.duration_ns)},
                 {"transcript", String(evidence.transcript)},
                 {"identity", VectorJson(evidence.identity)}});
}
void Unknown(ObjectReply* reply, const std::string& id) {
  if (reply->commit_unknown) reply->value = Object({{"id", String(id)}});
}
}  // namespace

ObjectReply VoiceRegistry::Read(ObjectKind kind, const std::string& id,
                                const BinaryAccess& access) {
  if (!ValidId(id)) return Error(400, "invalid_request", "Invalid voice ID");
  auto result = objects_.Execute(Request(kind, id, access), access.deadline,
                                 access.cancelled);
  if (!result.ok()) return result;
  const auto* data = result.value.Find("data");
  const bool voice = kind == ObjectKind::kVoice;
  if (!data ||
      data->GetString("format") !=
          (voice ? "q4t.voice.v1" : "q4t.voice_consent.v1") ||
      !Text(data->GetString("name"), 256) ||
      !Hash(data->GetString("source_sha256")) ||
      result.value.GetInt("revision") < 1 ||
      result.value.GetInt("created_at") < 1)
    return Corrupt();
  BinaryManifest blob;
  const auto* manifest = data->Find("blob");
  std::vector<float> vector;
  if (!manifest || !ParseBinaryManifest(*manifest, &blob) || !blob.bytes ||
      blob.bytes > kVoiceAudioBytes ||
      !ReadVector(*data, "identity", 192, &vector) ||
      data->GetInt("duration_ns") < 53333333 ||
      data->GetInt("duration_ns") > 60000000000LL ||
      !Text(data->GetString("transcript"), 16384))
    return Corrupt();
  if (voice) {
    if (data->GetString("type") != "audio_sample" ||
        !ValidId(data->GetString("consent")) ||
        !Hash(data->GetString("consent_source_sha256")) ||
        data->GetString("condition_model") != kConditionModel ||
        data->GetString("condition_revision") != kConditionRevision ||
        !ReadVector(*data, "condition", 2048, &vector))
      return Corrupt();
  } else if (!WellFormedVoiceLanguage(data->GetString("language")) ||
             !Text(data->GetString("verified_phrase"), 4096)) {
    return Corrupt();
  }
  return result;
}

ObjectReply VoiceRegistry::Publish(ObjectRequest root, std::string_view bytes,
                                   const BinaryAccess& access,
                                   std::span<const ObjectRequest> parents) {
  BinaryStore binary(objects_);
  BinaryStage stage;
  auto reply = binary.Begin(access, bytes.size(), 120, &stage);
  const auto id = root.id;
  const auto kind = root.kind;
  size_t offset = 0;
  while (reply.ok() && offset < bytes.size()) {
    const auto chunk = bytes.substr(offset, kBinaryChunkBytes);
    reply = binary.Append(access, &stage, chunk);
    offset += chunk.size();
  }
  if (reply.ok())
    reply = binary.Publish(access, &stage, std::move(root), parents);
  if (!reply.ok()) {
    // Never undo/replay an uncertain commit. A known failure detaches staging;
    // cancellation/IO failure may leave only a bounded temporary lease.
    if (!stage.id.empty() && !stage.closed && !stage.commit_unknown &&
        !reply.commit_unknown) {
      const auto cleanup = binary.Abort(access, &stage);
      if (!cleanup.ok()) objects_.RecordAuxiliaryCleanupFailure();
    }
    Unknown(&reply, id);
    if (reply.commit_unknown && !stage.id.empty())
      Set(&reply.value, "stage_id", String(stage.id));
    return reply;
  }
  // The public receipt uses committed metadata. No audio/embedding leaks.
  auto stored = Read(kind, id, access);
  if (!stored.ok()) {
    stored.commit_unknown = true;
    Unknown(&stored, id);
    return stored;
  }
  stored.value = Metadata(stored.value, kind);
  return stored;
}

ObjectReply VoiceRegistry::CreateConsent(const VoiceConsentRequest& request,
                                         const VoiceSampleEvidence& evidence,
                                         std::string verified_phrase,
                                         const BinaryAccess& access) {
  if (!Text(request.name, 256) || !WellFormedVoiceLanguage(request.language) ||
      !Text(verified_phrase, 4096) ||
      !Evidence(request.recording.bytes, evidence, false))
    return Invalid("Consent requires verified recording, phrase and speaker");
  auto root =
      Request(ObjectKind::kVoiceConsent, NewObjectId(ObjectKind::kVoiceConsent),
              access, ObjectOperation::kPut);
  if (root.id.empty())
    return Error(503, "voice_id_unavailable", "No consent ID");
  root.data = EvidenceJson(evidence);
  Set(&root.data, "format", String("q4t.voice_consent.v1"));
  Set(&root.data, "name", String(request.name));
  Set(&root.data, "language", String(request.language));
  Set(&root.data, "verified_phrase", String(std::move(verified_phrase)));
  return Publish(std::move(root), request.recording.bytes, access);
}

ObjectReply VoiceRegistry::RetrieveConsent(const std::string& id,
                                           const BinaryAccess& access) {
  auto saved = Read(ObjectKind::kVoiceConsent, id, access);
  if (saved.ok())
    saved.value = Metadata(saved.value, ObjectKind::kVoiceConsent);
  return saved;
}
ObjectReply VoiceRegistry::UpdateConsent(const std::string& id,
                                         const std::string& name,
                                         const BinaryAccess& access) {
  if (!Text(name, 256))
    return Error(400, "invalid_request", "Invalid consent name");
  auto saved = Read(ObjectKind::kVoiceConsent, id, access);
  if (!saved.ok()) return saved;
  auto update =
      Request(ObjectKind::kVoiceConsent, id, access, ObjectOperation::kPut);
  update.expected_revision = saved.value.GetInt("revision");
  update.data = *saved.value.Find("data");
  update.expires_at = saved.value.GetInt("expires_at");
  Set(&update.data, "name", String(name));
  auto result = objects_.Execute(update, access.deadline, access.cancelled);
  if (result.ok()) result.value = Metadata(result.value, update.kind);
  Unknown(&result, id);
  return result;
}
ObjectReply VoiceRegistry::BindConsent(const std::string& id,
                                       const BinaryAccess& access,
                                       VoiceConsentBinding* out) {
  if (!out) return Invalid("Missing consent binding owner");
  auto saved = Read(ObjectKind::kVoiceConsent, id, access);
  if (!saved.ok()) return saved;
  const auto& data = *saved.value.Find("data");
  VoiceConsentBinding next{access.scope,
                           id,
                           data.GetString("source_sha256"),
                           saved.value.GetInt("revision"),
                           {}};
  if (!ReadVector(data, "identity", 192, &next.identity)) return Corrupt();
  *out = std::move(next);
  saved.value = Metadata(saved.value, ObjectKind::kVoiceConsent);
  return saved;
}

ObjectReply VoiceRegistry::CreateSampleVoice(
    const VoiceCreateRequest& request, const VoiceSampleEvidence& evidence,
    const VoiceConsentBinding& consent, const BinaryAccess& access) {
  if (request.type != VoiceType::kAudioSample || !Text(request.name, 256) ||
      request.consent != consent.id || access.scope != consent.scope ||
      consent.revision < 1 ||
      !Evidence(request.audio_sample.bytes, evidence, true))
    return Invalid("Voice requires owned, verified sample and consent binding");
  auto saved = Read(ObjectKind::kVoiceConsent, consent.id, access);
  if (!saved.ok()) return saved;
  const auto& data = *saved.value.Find("data");
  if (saved.value.GetInt("revision") != consent.revision ||
      data.GetString("source_sha256") != consent.source_sha256)
    return Error(409, "voice_consent_changed",
                 "Consent changed during verification");
  std::vector<float> identity;
  if (!ReadVector(data, "identity", 192, &identity)) return Corrupt();
  if (identity != consent.identity || !SameVoice(identity, evidence.identity))
    return Error(400, "voice_speaker_mismatch",
                 "Sample and consent speakers differ");
  auto parent = Request(ObjectKind::kVoiceConsent, consent.id, access,
                        ObjectOperation::kPut);
  parent.data = data;
  parent.expected_revision = consent.revision;
  parent.expires_at = saved.value.GetInt("expires_at");
  auto root = Request(ObjectKind::kVoice, NewObjectId(ObjectKind::kVoice),
                      access, ObjectOperation::kPut);
  if (root.id.empty()) return Error(503, "voice_id_unavailable", "No voice ID");
  root.data = EvidenceJson(evidence);
  Set(&root.data, "format", String("q4t.voice.v1"));
  Set(&root.data, "name", String(request.name));
  Set(&root.data, "type", String("audio_sample"));
  Set(&root.data, "consent", String(consent.id));
  Set(&root.data, "consent_source_sha256", String(consent.source_sha256));
  Set(&root.data, "condition", VectorJson(evidence.condition));
  Set(&root.data, "condition_model", String(kConditionModel));
  Set(&root.data, "condition_revision", String(kConditionRevision));
  return Publish(std::move(root), request.audio_sample.bytes, access,
                 {&parent, 1});
}

ObjectReply VoiceRegistry::Resolve(const std::string& id,
                                   const BinaryAccess& access,
                                   VoiceSynthesisBinding* out) {
  if (!out) return Invalid("Missing voice binding owner");
  auto saved = Read(ObjectKind::kVoice, id, access);
  if (!saved.ok()) return saved;
  const auto& data = *saved.value.Find("data");
  auto consent =
      Read(ObjectKind::kVoiceConsent, data.GetString("consent"), access);
  if (!consent.ok()) return consent;
  if (consent.value.Find("data")->GetString("source_sha256") !=
      data.GetString("consent_source_sha256"))
    return Corrupt();
  VoiceSynthesisBinding next{access.scope,
                             id,
                             data.GetString("consent"),
                             data.GetString("source_sha256"),
                             saved.value.GetInt("revision"),
                             {}};
  if (!ReadVector(data, "condition", 2048, &next.condition)) return Corrupt();
  *out = std::move(next);
  saved.value = Metadata(saved.value, ObjectKind::kVoice);
  return saved;
}

ObjectReply VoiceRegistry::Erase(ObjectKind kind, const std::string& id,
                                 const BinaryAccess& access) {
  if (!ValidId(id)) return Error(400, "invalid_request", "Invalid voice ID");
  // Ownership and durable integrity still apply. Semantic payload corruption
  // must not prevent its owner from deleting an unusable optional resource.
  auto saved = objects_.Execute(Request(kind, id, access), access.deadline,
                                access.cancelled);
  if (!saved.ok()) return saved;
  auto erase = Request(kind, id, access, ObjectOperation::kErase);
  erase.expected_revision = saved.value.GetInt("revision");
  auto result = objects_.Execute(erase, access.deadline, access.cancelled);
  if (result.ok())
    result.value = Object({{"id", String(id)},
                           {"deleted", Boolean(true)},
                           {"object", String(kind == ObjectKind::kVoice
                                                 ? "audio.voice"
                                                 : "audio.voice_consent")}});
  Unknown(&result, id);
  return result;
}
ObjectReply VoiceRegistry::DeleteConsent(const std::string& id,
                                         const BinaryAccess& access) {
  return Erase(ObjectKind::kVoiceConsent, id, access);
}
ObjectReply VoiceRegistry::DeleteVoice(const std::string& id,
                                       const BinaryAccess& access) {
  return Erase(ObjectKind::kVoice, id, access);
}
ObjectReply VoiceRegistry::ListConsents(int limit, std::string after,
                                        const BinaryAccess& access) {
  if (limit < 1 || limit > 100 || (!after.empty() && !ValidId(after)))
    return Error(400, "invalid_request", "Invalid consent pagination");
  auto request =
      Request(ObjectKind::kVoiceConsent, {}, access, ObjectOperation::kList);
  request.limit = limit;
  request.after = std::move(after);
  auto result = objects_.Execute(request, access.deadline, access.cancelled);
  if (!result.ok()) return result;
  auto& data = Field(result.value, "data");
  if (!data.IsArray()) return Corrupt();
  for (auto& record : data.array) {
    const auto* value = record.Find("data");
    if (!value || value->GetString("format") != "q4t.voice_consent.v1" ||
        !Text(value->GetString("name"), 256) ||
        !WellFormedVoiceLanguage(value->GetString("language")))
      return Corrupt();
    record = Metadata(record, ObjectKind::kVoiceConsent);
  }
  return result;
}
}  // namespace q4t::server
