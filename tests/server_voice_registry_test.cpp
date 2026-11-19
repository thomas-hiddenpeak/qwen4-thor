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

namespace {
VoiceSampleEvidence ReferenceEvidence(std::string_view bytes,
                                      size_t samples = 3841) {
  auto result = Evidence(bytes);
  result.reference_samples = samples;
  result.duration_ns = samples * 1000000000ULL / 24000;
  result.reference_codes.resize((samples + 1919) / 1920);
  for (size_t i = 0; i < result.reference_codes.size(); ++i)
    for (size_t group = 0; group < 16; ++group)
      result.reference_codes[i][group] = (i * 37 + group * 61) % 2048;
  return result;
}
bool BindNewConsent(VoiceRegistry& registry, VoiceConsentBinding* out) {
  const auto consent = Create(registry);
  return Ok(consent) &&
         Ok(registry.BindConsent(consent.value.GetString("id"), Access(), out));
}
q4t::io::Json* Mutable(q4t::io::Json& value, const std::string& key) {
  for (auto& [name, field] : value.object)
    if (name == key) return &field;
  throw std::runtime_error("voice fixture missing field");
}
void SetNumber(q4t::io::Json* out, double number) {
  // A parsed number retains its original spelling; replace both forms.
  *out = {};
  out->type = q4t::io::Json::Type::kNumber;
  out->number = number;
}
ObjectReply Replace(ObjectStore& store, const std::string& id,
                    const q4t::io::Json& data) {
  const auto saved = Stored(store, ObjectKind::kVoice, id);
  if (!saved.ok()) return saved;
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = ObjectKind::kVoice;
  request.scope = Access().scope;
  request.id = id;
  request.expected_revision = saved.value.GetInt("revision");
  request.data = data;
  request.ttl_seconds = 0;
  return store.Execute(request, Access().deadline);
}
}  // namespace

Q4T_TEST(
    voice_icl_persists_exact_reference_with_legacy_vector_and_private_metadata) {
  Fixture f;
  std::string id;
  const auto expected = ReferenceEvidence("fixture sample");
  {
    ObjectStore store(f.options);
    VoiceRegistry registry(store);
    VoiceConsentBinding consent;
    Q4T_CHECK(BindNewConsent(registry, &consent));
    const auto request = Sample(consent.id);
    const auto created =
        registry.CreateSampleVoice(request, expected, consent, Access());
    Q4T_CHECK(Ok(created) && created.value.object.size() == 5 &&
              !created.value.Find("reference") &&
              !created.value.Find("condition"));
    id = created.value.GetString("id");
    const auto record = Stored(store, ObjectKind::kVoice, id);
    Q4T_CHECK(Ok(record));
    const auto* data = record.value.Find("data");
    Q4T_CHECK(data->GetString("format") == "q4t.voice.v1" &&
              data->GetArray("condition")->array.size() == 2048);
    const auto* reference = data->Find("reference");
    Q4T_CHECK(reference &&
              reference->GetString("format") == "q4t.tts-reference.v1" &&
              reference->GetInt("sample_rate") == 24000 &&
              reference->GetInt("samples") == 3841 &&
              reference->GetArray("codes")->array.size() == 3);
  }
  ObjectStore reopened(f.options);
  VoiceRegistry registry(reopened);
  VoiceSynthesisBinding result;
  Q4T_CHECK(Ok(registry.Resolve(id, Access(), &result)));
  Q4T_CHECK(result.reference_codes == expected.reference_codes &&
            result.reference_samples == 3841 &&
            result.reference_transcript == expected.transcript &&
            result.condition == expected.condition);
  return true;
}

Q4T_TEST(voice_icl_absent_reference_keeps_existing_xvector_records) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  VoiceConsentBinding consent;
  Q4T_CHECK(BindNewConsent(registry, &consent));
  const auto request = Sample(consent.id);
  const auto expected = Evidence(request.audio_sample.bytes);
  const auto created =
      registry.CreateSampleVoice(request, expected, consent, Access());
  Q4T_CHECK(Ok(created));
  VoiceSynthesisBinding result;
  result.reference_codes = ReferenceEvidence("fixture").reference_codes;
  result.reference_samples = 3841;
  result.reference_transcript = "previous request";
  Q4T_CHECK(
      Ok(registry.Resolve(created.value.GetString("id"), Access(), &result)));
  Q4T_CHECK(result.reference_codes.empty() && !result.reference_samples &&
            result.reference_transcript.empty() &&
            result.condition == expected.condition);
  return true;
}

