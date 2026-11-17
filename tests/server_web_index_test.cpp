#include "q4t/server/web_index.h"

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access(std::string scope = "client:web-a") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root =
        std::filesystem::absolute(env ? env : ".q4t-work/web-index-cpu");
    std::filesystem::create_directories(root);
    auto path = (root / "web-index-XXXXXX").string();
    if (!mkdtemp(path.data()))
      throw std::runtime_error("web index fixture path");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
std::vector<WebPageReceipt> Sources(std::string text = "Actual source alpha") {
  WebPageReceipt receipt;
  receipt.source.urls = {"https://example.com/source"};
  receipt.source.content.bytes = text;
  receipt.source.content.mime_type = "text/plain";
  receipt.source.content.sha256 = Digest(text);
  receipt.page.url = receipt.source.urls.back();
  receipt.page.title = "Source";
  receipt.page.text = text;
  receipt.page.source_sha256 = receipt.source.content.sha256;
  return {std::move(receipt)};
}
const std::string policy = Digest("web-index-fixture-policy-v1");
ObjectRequest ReadRequest(const std::string& scope = "client:web-a") {
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.scope = scope;
  request.id = "webidx_" + policy;
  return request;
}
}  // namespace

Q4T_TEST(web_index_persists_exact_sources_and_offline_search_across_restart) {
  Fixture fixture;
  WebIndexSnapshot before;
  {
    ObjectStore objects(fixture.options);
    Q4T_CHECK(Ok(WebIndexStore(objects).Publish(Access(), policy, Sources(), 0,
                                                3600, &before)));
    Q4T_CHECK(before.revision == 1 && before.pages.size() == 1 &&
              before.pages[0].source_sha256 == Digest("Actual source alpha"));
  }
  ObjectStore objects(fixture.options);
  WebIndexSnapshot after;
  Q4T_CHECK(Ok(WebIndexStore(objects).Read(Access(), policy, &after)));
  Q4T_CHECK(after.id == before.id && after.revision == before.revision &&
            after.expires_at == before.expires_at &&
            after.pages[0].text == before.pages[0].text);
  WebSearchOptions options;
  options.external_web_access = false;
  const std::vector<std::string> queries{"alpha"};
  std::vector<WebSearchHit> hits;
  Q4T_CHECK(
      Ok(SearchWebPages(after.pages, queries, options, Access(), 10, &hits)) &&
      hits.size() == 1 && hits[0].page == 0);
  return true;
}

Q4T_TEST(web_index_scope_and_configuration_identity_are_both_required) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(index.Publish(Access(), policy, Sources("alpha only"), 0, 3600,
                             &snapshot)));
  Q4T_CHECK(index.Read(Access("client:web-b"), policy, &snapshot).code ==
            "web_index_unavailable");
  Q4T_CHECK(index.Read(Access(), Digest("other config"), &snapshot).code ==
            "web_index_unavailable");
  Q4T_CHECK(Ok(index.Publish(Access("client:web-b"), policy,
                             Sources("beta only"), 0, 3600, &snapshot)));
  Q4T_CHECK(snapshot.pages[0].text == "beta only");
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.pages[0].text == "alpha only");
  return true;
}

Q4T_TEST(web_index_cas_conflict_preserves_prior_complete_snapshot) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(
      index.Publish(Access(), policy, Sources("first"), 0, 3600, &snapshot)));
  Q4T_CHECK(
      index.Publish(Access(), policy, Sources("discarded"), 0, 3600, &snapshot)
              .http_status == 409 &&
      snapshot.pages.empty());
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.revision == 1 && snapshot.pages[0].text == "first");
  Q4T_CHECK(Ok(
      index.Publish(Access(), policy, Sources("second"), 1, 3600, &snapshot)));
  Q4T_CHECK(snapshot.revision == 2 && snapshot.pages[0].text == "second");
  Q4T_CHECK(index.Erase(Access(), policy, 1).http_status == 409);
  Q4T_CHECK(Ok(index.Erase(Access(), policy, 2)));
  Q4T_CHECK(index.Read(Access(), policy, &snapshot).code ==
            "web_index_unavailable");
  return true;
}

