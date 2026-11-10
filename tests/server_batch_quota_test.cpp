#include "q4t/server/batch_quota.h"

#include <unistd.h>

#include <barrier>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>

#include "../src/server/object_protocol.h"
#include "q4t/server/batch_preparation.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
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
    const auto base =
        std::filesystem::absolute(out ? out : ".q4t-work/batch-quota");
    std::filesystem::create_directories(base);
    auto path = (base / "batch-quota-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("Batch quota fixture");
    options.directory = path + "/objects";
    options.io_timeout_ms = 250;
    options.restart_backoff_ms = 0;
    options.batch_model_queue_tokens = 10;
    options.batch_client_model_queue_tokens = 10;
  }
};
BinaryAccess Access(std::string scope = "client:alpha") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
struct Work {
  std::string id, scope, model;
  BatchInputRow row;
  BatchPreparedInput prepared;
};
Work Prepare(ObjectStore& objects, uint64_t tokens,
             std::string scope = "client:alpha", std::string model = "cpu") {
  Work work;
  work.scope = std::move(scope);
  work.model = std::move(model);
  work.row.line = 1;
  work.row.custom_id = "original-item";
  work.row.body = Object(
      {{"model", String(work.model)}, {"input", String("Read record 42")}});
  const auto text =
      OutputJson(Object({{"custom_id", String(work.row.custom_id)},
                         {"method", String("POST")},
                         {"url", String("/v1/responses")},
                         {"body", work.row.body}})) +
      '\n';
  work.row.wire_bytes = text.size();
  FileUpload upload(objects, Access(work.scope),
                    "multipart/form-data; boundary=b");
  const auto wire =
      "--b\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch"
      "\r\n--b\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"input.jsonl\"\r\n\r\n" +
      text + "\r\n--b--\r\n";
  if (!Ok(upload.Feed(wire, Access().deadline)))
    throw std::runtime_error("quota File feed");
  auto file = upload.Finish();
  if (!Ok(file)) throw std::runtime_error("quota File finish");
  BatchCreate options;
  options.endpoint = "/v1/responses";
  options.input_file_id = file.value.GetString("id");
  const auto created = BatchState(objects).Create(Access(work.scope), options);
  if (!Ok(created)) throw std::runtime_error("quota Batch create");
  work.id = created.value.GetString("id");
  ChatPlanConfig config;
  config.model = work.model;
  BatchInputMeter meter;
  if (tokens) {
    // Explicit fixed CPU measurement exercises storage/admission, not BPE.
    meter = [tokens](const auto&, const auto&, const auto&, const auto&,
                     const auto&, BatchInputBudget* result) {
      *result = {tokens, true};
      return ObjectReply{};
    };
  }
  if (!Ok(BatchPreparation(objects).Prepare(Access(work.scope), work.id,
                                            "/v1/responses", work.row, config,
                                            &work.prepared, meter)))
    throw std::runtime_error("quota frozen input");
  return work;
}
ObjectReply Append(ObjectStore& objects, const Work& work) {
  BatchRowPosition row{work.row.line,
                       work.row.offset,
                       work.row.wire_bytes,
                       Digest(work.row.custom_id),
                       work.prepared.reference,
                       work.prepared.binding_sha256,
                       work.prepared.inline_input,
                       work.prepared.input_budget};
  return BatchState(objects).AppendValidatedRow(Access(work.scope), work.id,
                                                row, work.model);
}
ObjectReply Root(ObjectStore& objects, const Work& work) {
  return BatchState(objects).Load(Access(work.scope), work.id);
}
ObjectReply Fail(ObjectStore& objects, const Work& work) {
  return BatchState(objects).FailValidation(
      Access(work.scope), work.id, Error(400, "fixture_failed", "CPU fixture"));
}
ObjectReply Edit(ObjectStore& objects, const Work& work,
                 const std::function<void(Json&)>& mutate) {
  const auto old = Root(objects, work);
  if (!old.ok()) return old;
  ObjectRequest put;
  put.operation = ObjectOperation::kPut;
  put.kind = ObjectKind::kBatch;
  put.scope = work.scope;
  put.id = work.id;
  put.expected_revision = old.value.GetInt("revision");
  put.created_at = old.value.GetInt("created_at");
  put.expires_at = old.value.GetInt("expires_at");
  put.data = *old.value.Find("data");
  mutate(put.data);
  return objects.Execute(put, Access().deadline);
}
bool Limited(const ObjectReply& reply) {
  return reply.http_status == 429 && !reply.commit_unknown &&
         reply.code == "batch_enqueued_token_limit_exceeded";
}
bool Empty(ObjectStore& objects, const Work& work) {
  const auto root = Root(objects, work);
  return root.ok() && root.value.Find("data")->GetInt("validated_rows") == 0 &&
         root.value.Find("data")->GetArray("pages")->array.empty() &&
         !root.value.Find("data")->Has("enqueued_input_tokens");
}
bool OnlineObject(ObjectStore& objects, const std::string& scope) {
  ObjectRequest put;
  put.operation = ObjectOperation::kPut;
  put.kind = ObjectKind::kResponse;
  put.scope = scope;
  put.id = NewObjectId(put.kind);
  put.data = Object({{"fixture", String("independent online state")}});
  return Ok(objects.Execute(put, Access().deadline));
}
}  // namespace

