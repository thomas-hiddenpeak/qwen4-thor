#include "q4t/server/voice_registry.h"

#include <algorithm>
#include <cstdlib>
#include <stdexcept>
#include <filesystem>
#include <limits>
#include <thread>

#include "q4t/server/object_retention.h"
#include "q4t/server/output_schema.h"
#include "q4t/trace/sha256.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(env ? env : ".q4t-work/voice-cpu");
    std::filesystem::create_directories(root);
    auto path = (root / "voice-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("voice fixture path");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
BinaryAccess Access(std::string scope = "client:voice-a") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
VoiceSampleEvidence Evidence(std::string_view bytes) {
  VoiceSampleEvidence e;
  e.source_sha256 = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size()}));
  e.duration_ns = 3000000000ULL;
  e.transcript = "CPU storage fixture, not model evidence";
  e.identity.assign(192, .125F);
  e.condition.assign(2048, .03125F);
  for (size_t i = 0; i < e.condition.size(); ++i)
    e.condition[i] += float(i) / 8192;
  return e;
}
VoiceConsentRequest Consent() {
  return {"Owner", "en-US", {"fixture consent", "test.wav", "audio/wav"}};
}
VoiceCreateRequest Sample(const std::string& id) {
  VoiceCreateRequest r;
  r.name = "Sample";
  r.consent = id;
  r.audio_sample = {"fixture sample", "sample.wav", "audio/wav"};
  return r;
}
ObjectReply Create(VoiceRegistry& registry) {
  const auto r = Consent();
  return registry.CreateConsent(r, Evidence(r.recording.bytes),
                                "CPU fixture verified phrase", Access());
}
ObjectReply Stored(ObjectStore& store, ObjectKind kind, const std::string& id) {
  ObjectRequest r;
  r.kind = kind;
  r.id = id;
  r.scope = Access().scope;
  return store.Execute(r, Access().deadline);
}
}  // namespace

Q4T_TEST(voice_registry_persists_exact_condition_and_private_recordings) {
  Fixture f;
  std::string id, consent;
  auto expected = Evidence("fixture sample").condition;
  {
    ObjectStore store(f.options);
    VoiceRegistry registry(store);
    const auto c = Create(registry);
    Q4T_CHECK(Ok(c));
    consent = c.value.GetString("id");
    Q4T_CHECK(consent.starts_with("cons_") &&
              c.value.GetString("object") == "audio.voice_consent" &&
              c.value.object.size() == 5);
    VoiceConsentBinding binding;
    Q4T_CHECK(Ok(registry.BindConsent(consent, Access(), &binding)));
    const auto r = Sample(consent);
    const auto v = registry.CreateSampleVoice(r, Evidence(r.audio_sample.bytes),
                                              binding, Access());
    Q4T_CHECK(Ok(v));
    id = v.value.GetString("id");
    Q4T_CHECK(id.starts_with("voice_") && v.value.object.size() == 5 &&
              v.value.GetString("object") == "audio.voice" &&
              v.value.GetString("type") == "audio_sample");
    BinaryStore binary(store);
    BinaryRead read;
    Q4T_CHECK(Ok(binary.Open(Access(), ObjectKind::kVoice, id, 60, &read)));
    std::string bytes;
    Q4T_CHECK(Ok(binary.Read(Access(), read, 0, &bytes)));
    Q4T_CHECK(bytes == "fixture sample");
    Q4T_CHECK(Ok(binary.Close(Access(), &read)));
  }
  ObjectStore reopened(f.options);
  VoiceRegistry registry(reopened);
  VoiceSynthesisBinding voice;
  Q4T_CHECK(Ok(registry.Resolve(id, Access(), &voice)));
  Q4T_CHECK(voice.condition == expected && voice.consent == consent &&
            voice.scope == Access().scope);
  return true;
}

