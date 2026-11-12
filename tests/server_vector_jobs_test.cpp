#include "q4t/server/vector_jobs.h"

#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/application_owner.h"
#include "q4t/server/files.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/responses_owner.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
struct Fixture {
  ObjectStoreOptions objects;
  VectorJobsOptions jobs;
  ClientAdmission clients;
  explicit Fixture(uint32_t alpha_tpm = 10000) {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!out) throw std::runtime_error("Missing vector jobs output");
    auto path = (std::filesystem::path(out) / "vector-jobs-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Vector job fixture");
    objects.directory = path + "/objects";
    objects.restart_backoff_ms = 0;
    objects.restart_limit = 10;
    objects.io_timeout_ms = 100;
    jobs.scan_interval = 5ms;
    jobs.retry_delay = 5ms;
    jobs.retry_backoff = 20ms;
    jobs.claim_seconds = 30;
    jobs.ingestion.dimensions = 32;
    auto entries = Array();
    for (const auto* id : {"alpha", "beta"})
      entries.array.push_back(
          Object({{"id", String(id)},
                  {"max_inflight", Number(1)},
                  {"max_control_inflight", Number(1)},
                  {"max_prompt_tokens", Number(4096)},
                  {"max_output_tokens", Number(64)},
                  {"max_body_bytes", Number(4096)},
                  {"requests_per_minute", Number(1)},
                  {"tokens_per_minute",
                   Number(std::string(id) == "alpha" ? alpha_tpm : 10000)}}));
    GatewayClientPolicy policy;
    if (!ParseGatewayClientPolicy(
            OutputJson(Object({{"version", Number(3)},
                               {"revision", Number(0)},
                               {"fair_queue", Boolean(false)},
                               {"gateway_token", String(std::string(64, 'a'))},
                               {"clients", std::move(entries)}})),
            &policy) ||
        !clients.Configure(std::move(policy)))
      throw std::runtime_error("Vector client fixture");
  }
};
BinaryAccess Access(std::string scope = "client:alpha") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok()) std::fprintf(stderr, "%s\n", reply.code.c_str());
  return reply.ok();
}
template <typename F>
bool Wait(F&& done, std::chrono::milliseconds budget = 3000ms) {
  const auto end = Clock::now() + budget;
  do {
    if (done()) return true;
    std::this_thread::sleep_for(1ms);
  } while (Clock::now() < end);
  return false;
}
struct Seeded {
  std::string store, file, member;
};
Seeded Seed(ObjectStore& objects, const BinaryAccess& access = Access()) {
  const auto wire =
      "--f\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
      "user_data\r\n--f\r\nContent-Disposition: form-data; "
      "name=\"file\"; filename=\"MODEL.md\"\r\n\r\nProject "
      "document\r\n--f--\r\n";
  FileUpload upload(objects, access, "multipart/form-data; boundary=f");
  if (!Ok(upload.Feed(wire, access.deadline)))
    throw std::runtime_error("Upload fixture");
  auto file = upload.Finish();
  auto store = VectorStores(objects).Create(access, Object());
  if (!Ok(file) || !Ok(store)) throw std::runtime_error("Seed fixture");
  Seeded seed{store.value.GetString("id"), file.value.GetString("id"), {}};
  if (!Ok(VectorStores(objects).Attach(
          access, seed.store, Object({{"file_id", String(seed.file)}}))))
    throw std::runtime_error("Attach fixture");
  seed.member = VectorStores(objects)
                    .LoadRecord(access, seed.store, seed.file)
                    .value.GetString("id");
  return seed;
}
// Explicit CPU byte tokens and BF16 mathematical vectors, no GPU model claim.
ObjectReply Encode(std::string_view text, const VectorSearchControl& control,
                   VectorEncodedText* output) {
  return EncodeVectorText(
      text,
      [](auto source, auto* tokens) {
        for (unsigned char c : source) tokens->push_back(c);
        return q4t::Status{};
      },
      [](uint32_t token, auto* piece) {
        *piece = std::string(1, static_cast<char>(token));
        return q4t::Status{};
      },
      control, output);
}
ObjectReply Hidden(const std::vector<int32_t>& tokens,
                   EmbeddingStepResult* result) {
  result->committed_input = tokens.size();
  result->hidden.assign(1024, 0);
  result->hidden[0] = 0x3f80;
  return {};
}
VectorJobs::Embedder Embed(std::atomic<int>& calls) {
  return [&](const auto& tokens, const auto& client, auto& permit,
             const auto& control, auto* result) {
    if (!permit.Holds(client, ClientLane::kData) || control->Cancelled())
      return Error(500, "fixture_owner", "Missing execution ownership");
    ++calls;
    return Hidden(tokens, result);
  };
}
std::string Status(ObjectStore& objects, const Seeded& seed,
                   const BinaryAccess& access = Access()) {
  return VectorStores(objects)
      .RetrieveFile(access, seed.store, seed.file)
      .value.GetString("status");
}
std::string GoneOwner() {
  int pipe_fds[2];
  if (pipe(pipe_fds)) throw std::runtime_error("Identity pipe");
  const pid_t pid = fork();
  if (pid == 0) {
    close(pipe_fds[0]);
    const auto text = OutputJson(CurrentResponseOwner());
    const auto n = write(pipe_fds[1], text.data(), text.size());
    _exit(n == static_cast<ssize_t>(text.size()) ? 0 : 1);
  }
  close(pipe_fds[1]);
  char text[257];
  const auto n = read(pipe_fds[0], text, sizeof(text));
  close(pipe_fds[0]);
  int status = 0;
  if (pid < 0 || waitpid(pid, &status, 0) != pid || status || n <= 0)
    throw std::runtime_error("Child identity unavailable");
  return {text, static_cast<size_t>(n)};
}
struct Injection {
  explicit Injection(std::string entry) { ObjectWorkerEntryForTesting(entry); }
  ~Injection() { ObjectWorkerEntryForTesting({}); }
};
}  // namespace