Q4T_TEST(batch_quota_actual_worker_client_model_and_atomic_rejection) {
  Fixture fixture;
  fixture.options.batch_client_model_queue_tokens = 6;
  ObjectStore objects(fixture.options);
  const auto a = Prepare(objects, 6);
  const auto a_more = Prepare(objects, 1);
  const auto b = Prepare(objects, 4, "client:beta");
  const auto b_more = Prepare(objects, 1, "client:beta");
  const auto other = Prepare(objects, 6, "client:alpha", "other-model");
  Q4T_CHECK(Ok(Append(objects, a)) && Limited(Append(objects, a_more)) &&
            Empty(objects, a_more));
  Q4T_CHECK(Ok(Append(objects, b)) && Limited(Append(objects, b_more)) &&
            Empty(objects, b_more) && Ok(Append(objects, other)));
  Q4T_CHECK(OnlineObject(objects, a.scope) && OnlineObject(objects, b.scope));
  return true;
}

Q4T_TEST(batch_quota_concurrent_clients_have_one_durable_winner) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto a = Prepare(objects, 7);
  const auto b = Prepare(objects, 7, "client:beta");
  std::barrier start(3);
  auto run = [&](const Work& work) {
    start.arrive_and_wait();
    return Append(objects, work);
  };
  auto first = std::async(std::launch::async, run, std::cref(a));
  auto second = std::async(std::launch::async, run, std::cref(b));
  start.arrive_and_wait();
  const auto ar = first.get(), br = second.get();
  Q4T_CHECK(ar.ok() != br.ok());
  const auto& winner = ar.ok() ? a : b;
  const auto& loser = ar.ok() ? b : a;
  Q4T_CHECK(Limited(ar.ok() ? br : ar) && Empty(objects, loser));
  Q4T_CHECK(Ok(Fail(objects, winner)) && Ok(Append(objects, loser)));
  return true;
}

Q4T_TEST(batch_quota_restart_rebuilds_and_lower_limits_allow_drain) {
  Fixture fixture;
  Work a, b, next, extra;
  {
    ObjectStore objects(fixture.options);
    a = Prepare(objects, 6);
    b = Prepare(objects, 4, "client:beta");
    next = Prepare(objects, 1);
    extra = Prepare(objects, 1);
    Q4T_CHECK(Ok(Append(objects, a)) && Ok(Append(objects, b)));
  }
  fixture.options.batch_model_queue_tokens = 5;
  fixture.options.batch_client_model_queue_tokens = 5;
  ObjectStore objects(fixture.options);
  Q4T_CHECK(Ok(Root(objects, a)) && Limited(Append(objects, next)) &&
            Ok(Fail(objects, a)) && Ok(Append(objects, next)));
  BatchState batches(objects);
  Q4T_CHECK(Ok(batches.Cancel(Access(b.scope), b.id)) &&
            Limited(Append(objects, extra)));
  Q4T_CHECK(Ok(batches.FinishCancelled(Access(b.scope), b.id)) &&
            Ok(Append(objects, extra)));
  return true;
}