Q4T_TEST(voice_registry_every_lookup_mutation_and_list_is_owner_scoped) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  const auto id = c.value.GetString("id");
  VoiceConsentBinding binding;
  const auto other = Access("client:voice-b");
  Q4T_CHECK(registry.BindConsent(id, other, &binding).http_status == 404);
  Q4T_CHECK(registry.RetrieveConsent(id, other).http_status == 404);
  Q4T_CHECK(registry.UpdateConsent(id, "hijack", other).http_status == 404);
  Q4T_CHECK(registry.DeleteConsent(id, other).http_status == 404);
  Q4T_CHECK(registry.ListConsents(20, {}, other)
                .value.GetArray("data")
                ->array.empty());
  Q4T_CHECK(Ok(registry.BindConsent(id, Access(), &binding)));
  const auto r = Sample(id);
  Q4T_CHECK(
      !registry
           .CreateSampleVoice(r, Evidence(r.audio_sample.bytes), binding, other)
           .ok());
  const auto v = registry.CreateSampleVoice(r, Evidence(r.audio_sample.bytes),
                                            binding, Access());
  Q4T_CHECK(Ok(v));
  VoiceSynthesisBinding voice;
  voice.id = "unchanged";
  Q4T_CHECK(
      registry.Resolve(v.value.GetString("id"), other, &voice).http_status ==
      404);
  Q4T_CHECK(voice.id == "unchanged");
  Q4T_CHECK(registry.DeleteVoice(v.value.GetString("id"), other).http_status ==
            404);
  return true;
}

Q4T_TEST(voice_registry_revocation_blocks_new_resolution_keeps_owned_snapshot) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  const auto consent = c.value.GetString("id");
  VoiceConsentBinding binding;
  Q4T_CHECK(Ok(registry.BindConsent(consent, Access(), &binding)));
  const auto r = Sample(consent);
  const auto v = registry.CreateSampleVoice(r, Evidence(r.audio_sample.bytes),
                                            binding, Access());
  Q4T_CHECK(Ok(v));
  const auto id = v.value.GetString("id");
  VoiceSynthesisBinding snapshot;
  Q4T_CHECK(Ok(registry.Resolve(id, Access(), &snapshot)));
  const auto saved = snapshot.condition;
  const auto deleted = registry.DeleteConsent(consent, Access());
  Q4T_CHECK(Ok(deleted) && deleted.value.GetBool("deleted"));
  Q4T_CHECK(registry.Resolve(id, Access(), &snapshot).http_status == 404);
  Q4T_CHECK(snapshot.condition == saved);
  Q4T_CHECK(!registry
                 .CreateSampleVoice(r, Evidence(r.audio_sample.bytes), binding,
                                    Access())
                 .ok());
  Q4T_CHECK(Ok(registry.DeleteVoice(id, Access())));
  Q4T_CHECK(registry.DeleteVoice(id, Access()).http_status == 404);
  return true;
}

Q4T_TEST(voice_registry_consent_rename_is_cas_and_preserves_evidence) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  const auto id = c.value.GetString("id");
  VoiceConsentBinding old, renamed;
  Q4T_CHECK(Ok(registry.BindConsent(id, Access(), &old)));
  auto update = registry.UpdateConsent(id, "Renamed", Access());
  Q4T_CHECK(Ok(update) && update.value.GetString("name") == "Renamed" &&
            update.value.GetInt("created_at") == c.value.GetInt("created_at"));
  Q4T_CHECK(Ok(registry.BindConsent(id, Access(), &renamed)));
  Q4T_CHECK(renamed.revision == old.revision + 1 &&
            renamed.identity == old.identity &&
            renamed.source_sha256 == old.source_sha256);
  const auto r = Sample(id);
  Q4T_CHECK(
      registry
          .CreateSampleVoice(r, Evidence(r.audio_sample.bytes), old, Access())
          .http_status == 409);
  return true;
}