Q4T_TEST(vector_jobs_persistent_queue_executes_exactly_once_and_retains_index) {
  Fixture f;
  Seeded seed;
  {
    ObjectStore initial(f.objects);
    seed = Seed(initial);
  }
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(calls == 1 && f.clients.RateUsage(0).requests == 0);
  const auto record =
      VectorStores(objects).LoadFile(Access(), seed.store, seed.file);
  Q4T_CHECK(Ok(record));
  VectorIndexIdentity identity;
  Q4T_CHECK(ParseVectorIndexIdentity(
      *record.value.Find("data")->Find("index_identity"), &identity));
  InputSnapshotRef snapshot;
  Q4T_CHECK(ParseInputSnapshotReference(*record.value.Find("data")
                                             ->GetArray("input_snapshots")
                                             ->array.front()
                                             .Find("snapshot"),
                                        &snapshot));
  VectorIndex index;
  InputSnapshotStore snapshots(objects);
  Q4T_CHECK(
      Ok(ReadVectorIndex(snapshots, Access(), snapshot, identity, &index)));
  Q4T_CHECK(index.text == "Project document" && index.chunks.size() == 1);
  return true;
}

Q4T_TEST(vector_jobs_cancel_is_durable_and_waits_for_execution_drain) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto seed = Seed(objects);
  std::atomic<bool> entered{false}, cancelled{false}, release{false};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode,
                  [&](const auto& tokens, const auto&, auto&,
                      const auto& control, auto* result) {
                    entered = true;
                    Wait([&] { return control->Cancelled(); });
                    cancelled = true;
                    Wait([&] { return release.load(); });
                    result->committed_input = tokens.size();
                    return Error(499, "request_cancelled", "Cancelled fixture");
                  });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  const auto receipt = jobs.Cancel(Access(), seed.store, seed.file);
  Q4T_CHECK(Ok(receipt) && receipt.value.GetString("status") == "in_progress");
  Q4T_CHECK(Wait([&] { return cancelled.load(); }));
  Q4T_CHECK(Status(objects, seed) == "in_progress");
  release = true;
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "cancelled"; }));
  Q4T_CHECK(f.clients.RateUsage(0).reserved == 0);
  return true;
}

Q4T_TEST(vector_jobs_queued_cancel_survives_worker_restart_without_model) {
  Fixture f;
  Seeded seed;
  {
    ObjectStore objects(f.objects);
    seed = Seed(objects);
    Q4T_CHECK(Ok(
        VectorStores(objects).RequestCancel(Access(), seed.store, seed.file)));
  }
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "cancelled"; }));
  Q4T_CHECK(calls == 0);
  return true;
}