Q4T_TEST(
    voice_icl_invalid_encoder_evidence_never_publishes_or_changes_consent) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  VoiceConsentBinding consent;
  Q4T_CHECK(BindNewConsent(registry, &consent));
  const auto request = Sample(consent.id);
  for (int mutation = 0; mutation < 7; ++mutation) {
    auto evidence = ReferenceEvidence(request.audio_sample.bytes);
    if (mutation == 0) evidence.reference_samples = 0;
    if (mutation == 1) evidence.reference_samples = 1440001;
    if (mutation == 2) evidence.reference_codes.pop_back();
    if (mutation == 3) evidence.reference_codes[0][15] = 2048;
    if (mutation == 4) evidence.reference_codes[2][15] = -1;
    if (mutation == 5) evidence.duration_ns += 1;
    if (mutation == 6) evidence.reference_codes.clear();
    const auto result =
        registry.CreateSampleVoice(request, evidence, consent, Access());
    Q4T_CHECK(result.http_status == 400 &&
              result.code == "invalid_voice_evidence");
  }
  const auto saved = Stored(store, ObjectKind::kVoiceConsent, consent.id);
  Q4T_CHECK(Ok(saved) && saved.value.GetInt("revision") == consent.revision);
  ObjectRequest list;
  list.operation = ObjectOperation::kList;
  list.kind = ObjectKind::kVoice;
  list.scope = Access().scope;
  const auto result = store.Execute(list, Access().deadline);
  Q4T_CHECK(Ok(result) && result.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(
    voice_icl_malformed_saved_reference_fails_locally_without_silent_downgrade) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  VoiceConsentBinding consent;
  Q4T_CHECK(BindNewConsent(registry, &consent));
  const auto request = Sample(consent.id);
  const auto created = registry.CreateSampleVoice(
      request, ReferenceEvidence(request.audio_sample.bytes), consent,
      Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  const auto saved = Stored(store, ObjectKind::kVoice, id);
  Q4T_CHECK(Ok(saved));
  const auto data = *saved.value.Find("data");
  for (int mutation = 0; mutation < 10; ++mutation) {
    auto bad = data;
    auto* reference = Mutable(bad, "reference");
    if (mutation == 0) *reference = {};
    if (mutation == 1) Mutable(*reference, "format")->str = "unknown";
    if (mutation == 2) Mutable(*reference, "codec_sha256")->str[0] = '0';
    if (mutation == 3) SetNumber(Mutable(*reference, "sample_rate"), 16000);
    if (mutation == 4) SetNumber(Mutable(*reference, "samples"), 3841.5);
    if (mutation == 5) Mutable(*reference, "codes")->array[0].array.pop_back();
    if (mutation == 6)
      SetNumber(&Mutable(*reference, "codes")->array[0].array[15], 2048);
    if (mutation == 7)
      SetNumber(&Mutable(*reference, "codes")->array[0].array[15], .5);
    if (mutation == 8) Mutable(*reference, "codes")->array.clear();
    if (mutation == 9)
      SetNumber(Mutable(bad, "duration_ns"), data.GetNumber("duration_ns") + 1);
    Q4T_CHECK(Ok(Replace(store, id, bad)));
    VoiceSynthesisBinding out;
    out.id = "unchanged";
    const auto reply = registry.Resolve(id, Access(), &out);
    if (reply.code != "voice_state_invalid")
      std::fprintf(stderr, "reference mutation %d: %d %s\n", mutation,
                   reply.http_status, reply.code.c_str());
    Q4T_CHECK(reply.http_status == 503 && reply.code == "voice_state_invalid");
    Q4T_CHECK(out.id == "unchanged" && out.reference_codes.empty());
    Q4T_CHECK(Ok(registry.RetrieveConsent(consent.id, Access())));
  }
  Q4T_CHECK(Ok(registry.DeleteVoice(id, Access())));
  return true;
}

Q4T_TEST(voice_icl_keeps_owner_isolation_revocation_and_owned_condition) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  VoiceConsentBinding consent;
  Q4T_CHECK(BindNewConsent(registry, &consent));
  const auto request = Sample(consent.id);
  const auto expected = ReferenceEvidence(request.audio_sample.bytes);
  const auto created =
      registry.CreateSampleVoice(request, expected, consent, Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  VoiceSynthesisBinding out;
  Q4T_CHECK(registry.Resolve(id, Access("other"), &out).http_status == 404);
  Q4T_CHECK(out.reference_codes.empty());
  Q4T_CHECK(Ok(registry.Resolve(id, Access(), &out)));
  Q4T_CHECK(Ok(registry.DeleteConsent(consent.id, Access())));
  Q4T_CHECK(registry.Resolve(id, Access(), &out).http_status == 404);
  Q4T_CHECK(out.reference_codes == expected.reference_codes &&
            out.reference_transcript == expected.transcript);
  return true;
}

Q4T_TEST(voice_icl_full_minute_reference_roundtrips_without_dropping_groups) {
  Fixture f;
  ObjectStore store(f.options);
  VoiceRegistry registry(store);
  VoiceConsentBinding consent;
  Q4T_CHECK(BindNewConsent(registry, &consent));
  const auto request = Sample(consent.id);
  const auto expected = ReferenceEvidence(request.audio_sample.bytes, 1440000);
  const auto created =
      registry.CreateSampleVoice(request, expected, consent, Access());
  Q4T_CHECK(Ok(created));
  VoiceSynthesisBinding out;
  Q4T_CHECK(
      Ok(registry.Resolve(created.value.GetString("id"), Access(), &out)));
  Q4T_CHECK(out.reference_codes.size() == 750 &&
            out.reference_codes == expected.reference_codes);
  return true;
}