Q4T_TEST(voice_registry_concurrent_creates_commit_only_one_matched_revision) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  const auto id = c.value.GetString("id");
  VoiceConsentBinding binding;
  Q4T_CHECK(Ok(registry.BindConsent(id, Access(), &binding)));
  const auto r = Sample(id);
  auto evidence = Evidence(r.audio_sample.bytes);
  ObjectReply a, b;
  std::jthread first(
      [&] { a = registry.CreateSampleVoice(r, evidence, binding, Access()); });
  std::jthread second(
      [&] { b = registry.CreateSampleVoice(r, evidence, binding, Access()); });
  first.join();
  second.join();
  Q4T_CHECK(a.ok() != b.ok());
  Q4T_CHECK((a.ok() ? b : a).http_status == 409);
  Q4T_CHECK(
      Stored(store, ObjectKind::kVoiceConsent, id).value.GetInt("revision") ==
      2);
  return true;
}

Q4T_TEST(
    voice_registry_source_mismatch_nonfinite_and_wrong_speaker_fail_closed) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  auto request = Consent();
  auto evidence = Evidence(request.recording.bytes);
  evidence.source_sha256[0] = evidence.source_sha256[0] == 'a' ? 'b' : 'a';
  Q4T_CHECK(
      !registry.CreateConsent(request, evidence, "phrase", Access()).ok());
  evidence = Evidence(request.recording.bytes);
  Q4T_CHECK(!registry.CreateConsent(request, evidence, {}, Access()).ok());
  evidence.identity[0] = std::numeric_limits<float>::quiet_NaN();
  Q4T_CHECK(
      !registry.CreateConsent(request, evidence, "phrase", Access()).ok());
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  VoiceConsentBinding binding;
  Q4T_CHECK(
      Ok(registry.BindConsent(c.value.GetString("id"), Access(), &binding)));
  const auto r = Sample(binding.id);
  evidence = Evidence(r.audio_sample.bytes);
  for (auto& v : evidence.identity) v = -v;
  Q4T_CHECK(registry.CreateSampleVoice(r, evidence, binding, Access()).code ==
            "voice_speaker_mismatch");
  evidence = Evidence(r.audio_sample.bytes);
  evidence.condition.assign(2048, 0);
  Q4T_CHECK(!registry.CreateSampleVoice(r, evidence, binding, Access()).ok());
  evidence.condition[0] = std::numeric_limits<float>::infinity();
  Q4T_CHECK(!registry.CreateSampleVoice(r, evidence, binding, Access()).ok());
  Q4T_CHECK(Stored(store, ObjectKind::kVoiceConsent, binding.id)
                .value.GetInt("revision") == 1);
  return true;
}