Q4T_TEST(vector_jobs_dead_process_is_recovered_with_original_attempt_budget) {
  Fixture f;
  const auto gone = GoneOwner();  // Real child has exited, not monitor absence.
  ObjectStore objects(f.objects);
  const auto seed = Seed(objects);
  VectorIngestionClaim claim;
  Q4T_CHECK(Ok(VectorStores(objects).Claim(Access(), seed.store, seed.file,
                                           gone, 30, &claim)));
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  auto record =
      VectorStores(objects).LoadRecord(Access(), seed.store, seed.file);
  Q4T_CHECK(record.value.Find("data")->Find("job")->GetInt("attempts") == 2);
  Q4T_CHECK(calls == 1 && jobs.Health().recovered == 1);
  return true;
}

Q4T_TEST(vector_jobs_live_and_unknown_owners_are_never_stolen) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto live = Seed(objects), unknown = Seed(objects);
  VectorIngestionClaim first, second;
  VectorStores stores(objects);
  Q4T_CHECK(Ok(stores.Claim(Access(), live.store, live.file,
                            OutputJson(CurrentResponseOwner()), 1, &first)));
  Q4T_CHECK(Ok(stores.Claim(Access(), unknown.store, unknown.file,
                            "unavailable-process-monitor", 1, &second)));
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return jobs.Health().recovery_pending == 2; }));
  // Even expired ownership remains protected while its death is unproven.
  Q4T_CHECK(Wait([&] { return std::time(nullptr) > first.deadline_at; }));
  Q4T_CHECK(calls == 0 && jobs.Health().owned == 0);
  Q4T_CHECK(Status(objects, live) == "in_progress");
  Q4T_CHECK(Status(objects, unknown) == "in_progress");
  return true;
}

Q4T_TEST(vector_jobs_shared_tpm_wait_is_fair_and_does_not_spend_attempts) {
  Fixture f(1);
  ObjectStore objects(f.objects);
  const auto alpha = Seed(objects);
  const auto beta = Seed(objects, Access("client:beta"));
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] {
    return jobs.Health().rate_deferrals > 0 &&
           Status(objects, beta, Access("client:beta")) == "completed";
  }));
  Q4T_CHECK(Status(objects, alpha) == "in_progress" && calls == 1);
  auto record =
      VectorStores(objects).LoadRecord(Access(), alpha.store, alpha.file);
  Q4T_CHECK(record.value.Find("data")->Find("job")->GetInt("attempts") == 1);
  Q4T_CHECK(f.clients.RateUsage(0).reserved == 0);
  Q4T_CHECK(Ok(jobs.Cancel(Access(), alpha.store, alpha.file)));
  Q4T_CHECK(Wait([&] { return Status(objects, alpha) == "cancelled"; }));
  return true;
}

Q4T_TEST(vector_jobs_lost_claim_and_parent_receipts_never_replay_model) {
  Fixture f;
  Seeded seed;
  {
    ObjectStore objects(f.objects);
    seed = Seed(objects);
  }
  Injection injection("--q4t-object-worker-application-lost-ack");
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(calls == 1 && objects.Health().workers_started >= 2);
  return true;
}

Q4T_TEST(vector_jobs_lost_snapshot_receipt_publishes_original_without_replay) {
  Fixture f;
  Seeded seed;
  {
    ObjectStore objects(f.objects);
    seed = Seed(objects);
  }
  Injection injection("--q4t-object-worker-snapshot-lost-ack");
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(calls == 1 && objects.Health().workers_started >= 2);
  return true;
}

Q4T_TEST(vector_jobs_deleted_source_closes_failure_and_other_client_continues) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto missing = Seed(objects);
  const auto beta = Seed(objects, Access("client:beta"));
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), missing.file)));
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] {
    return Status(objects, missing) == "failed" &&
           Status(objects, beta, Access("client:beta")) == "completed";
  }));
  Q4T_CHECK(calls == 1 && !jobs.Health().failed);
  return true;
}

Q4T_TEST(vector_jobs_exception_after_consumption_is_terminal_without_replay) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto seed = Seed(objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode,
                  [&](const auto& tokens, const auto&, auto&, const auto&,
                      auto* result) -> ObjectReply {
                    ++calls;
                    result->committed_input = tokens.size();
                    throw std::runtime_error("Explicit CPU execution failure");
                  });
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "failed"; }));
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(calls == 1 && f.clients.RateUsage(0).reserved == 0);
  Q4T_CHECK(f.clients.RateUsage(0).settled_tokens > 0);
  return true;
}

