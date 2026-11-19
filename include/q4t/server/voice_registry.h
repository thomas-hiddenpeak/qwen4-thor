// Native scoped voice registry. Audio/model verification precedes mutation;
// HTTP callers never supply vectors or verification evidence directly.
#pragma once

#include <vector>

#include "q4t/audio/tts_generation.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/voice_request.h"

namespace q4t::server {
struct VoiceSampleEvidence {
  std::string source_sha256, transcript;
  uint64_t duration_ns = 0;
  std::vector<float> identity;   // Independent 192-dimensional speaker model.
  std::vector<float> condition;  // Actual 2048-dimensional Base condition.
  uint64_t reference_samples =
      0;  // Actual 24kHz encoder input, not a duration estimate.
  std::vector<audio::TtsCodes> reference_codes{};
};
struct VoiceConsentBinding {
  std::string scope, id, source_sha256;
  int64_t revision = 0;
  std::vector<float> identity;
};
struct VoiceSynthesisBinding {
  std::string scope, id, consent, source_sha256;
  int64_t revision = 0;
  std::vector<float> condition;
  uint64_t reference_samples = 0;
  std::string reference_transcript{};
  std::vector<audio::TtsCodes> reference_codes{};
};

class VoiceRegistry {
 public:
  explicit VoiceRegistry(ObjectStore& objects) : objects_(objects) {}
  // verified_phrase is the server's matched consent phrase, not request text.
  ObjectReply CreateConsent(const VoiceConsentRequest& request,
                            const VoiceSampleEvidence& evidence,
                            std::string verified_phrase,
                            const BinaryAccess& access);
  ObjectReply RetrieveConsent(const std::string& id,
                              const BinaryAccess& access);
  ObjectReply UpdateConsent(const std::string& id, const std::string& name,
                            const BinaryAccess& access);
  ObjectReply DeleteConsent(const std::string& id, const BinaryAccess& access);
  ObjectReply ListConsents(int limit, std::string after,
                           const BinaryAccess& access);
  ObjectReply BindConsent(const std::string& id, const BinaryAccess& access,
                          VoiceConsentBinding* out);
  // Publishes sample bytes + real condition and CAS-checks the matched
  // consent in one transaction. Conflict/revocation never creates a voice.
  ObjectReply CreateSampleVoice(const VoiceCreateRequest& request,
                                const VoiceSampleEvidence& evidence,
                                const VoiceConsentBinding& consent,
                                const BinaryAccess& access);
  ObjectReply DeleteVoice(const std::string& id, const BinaryAccess& access);
  // Authorization snapshot for one bounded synthesis request. No process-wide
  // cache: every new resolution checks the live, same-owner consent. Deletion
  // does not cancel a request that already owns its authorized vector.
  ObjectReply Resolve(const std::string& id, const BinaryAccess& access,
                      VoiceSynthesisBinding* out);

 private:
  ObjectReply Read(ObjectKind kind, const std::string& id,
                   const BinaryAccess& access);
  ObjectReply Erase(ObjectKind kind, const std::string& id,
                    const BinaryAccess& access);
  ObjectReply Publish(ObjectRequest root, std::string_view bytes,
                      const BinaryAccess& access,
                      std::span<const ObjectRequest> parents = {});
  ObjectStore& objects_;
};
}  // namespace q4t::server