Q4T_TEST(voice_registry_quota_failure_does_not_publish_voice_or_touch_consent) {
  Fixture f;
  f.options.max_object_bytes = 16 * 1024;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  VoiceConsentBinding binding;
  Q4T_CHECK(
      Ok(registry.BindConsent(c.value.GetString("id"), Access(), &binding)));
  const auto r = Sample(binding.id);
  const auto failed = registry.CreateSampleVoice(
      r, Evidence(r.audio_sample.bytes), binding, Access());
  Q4T_CHECK(!failed.ok() && !failed.commit_unknown);
  Q4T_CHECK(Stored(store, ObjectKind::kVoiceConsent, binding.id)
                .value.GetInt("revision") == 1);
  ObjectRequest list;
  list.kind = ObjectKind::kVoice;
  list.operation = ObjectOperation::kList;
  list.scope = Access().scope;
  const auto result = store.Execute(list, Access().deadline);
  Q4T_CHECK(Ok(result) && result.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(voice_registry_deadline_and_cancellation_leave_no_public_consent) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto r = Consent();
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(
      registry
          .CreateConsent(r, Evidence(r.recording.bytes), "phrase", cancelled)
          .http_status == 499);
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(
      registry.CreateConsent(r, Evidence(r.recording.bytes), "phrase", expired)
          .http_status == 504);
  const auto list = registry.ListConsents(20, {}, Access());
  Q4T_CHECK(Ok(list) && list.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(voice_registry_pagination_projects_metadata_without_biometrics) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  Q4T_CHECK(Ok(Create(registry)) && Ok(Create(registry)));
  const auto first = registry.ListConsents(1, {}, Access());
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more") &&
            first.value.GetArray("data")->array.size() == 1);
  const auto last = first.value.GetString("last_id");
  const auto second = registry.ListConsents(1, last, Access());
  Q4T_CHECK(Ok(second) && !second.value.GetBool("has_more") &&
            second.value.GetString("last_id") != last);
  const auto wire = OutputJson(first.value);
  for (const auto* field : {"identity", "blob", "transcript", "source_sha256",
                            "verified_phrase", "condition"})
    Q4T_CHECK(wire.find(field) == wire.npos);
  Q4T_CHECK(registry.ListConsents(101, {}, Access()).http_status == 400);
  return true;
}

Q4T_TEST(voice_registry_retention_keeps_audio_edges_and_corruption_is_local) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto c = Create(registry);
  Q4T_CHECK(Ok(c));
  VoiceConsentBinding binding;
  Q4T_CHECK(
      Ok(registry.BindConsent(c.value.GetString("id"), Access(), &binding)));
  const auto r = Sample(binding.id);
  const auto v = registry.CreateSampleVoice(r, Evidence(r.audio_sample.bytes),
                                            binding, Access());
  Q4T_CHECK(Ok(v));
  const auto id = v.value.GetString("id");
  const auto saved = Stored(store, ObjectKind::kVoice, id);
  Q4T_CHECK(Ok(saved));
  auto data = *saved.value.Find("data");
  const auto info = InspectObjectRetention("voice", data);
  Q4T_CHECK(!info.uncertain && info.references.size() == 2 &&
            std::find(info.references.begin(), info.references.end(),
                      binding.id) != info.references.end());
  auto missing = data;
  std::erase_if(missing.object,
                [](const auto& field) { return field.first == "consent"; });
  Q4T_CHECK(InspectObjectRetention("voice", missing).uncertain);
  for (auto& [key, field] : data.object)
    if (key == "condition_revision") field.str = "wrong checkpoint";
  ObjectRequest update;
  update.operation = ObjectOperation::kPut;
  update.kind = ObjectKind::kVoice;
  update.scope = Access().scope;
  update.id = id;
  update.expected_revision = saved.value.GetInt("revision");
  update.data = std::move(data);
  update.ttl_seconds = 0;
  Q4T_CHECK(Ok(store.Execute(update, Access().deadline)));
  VoiceSynthesisBinding out;
  Q4T_CHECK(registry.Resolve(id, Access(), &out).code == "voice_state_invalid");
  Q4T_CHECK(Ok(registry.DeleteVoice(id, Access())));
  Q4T_CHECK(Ok(registry.RetrieveConsent(binding.id, Access())));
  return true;
}

Q4T_TEST(voice_registry_uncertain_staging_keeps_ids_without_replaying) {
  Fixture f;
  f.options.io_timeout_ms = 250;
  struct Injection {
    Injection() { ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack"); }
    ~Injection() { ObjectWorkerEntryForTesting({}); }
  } inject;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  const auto failed = Create(registry);
  Q4T_CHECK(!failed.ok() && failed.commit_unknown &&
            failed.ClientCode() == "object_commit_unknown");
  const auto id = failed.value.GetString("id");
  Q4T_CHECK(id.starts_with("cons_") &&
            failed.value.GetString("stage_id").starts_with("tool_run_"));
  const auto end = Clock::now() + 1s;
  while (store.Health().pending && Clock::now() < end)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(!store.Health().pending);
  Q4T_CHECK(registry.RetrieveConsent(id, Access()).http_status == 404);
  Q4T_CHECK(registry.ListConsents(20, {}, Access())
                .value.GetArray("data")
                ->array.empty());
  Q4T_CHECK(store.Health().workers_started == 2);
  return true;
}