Q4T_TEST(vector_jobs_two_controllers_cannot_claim_same_invocation) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto seed = Seed(objects);
  std::atomic<int> calls{0};
  VectorJobs first(objects, f.clients, f.jobs, Encode, Embed(calls));
  VectorJobs second(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  Q4T_CHECK(Wait(
      [&] { return first.Health().owned == 0 && second.Health().owned == 0; }));
  Q4T_CHECK(calls == 1);
  return true;
}

Q4T_TEST(vector_jobs_shutdown_drains_and_closes_without_model_replay) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto seed = Seed(objects);
  std::atomic<bool> entered{false};
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode,
                  [&](const auto& tokens, const auto&, auto&,
                      const auto& control, auto* result) {
                    ++calls;
                    entered = true;
                    Wait([&] { return control->Cancelled(); });
                    result->committed_input = tokens.size();
                    return Error(499, "server_shutdown", "Stopped fixture");
                  });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  jobs.RequestStop();
  jobs.Join();
  Q4T_CHECK(Status(objects, seed) == "failed" && calls == 1);
  Q4T_CHECK(jobs.Health().active == 0 && f.clients.RateUsage(0).reserved == 0);
  return true;
}

Q4T_TEST(vector_jobs_detach_reattach_cannot_publish_original_execution) {
  Fixture f;
  ObjectStore objects(f.objects);
  const auto seed = Seed(objects);
  std::atomic<bool> entered{false}, release{false};
  std::atomic<int> calls{0};
  VectorJobs jobs(
      objects, f.clients, f.jobs, Encode,
      [&](const auto& tokens, const auto&, auto&, const auto&, auto* result) {
        if (++calls == 1) {
          entered = true;
          Wait([&] { return release.load(); });
        }
        return Hidden(tokens, result);
      });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  VectorStores stores(objects);
  Q4T_CHECK(Ok(stores.Detach(Access(), seed.store, seed.file)));
  Q4T_CHECK(Ok(stores.Attach(Access(), seed.store,
                             Object({{"file_id", String(seed.file)}}))));
  const auto record = stores.LoadRecord(Access(), seed.store, seed.file);
  Q4T_CHECK(Ok(record) && record.value.GetString("id") != seed.member);
  release = true;
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  Q4T_CHECK(Wait([&] { return jobs.Health().owned == 0; }));
  Q4T_CHECK(calls == 2);
  return true;
}

Q4T_TEST(
    vector_jobs_partial_start_failure_drains_and_recovers_auxiliary_owner) {
  Fixture f;
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  ApplicationOwner<VectorJobs> owner({5ms, 100ms});
  const auto factory = [&] {
    return std::make_unique<VectorJobs>(objects, f.clients, f.jobs, Encode,
                                        Embed(calls));
  };
  FailApplicationThreadForTesting(ApplicationThreadKind::kVectorScanner);
  Q4T_CHECK(!owner.TryStart(factory) && !owner && calls == 0);
  Q4T_CHECK(Wait([&] {
    owner.TryStart(factory);
    return bool(owner);
  }));
  const auto seed = Seed(objects);
  Q4T_CHECK(Wait([&] { return Status(objects, seed) == "completed"; }));
  Q4T_CHECK(owner.StartupHealth().recoveries == 1 && calls == 1);
  Json health;
  Q4T_CHECK(
      q4t::io::ParseJson(VectorJobsHealthJson(owner->Health(), true), &health));
  Q4T_CHECK(!health.GetBool("readiness_dependency", true));
  return true;
}