Q4T_TEST(batch_quota_unknown_receipt_and_journal_recovery_charge_once) {
  for (const auto* selector : {"--q4t-object-worker-batch-budget-lost-ack",
                               "--q4t-object-worker-commit-pause"}) {
    Fixture fixture;
    Work original, next;
    {
      ObjectStore objects(fixture.options);
      original = Prepare(objects, 6);
      next = Prepare(objects, 5, "client:beta");
    }
    ObjectWorkerEntryForTesting(selector);
    ScopeExit reset([] { ObjectWorkerEntryForTesting({}); });
    {
      ObjectStore objects(fixture.options);
      const auto written = Append(objects, original);
      Q4T_CHECK(!written.ok() && written.commit_unknown);
    }
    ObjectWorkerEntryForTesting({});
    ObjectStore objects(fixture.options);
    const auto recovered = Root(objects, original);
    Q4T_CHECK(Ok(recovered) &&
              recovered.value.Find("data")->GetInt("metered_rows") == 1 &&
              recovered.value.Find("data")->GetInt("enqueued_input_tokens") ==
                  6);
    Q4T_CHECK(Limited(Append(objects, next)) &&
              !Append(objects, original).ok() && Ok(Fail(objects, original)) &&
              Ok(Append(objects, next)));
  }
  return true;
}

Q4T_TEST(batch_quota_execution_cancel_and_finalization_retain_until_terminal) {
  for (const auto* terminal : {"completed", "cancelled"}) {
    Fixture fixture;
    fixture.options.batch_model_queue_tokens = 7;
    fixture.options.batch_client_model_queue_tokens = 7;
    ObjectStore objects(fixture.options);
    const auto work = Prepare(objects, 7), next = Prepare(objects, 1);
    BatchState batches(objects);
    Q4T_CHECK(Ok(Append(objects, work)) &&
              Ok(batches.CompleteValidation(Access(), work.id, work.model)));
    auto started = batches.StartRow(Access(), work.id, 0);
    Q4T_CHECK(Ok(started));
    if (std::string_view(terminal) == "cancelled")
      Q4T_CHECK(Ok(batches.Cancel(Access(), work.id)) &&
                !batches.FinishCancelled(Access(), work.id).ok());
    Q4T_CHECK(Limited(Append(objects, next)));
    BatchRowResult result;
    result.request_id = started.value.GetString("request_id");
    result.http_status = 200;
    result.response_body = R"({"object":"response","output":[]})";
    Q4T_CHECK(Ok(batches.FinishRow(Access(), work.id, 0, work.row.custom_id,
                                   result)) &&
              Ok(batches.BeginFinalization(Access(), work.id, terminal)));
    Q4T_CHECK(Limited(Append(objects, next)));
    Q4T_CHECK(Ok(batches.Finalize(Access(), work.id)) &&
              Ok(Append(objects, next)));
  }
  return true;
}

Q4T_TEST(batch_quota_expired_restored_input_keeps_charge_until_finalization) {
  Fixture fixture;
  fixture.options.batch_model_queue_tokens = 7;
  fixture.options.batch_client_model_queue_tokens = 7;
  Work work, next;
  {
    ObjectStore objects(fixture.options);
    work = Prepare(objects, 7);
    next = Prepare(objects, 1);
    Q4T_CHECK(Ok(Append(objects, work)) &&
              Ok(BatchState(objects).CompleteValidation(Access(), work.id,
                                                        work.model)));
  }
  // Age this private stopped fixture by a day, preserving its signed record
  // shape and original 24h interval. No serving process or clock is changed.
  const auto path = std::filesystem::path(fixture.options.directory) /
                    (Digest(work.scope) + ".batch." + work.id + ".json");
  Json wrapper;
  {
    std::ifstream in(path);
    const std::string text{std::istreambuf_iterator<char>(in), {}};
    Q4T_CHECK(Parse(text, &wrapper));
  }
  auto& record = Field(wrapper, "record");
  auto& batch = Field(Field(record, "data"), "batch");
  for (auto* object : {&record, &batch})
    for (const auto* key : {"created_at", "expires_at"})
      Set(object, key, Number(object->GetInt(key) - kBatchWindowSeconds - 2));
  Set(&wrapper, "sha256", String(Digest(OutputJson(record))));
  {
    std::ofstream out(path, std::ios::trunc);
    out << OutputJson(wrapper);
    Q4T_CHECK(out.good());
  }
  ObjectStore objects(fixture.options);
  BatchState batches(objects);
  Q4T_CHECK(Limited(Append(objects, next)) &&
            Ok(batches.BeginFinalization(Access(), work.id, "expired")) &&
            Limited(Append(objects, next)));
  BatchRowResult expired;
  expired.error =
      Error(408, "batch_expired", "Original execution window ended");
  Q4T_CHECK(Ok(batches.FinishRow(Access(), work.id, 0, work.row.custom_id,
                                 expired)) &&
            Ok(batches.Finalize(Access(), work.id)) &&
            Ok(Append(objects, next)));
  return true;
}