Q4T_TEST(web_index_lost_write_receipt_keeps_original_identity_without_replay) {
  Fixture fixture;
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  ScopeExit restore([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  const auto reply =
      index.Publish(Access(), policy, Sources(), 0, 3600, &snapshot);
  Q4T_CHECK(reply.commit_unknown &&
            reply.value.GetString("id") == "webidx_" + policy &&
            snapshot.pages.empty());
  const auto until = Clock::now() + 3s;
  while (objects.Health().pending && Clock::now() < until)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.revision == 1 &&
            snapshot.pages[0].text == "Actual source alpha");
  Q4T_CHECK(
      index.Publish(Access(), policy, Sources("no replay"), 0, 3600, &snapshot)
          .http_status == 409);
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.revision == 1);
  return true;
}

Q4T_TEST(web_index_invalid_acquisition_receipts_cannot_replace_valid_sources) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(index.Publish(Access(), policy, Sources(), 0, 3600, &snapshot)));
  for (int failure = 0; failure < 4; ++failure) {
    auto sources = Sources();
    if (failure == 0) sources[0].source.content.bytes = "different bytes";
    if (failure == 1) sources[0].page.url = "https://forged.example/";
    if (failure == 2) sources[0].source.urls.clear();
    if (failure == 3) sources.push_back(sources[0]);
    Q4T_CHECK(
        index.Publish(Access(), policy, sources, 1, 3600, &snapshot).code ==
        "web_index_invalid");
  }
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.revision == 1);
  return true;
}

Q4T_TEST(web_index_byte_quota_and_cancel_do_not_mutate_previous_index) {
  Fixture fixture;
  fixture.options.max_object_bytes = 4096;
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(index.Publish(Access(), policy, Sources(), 0, 3600, &snapshot)));
  Q4T_CHECK(index
                .Publish(Access(), policy, Sources(std::string(8192, 'x')), 1,
                         3600, &snapshot)
                .http_status == 413);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(
      index.Publish(cancelled, policy, Sources("cancelled"), 1, 3600, &snapshot)
          .http_status == 499);
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.revision == 1 &&
            snapshot.pages[0].text == "Actual source alpha");
  return true;
}

Q4T_TEST(web_index_retention_recognizes_inline_data_without_false_references) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(index.Publish(Access(), policy, Sources("file_fake resp_fake"),
                             0, 3600, &snapshot)));
  auto result = objects.Execute(ReadRequest(), Access().deadline);
  Q4T_CHECK(Ok(result));
  const auto& data = *result.value.Find("data");
  auto info = InspectObjectRetention("tool_run", data);
  Q4T_CHECK(!info.uncertain && !info.active &&
            !info.collect_when_unreferenced && info.references.empty());
  const auto owner = Digest(Access().scope), digest = Digest(OutputJson(data));
  ObjectRetentionNode node{owner, "tool_run", snapshot.id, digest,
                           1,     2,          4096,        &info};
  ObjectRetentionPlan plan;
  Q4T_CHECK(PlanObjectRetention({&node, 1}, 3, 0, 1, &plan) &&
            plan.selected == std::vector<size_t>{0});
  auto bad = data;
  Set(&bad, "policy_sha256", String("broken"));
  Q4T_CHECK(InspectObjectRetention("tool_run", bad).uncertain);
  return true;
}

Q4T_TEST(web_index_real_expiry_and_empty_snapshot_have_distinct_results) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  WebIndexStore index(objects);
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(index.Publish(Access(), policy, {}, 0, 5, &snapshot)));
  const auto expires = snapshot.expires_at;
  Q4T_CHECK(Ok(index.Read(Access(), policy, &snapshot)) &&
            snapshot.pages.empty() && snapshot.expires_at == expires);
  const auto until = Clock::now() + 6s;
  while (std::time(nullptr) < expires && Clock::now() < until)
    std::this_thread::sleep_for(10ms);
  Q4T_CHECK(index.Read(Access(), policy, &snapshot).code ==
                "web_index_unavailable" &&
            snapshot.pages.empty() && snapshot.revision == 0);
  return true;
}