namespace {
struct BatchSeed {
  std::string store, id;
  std::vector<std::string> files;
  Json request;
};
BatchSeed BatchSources(ObjectStore& objects, size_t count = 2) {
  BatchSeed seed;
  auto ids = Array();
  for (size_t i = 0; i < count; ++i) {
    auto file = Seed(objects);
    if (!Ok(VectorStores(objects).Detach(Access(), file.store, file.file)))
      throw std::runtime_error("Detach seed source");
    if (seed.store.empty()) seed.store = file.store;
    seed.files.push_back(file.file);
    ids.array.push_back(String(file.file));
  }
  seed.request = Object({{"file_ids", std::move(ids)}});
  return seed;
}
void CreateBatch(ObjectStore& objects, BatchSeed* seed) {
  auto result =
      VectorStores(objects).CreateBatch(Access(), seed->store, seed->request);
  if (!Ok(result)) throw std::runtime_error("Create batch fixture");
  seed->id = result.value.GetString("id");
}
ObjectReply Batch(ObjectStore& objects, const BatchSeed& seed) {
  return VectorStores(objects).RetrieveBatch(Access(), seed.store, seed.id);
}
bool Finished(ObjectStore& objects, const BatchSeed& seed, std::string state) {
  return Wait(
      [&] { return Batch(objects, seed).value.GetString("status") == state; });
}
ObjectReply Raw(ObjectStore& objects, ObjectKind kind, const std::string& id) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.kind = kind;
  request.id = id;
  return objects.Execute(request, Clock::now() + 5s);
}
}  // namespace

Q4T_TEST(vector_batches_options_pagination_and_atomic_membership) {
  Fixture f;
  ObjectStore objects(f.objects);
  VectorStores stores(objects);
  auto seed = BatchSources(objects);
  Set(&seed.request, "attributes", Object({{"category", String("project")}}));
  CreateBatch(objects, &seed);
  auto created = Batch(objects, seed);
  Q4T_CHECK(seed.id.starts_with("vsfb_") &&
            created.value.GetString("object") == "vector_store.files_batch");
  Q4T_CHECK(created.value.Find("file_counts")->GetInt("in_progress") == 2);
  auto first = stores.ListBatchFiles(
      Access(), seed.store, seed.id,
      Object({{"limit", Number(1)}, {"order", String("asc")}}));
  Q4T_CHECK(Ok(first) && first.value.GetBool("has_more"));
  Q4T_CHECK(first.value.GetArray("data")
                ->array.front()
                .Find("attributes")
                ->GetString("category") == "project");
  auto second =
      stores.ListBatchFiles(Access(), seed.store, seed.id,
                            Object({{"after", *first.value.Find("last_id")},
                                    {"order", String("asc")}}));
  Q4T_CHECK(Ok(second) && !second.value.GetBool("has_more") &&
            second.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(stores.Retrieve(Access(), seed.store)
                .value.Find("file_counts")
                ->GetInt("total") == 2);
  auto individual = BatchSources(objects, 1);
  individual.request =
      Object({{"files",
               Array({Object({{"file_id", String(individual.files[0])},
                              {"attributes",
                               Object({{"category", String("specific")}})}})})},
              {"attributes", Object({{"category", String("ignored")}})}});
  CreateBatch(objects, &individual);
  Q4T_CHECK(stores.RetrieveFile(Access(), individual.store, individual.files[0])
                .value.Find("attributes")
                ->GetString("category") == "specific");
  return true;
}

Q4T_TEST(
    vector_batches_bad_forms_and_foreign_sources_publish_no_partial_files) {
  Fixture f;
  ObjectStore objects(f.objects);
  VectorStores stores(objects);
  const auto seed = BatchSources(objects);
  auto duplicate = Object(
      {{"file_ids", Array({String(seed.files[0]), String(seed.files[0])})}});
  Q4T_CHECK(stores.CreateBatch(Access(), seed.store, duplicate).http_status ==
            400);
  auto both = seed.request;
  Set(&both, "files", Array());
  Q4T_CHECK(stores.CreateBatch(Access(), seed.store, both).http_status == 400);
  Q4T_CHECK(
      stores.CreateBatch(Access(), seed.store, Object({{"file_ids", Json()}}))
          .http_status == 400);
  auto ids = Array();
  for (size_t i = 0; i < 2001; ++i) ids.array.push_back(String("file_missing"));
  Q4T_CHECK(
      stores.CreateBatch(Access(), seed.store, Object({{"file_ids", ids}}))
          .http_status == 400);
  ids.array.pop_back();
  Q4T_CHECK(
      stores.CreateBatch(Access(), seed.store, Object({{"file_ids", ids}}))
          .http_status == 404);
  const auto foreign = Seed(objects, Access("client:beta"));
  Q4T_CHECK(
      stores
          .CreateBatch(Access(), seed.store,
                       Object({{"file_ids", Array({String(seed.files[0]),
                                                   String(foreign.file)})}}))
          .http_status == 404);
  Q4T_CHECK(stores.Retrieve(Access(), seed.store)
                .value.Find("file_counts")
                ->GetInt("total") == 0);
  auto raw = Raw(objects, ObjectKind::kVectorStore, seed.store);
  Q4T_CHECK(Ok(raw) && !raw.value.Find("data")->Has("batches"));
  return true;
}

Q4T_TEST(vector_batches_execution_and_attribute_changes_update_receipt) {
  Fixture f;
  ObjectStore objects(f.objects);
  VectorStores stores(objects);
  auto seed = BatchSources(objects);
  CreateBatch(objects, &seed);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Finished(objects, seed, "completed"));
  Q4T_CHECK(
      Batch(objects, seed).value.Find("file_counts")->GetInt("completed") == 2);
  Q4T_CHECK(calls == 2);
  Q4T_CHECK(Ok(stores.UpdateFile(
      Access(), seed.store, seed.files[0],
      Object({{"attributes", Object({{"owner", String("actual")}})}}))));
  auto page = stores.ListBatchFiles(Access(), seed.store, seed.id, Object());
  Q4T_CHECK(Ok(page));
  bool changed = false;
  for (const auto& file : page.value.GetArray("data")->array)
    if (file.GetString("id") == seed.files[0])
      changed = file.Find("attributes")->GetString("owner") == "actual";
  Q4T_CHECK(changed);
  Q4T_CHECK(stores.CancelBatch(Access(), seed.store, seed.id)
                .value.GetString("status") == "completed");
  Q4T_CHECK(stores.RetrieveBatch(Access("client:beta"), seed.store, seed.id)
                .http_status == 404);
  return true;
}