Q4T_TEST(batch_quota_cannot_rewrite_budget_model_pages_or_delete_active_root) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto work = Prepare(objects, 6), next = Prepare(objects, 5);
  Q4T_CHECK(Ok(Append(objects, work)));
  for (int i = 0; i < 5; ++i) {
    const auto edited = Edit(objects, work, [&](Json& data) {
      if (i == 0) Set(&data, "enqueued_input_tokens", Number(1));
      if (i == 1) Set(&data, "enqueued_input_tokens", Number(7));
      if (i == 2) Set(&data, "canonical_model", String("quota-escape"));
      if (i == 3) Set(&data, "pages", Array());
      if (i == 4) {
        Set(&data, "metered_rows", Number(2));
        Set(&data, "validated_rows", Number(2));
        Set(&data, "enqueued_input_tokens", Number(7));
      }
    });
    Q4T_CHECK(!edited.ok() && !edited.commit_unknown &&
              edited.code == "batch_budget_conflict");
  }
  ObjectRequest erase;
  erase.operation = ObjectOperation::kErase;
  erase.kind = ObjectKind::kBatch;
  erase.scope = work.scope;
  erase.id = work.id;
  const auto erased = objects.Execute(erase, Access().deadline);
  Q4T_CHECK(erased.code == "batch_active_delete_conflict" &&
            Limited(Append(objects, next)));
  const auto root = Root(objects, work);
  Q4T_CHECK(Ok(root) &&
            root.value.Find("data")->GetInt("enqueued_input_tokens") == 6);
  return true;
}

Q4T_TEST(batch_quota_legacy_unknown_affects_only_relevant_batch_admission) {
  Fixture fixture;
  Work legacy, next, other;
  {
    ObjectStore objects(fixture.options);
    legacy = Prepare(objects, 0);
    next = Prepare(objects, 1);
    other = Prepare(objects, 1, "client:beta", "other-model");
    Q4T_CHECK(Ok(Append(objects, legacy)));
  }
  ObjectStore objects(fixture.options);
  const auto blocked = Append(objects, next);
  Q4T_CHECK(blocked.code == "batch_input_budget_unavailable" &&
            !blocked.commit_unknown && Empty(objects, next) &&
            Ok(Append(objects, other)) && OnlineObject(objects, next.scope));
  Q4T_CHECK(Ok(Fail(objects, legacy)) && Ok(Append(objects, next)));
  return true;
}

Q4T_TEST(batch_quota_exact_wide_rebuild_and_unknown_model_quarantine) {
  BatchQueueQuota quota;
  const BatchQueueCharge maximum{"model", kBatchTokenLimit, false};
  // Arithmetic at the actual maximum record count, without fabricating disk
  // capacity/performance evidence. Sum exceeds uint64_t, then drains exactly.
  for (int i = 0; i < 100000; ++i) quota.Add("owner", maximum);
  BatchQueueChange extra{{}, {"model", 1, false}};
  Q4T_CHECK(Limited(quota.Check("owner", std::span(&extra, 1), kBatchTokenLimit,
                                kBatchTokenLimit)));
  for (int i = 0; i < 99999; ++i) quota.Remove("owner", maximum);
  Q4T_CHECK(Limited(quota.Check("owner", std::span(&extra, 1), kBatchTokenLimit,
                                kBatchTokenLimit)));
  quota.Remove("owner", maximum);
  Q4T_CHECK(Ok(quota.Check("owner", std::span(&extra, 1), 1, 1)));
  const BatchQueueCharge unknown{"", 0, true};
  quota.Add("legacy", unknown);
  Q4T_CHECK(quota.Check("owner", std::span(&extra, 1), 1, 1).code ==
            "batch_input_budget_unavailable");
  BatchQueueChange drain{unknown, {}};
  Q4T_CHECK(Ok(quota.Check("legacy", std::span(&drain, 1), 1, 1)));
  quota.Remove("legacy", unknown);
  Q4T_CHECK(Ok(quota.Check("owner", std::span(&extra, 1), 1, 1)));
  return true;
}