Q4T_TEST(vector_batches_cancel_waits_for_actual_drain_and_keeps_source_files) {
  Fixture f;
  ObjectStore objects(f.objects);
  VectorStores stores(objects);
  auto seed = BatchSources(objects);
  CreateBatch(objects, &seed);
  std::atomic<bool> entered{false}, release{false};
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode,
                  [&](const auto& tokens, const auto&, auto&,
                      const auto& control, auto* result) {
                    ++calls;
                    entered = true;
                    Wait([&] { return control->Cancelled(); });
                    Wait([&] { return release.load(); });
                    result->committed_input = tokens.size();
                    return Error(499, "cancelled",
                                 "Explicit fixture cancellation");
                  });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  const auto cancel = stores.CancelBatch(Access(), seed.store, seed.id);
  Q4T_CHECK(Ok(cancel) && cancel.value.GetString("status") == "in_progress");
  Q4T_CHECK(Batch(objects, seed).value.GetString("status") == "in_progress");
  release = true;
  Q4T_CHECK(Finished(objects, seed, "cancelled") && calls == 1);
  for (const auto& file : seed.files)
    Q4T_CHECK(Ok(Files(objects).Retrieve(Access(), file)));
  Q4T_CHECK(
      Batch(objects, seed).value.Find("file_counts")->GetInt("cancelled") == 2);
  return true;
}

Q4T_TEST(vector_batches_detach_hides_content_and_retains_old_execution) {
  Fixture f;
  ObjectStore objects(f.objects);
  VectorStores stores(objects);
  auto seed = BatchSources(objects, 1);
  CreateBatch(objects, &seed);
  const auto original = stores.LoadRecord(Access(), seed.store, seed.files[0]);
  Q4T_CHECK(Ok(original));
  const auto member_id = original.value.GetString("id");
  std::atomic<bool> entered{false}, release{false};
  std::atomic<int> calls{0};
  VectorJobs jobs(
      objects, f.clients, f.jobs, Encode,
      [&](const auto& tokens, const auto&, auto&, const auto&, auto* result) {
        if (++calls == 1) {
          entered = true;
          Wait([&] { return release.load(); });
        }
        return Hidden(tokens, result);
      });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  Q4T_CHECK(Ok(stores.Detach(Access(), seed.store, seed.files[0])));
  Q4T_CHECK(stores.LoadFile(Access(), seed.store, seed.files[0]).http_status ==
            404);
  Q4T_CHECK(Batch(objects, seed).value.GetString("status") == "in_progress");
  Q4T_CHECK(
      Ok(stores.LoadExecution(Access(), seed.store, seed.files[0], member_id)));
  auto raw = Raw(objects, ObjectKind::kVectorStoreBatch, seed.id);
  const auto retention =
      InspectObjectRetention("vector_store_batch", *raw.value.Find("data"));
  Q4T_CHECK(!retention.uncertain &&
            retention.references == std::vector<std::string>{member_id});
  Q4T_CHECK(Ok(stores.Attach(Access(), seed.store,
                             Object({{"file_id", String(seed.files[0])}}))));
  release = true;
  Q4T_CHECK(Finished(objects, seed, "cancelled"));
  Q4T_CHECK(Wait([&] {
    return stores.RetrieveFile(Access(), seed.store, seed.files[0])
               .value.GetString("status") == "completed";
  }));
  Q4T_CHECK(calls == 2);
  raw = Raw(objects, ObjectKind::kVectorStoreBatch, seed.id);
  Q4T_CHECK(
      InspectObjectRetention("vector_store_batch", *raw.value.Find("data"))
          .references.empty());
  Q4T_CHECK(stores.ListBatchFiles(Access(), seed.store, seed.id, Object())
                .value.GetArray("data")
                ->array.empty());
  return true;
}

Q4T_TEST(vector_batches_detached_dead_owner_recovers_after_worker_restart) {
  Fixture f;
  const auto gone = GoneOwner();
  BatchSeed seed;
  {
    ObjectStore objects(f.objects);
    VectorStores stores(objects);
    seed = BatchSources(objects, 1);
    CreateBatch(objects, &seed);
    VectorIngestionClaim claim;
    Q4T_CHECK(Ok(
        stores.Claim(Access(), seed.store, seed.files[0], gone, 30, &claim)));
    Q4T_CHECK(Ok(stores.Detach(Access(), seed.store, seed.files[0])));
    Q4T_CHECK(Batch(objects, seed).value.GetString("status") == "in_progress");
  }
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Finished(objects, seed, "cancelled"));
  Q4T_CHECK(calls == 0 && jobs.Health().recovered == 1);
  return true;
}

Q4T_TEST(vector_batches_lost_create_receipt_keeps_original_atomic_batch) {
  Fixture f;
  BatchSeed seed;
  {
    ObjectStore objects(f.objects);
    seed = BatchSources(objects);
  }
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(f.objects);
    auto created =
        VectorStores(objects).CreateBatch(Access(), seed.store, seed.request);
    Q4T_CHECK(created.commit_unknown);
    seed.id = created.value.GetString("id");
    Q4T_CHECK(seed.id.starts_with("vsfb_"));
  }
  ObjectStore objects(f.objects);
  Q4T_CHECK(Ok(Batch(objects, seed)));
  Q4T_CHECK(Batch(objects, seed).value.Find("file_counts")->GetInt("total") ==
            2);
  auto root = Raw(objects, ObjectKind::kVectorStore, seed.store);
  Q4T_CHECK(root.value.Find("data")->GetArray("batches")->array.size() == 1);
  return true;
}

Q4T_TEST(vector_batches_lost_cancel_receipt_recovers_without_model) {
  Fixture f;
  BatchSeed seed;
  {
    ObjectStore objects(f.objects);
    seed = BatchSources(objects);
    CreateBatch(objects, &seed);
  }
  {
    Injection injection("--q4t-object-worker-application-lost-ack");
    ObjectStore objects(f.objects);
    Q4T_CHECK(VectorStores(objects)
                  .CancelBatch(Access(), seed.store, seed.id)
                  .commit_unknown);
  }
  ObjectStore objects(f.objects);
  std::atomic<int> calls{0};
  VectorJobs jobs(objects, f.clients, f.jobs, Encode, Embed(calls));
  Q4T_CHECK(Finished(objects, seed, "cancelled") && calls == 0);
  return true;
}

Q4T_TEST(vector_batches_queued_detach_closes_receipt_without_model) {
  Fixture f;
  ObjectStore objects(f.objects);
  VectorStores stores(objects);
  auto seed = BatchSources(objects, 1);
  CreateBatch(objects, &seed);
  Q4T_CHECK(Ok(stores.Detach(Access(), seed.store, seed.files[0])));
  Q4T_CHECK(Batch(objects, seed).value.GetString("status") == "cancelled");
  const auto raw = Raw(objects, ObjectKind::kVectorStoreBatch, seed.id);
  Q4T_CHECK(
      InspectObjectRetention("vector_store_batch", *raw.value.Find("data"))
          .references.empty());
  Q4T_CHECK(Ok(Files(objects).Retrieve(Access(), seed.files[0])));
  return true;
}
