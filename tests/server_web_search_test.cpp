#include "q4t/server/web_search.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <thread>

#include "../src/server/object_protocol.h"
#include "../src/server/web_page_internal.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:web-search", Clock::now() + 10s, {}}; }
Json JsonText(const char* text) {
  Json value;
  if (!Parse(text, &value)) throw std::runtime_error("web fixture JSON");
  return value;
}
FileInputContent Html(std::string text) {
  FileInputContent result;
  result.bytes = std::move(text);
  result.mime_type = "text/html; charset=utf-8";
  return result;
}
ObjectReply Decode(const FileInputContent& input, WebPage* page,
                   WebPageLimits limits = {}) {
  return ParseWebPage(input, "https://example.com/news/page.html", limits,
                      Access(), page);
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
WebPage Page(std::string url, std::string title, std::string text) {
  WebPage page;
  page.url = std::move(url);
  page.title = std::move(title);
  page.text = std::move(text);
  page.source_sha256 = Digest(page.text);
  return page;
}
}  // namespace

int WebPageSandboxFixture(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--web-page-sandbox-fixture")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv) ||
      !web_detail::LoadPageRuntime("text/html; charset=utf-8").ok() ||
      !detail::EnterNativeDocumentSandbox())
    return 2;
  uint32_t result = 0;
  if (open("/etc/passwd", O_RDONLY | O_CLOEXEC) == -1 && errno == EPERM)
    result |= 1;
  if (socket(AF_INET, SOCK_STREAM, 0) == -1 && errno == EPERM) result |= 2;
  if (fork() == -1 && errno == EPERM) result |= 4;
  if (mmap(nullptr, 4096, PROT_READ | PROT_WRITE | PROT_EXEC,
           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0) == MAP_FAILED &&
      errno == EPERM)
    result |= 8;
  WebPage page;
  if (web_detail::DecodePage(Html("<p>Sandbox text</p>"),
                             "https://example.com/", {}, &page)
          .ok() &&
      page.text == "Sandbox text")
    result |= 16;
  return detail::WorkerTransfer(&result, sizeof(result), true) ? 0 : 3;
}

Q4T_TEST(web_page_actual_sandbox_denies_host_io_process_and_executable_memory) {
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  std::unique_ptr<detail::GrammarProcess> process;
  Q4T_CHECK(detail::GrammarProcess::Start("--web-page-sandbox-fixture", control,
                                          &process));
  uint32_t result = 0;
  Q4T_CHECK(
      process->Transfer(&result, sizeof(result), false, Clock::now() + 10s));
  process.reset();
  Q4T_CHECK(result == 31 && control.counters->reaped == 1 &&
            control.counters->active == 0);
  return true;
}

Q4T_TEST(web_search_options_defaults_and_dated_aliases) {
  WebSearchOptions options;
  ChatPlanError error;
  for (const auto* type :
       {"web_search", "web_search_2025_08_26", "web_search_preview",
        "web_search_preview_2025_03_11"}) {
    Q4T_CHECK(ParseWebSearchOptions(Object({{"type", String(type)}}), false,
                                    &options, &error));
    Q4T_CHECK(options.type == type && options.context_size == "medium" &&
              options.external_web_access && options.text && !options.images &&
              !options.unlimited_return_tokens && options.location.IsNull());
  }
  Q4T_CHECK(ParseWebSearchOptions(Object(), true, &options, &error,
                                  "web_search_options"));
  return true;
}

Q4T_TEST(web_search_options_nested_controls_and_chat_location) {
  WebSearchOptions options;
  ChatPlanError error;
  auto value = JsonText(R"({"type":"web_search","external_web_access":false,
    "search_context_size":"high","return_token_budget":"unlimited",
    "user_location":{"country":"GB","city":"London","region":null},
    "search_content_types":["image","text"],
    "image_settings":{"max_results":3,"caption":true}})");
  Q4T_CHECK(ParseWebSearchOptions(value, false, &options, &error));
  Q4T_CHECK(!options.external_web_access && options.unlimited_return_tokens &&
            options.text && options.images && options.max_images == 3 &&
            options.captions && options.context_size == "high" &&
            options.location.GetString("city") == "London");
  value = JsonText(R"({"search_context_size":"low","user_location":{
    "type":"approximate","approximate":{"country":"CN","city":"北京",
      "timezone":"Asia/Shanghai"}}})");
  Q4T_CHECK(ParseWebSearchOptions(value, true, &options, &error,
                                  "web_search_options"));
  Q4T_CHECK(options.location.GetString("city") == "北京" &&
            options.location.GetString("timezone") == "Asia/Shanghai");
  return true;
}

Q4T_TEST(web_search_options_invalid_nested_values_do_not_publish_plan) {
  WebSearchOptions options;
  ChatPlanError error;
  for (
      const auto* text :
      {R"({"type":"web_search","return_token_budget":null})",
       R"({"type":"web_search","external_web_access":1})",
       R"({"type":"web_search","search_context_size":"huge"})",
       R"({"type":"web_search","user_location":{"country":"UKK"}})",
       R"({"type":"web_search","user_location":{"type":"precise"}})",
       R"({"type":"web_search","filters":{"allowed_domains":["https://x.com"]}})",
       R"({"type":"web_search","search_content_types":["text","text"]})",
       R"({"type":"web_search","image_settings":{"caption":true}})",
       R"({"type":"web_search","search_content_types":["image"],"image_settings":{"max_results":1.5}})",
       R"({"type":"web_search","filters":{"unknown":[]}})",
       R"({"type":"web_search","ca_file":"/tmp/ca"})"}) {
    Q4T_CHECK(!ParseWebSearchOptions(JsonText(text), false, &options, &error));
    Q4T_CHECK(error.http_status == 400 && !error.param.empty() &&
              options.type == "web_search" && options.location.IsNull());
  }
  Q4T_CHECK(
      !ParseWebSearchOptions(JsonText(R"({"user_location":{"country":"US"}})"),
                             true, &options, &error, "web_search_options"));
  return true;
}

Q4T_TEST(web_search_domains_match_subdomains_without_suffix_bypass) {
  WebSearchOptions options;
  ChatPlanError error;
  Q4T_CHECK(ParseWebSearchOptions(JsonText(R"({"type":"web_search","filters":{
        "allowed_domains":["EXAMPLE.com.","example.com"],
        "blocked_domains":["private.example.com"]}})"),
                                  false, &options, &error));
  Q4T_CHECK(options.allowed_domains == std::vector<std::string>{"example.com"});
  Q4T_CHECK(WebSearchUrlAllowed("https://docs.example.com/a", options));
  Q4T_CHECK(WebSearchUrlAllowed("https://example.com./a", options));
  for (const auto* url :
       {"https://badexample.com/", "https://example.com.evil/",
        "https://private.example.com/", "https://a.private.example.com/",
        "https://example.com@evil.test/", "file:///etc/passwd"})
    Q4T_CHECK(!WebSearchUrlAllowed(url, options));
  return true;
}

Q4T_TEST(web_search_legacy_preview_live_semantics_and_new_controls) {
  WebSearchOptions options;
  ChatPlanError error;
  auto value =
      JsonText(R"({"type":"web_search_preview","external_web_access":false})");
  Q4T_CHECK(ParseWebSearchOptions(value, false, &options, &error) &&
            options.external_web_access);
  for (const auto* field : {"filters", "return_token_budget",
                            "search_content_types", "image_settings"}) {
    auto bad = value;
    Set(&bad, field, Object());
    Q4T_CHECK(!ParseWebSearchOptions(bad, false, &options, &error));
  }
  return true;
}

Q4T_TEST(web_page_html_extracts_actual_text_title_and_source_hash) {
  const auto input = Html(
      "<!doctype html><html><head><title>标题 &amp; "
      "news</title></head><body><h1>真实页面</h1>"
      "<p>Hello <b>world</b> &#x1F600;.</p></body></html>");
  WebPage page;
  Q4T_CHECK(Ok(Decode(input, &page)));
  Q4T_CHECK(page.title == "标题 & news" &&
            page.text == "真实页面 Hello world 😀." &&
            page.url == "https://example.com/news/page.html" &&
            page.source_sha256 == Digest(input.bytes));
  return true;
}

Q4T_TEST(web_page_hidden_and_active_content_never_enters_extracted_text) {
  WebPage page;
  Q4T_CHECK(Ok(Decode(
      Html("<html><head><style>BAD STYLE</style><script>BAD "
           "SCRIPT</script></head>"
           "<body><p>Visible</p><div hidden>BAD HIDDEN</div>"
           "<span aria-hidden='true'>BAD ARIA</span>"
           "<div style='display: none'>BAD INLINE</div>"
           "<template>BAD TEMPLATE</template><iframe src='file:///etc/passwd'>"
           "BAD FRAME</iframe><object>BAD "
           "OBJECT</object><p>Tail</p></body></html>"),
      &page)));
  Q4T_CHECK(page.text == "Visible Tail" && page.links.empty());
  return true;
}

Q4T_TEST(web_page_observed_url_cannot_be_replaced_by_html_canonical_metadata) {
  WebPage page;
  Q4T_CHECK(Ok(Decode(
      Html("<head><base href='https://cdn.example.com/docs/'>"
           "<link rel='canonical' href='https://forged.example/secret'></head>"
           "<body><a href='../x?q=1&amp;y=2#anchor'>Actual link</a>"
           "<a href='javascript:alert(1)'>script</a><a "
           "href='file:///etc/passwd'>"
           "file</a><img src='./p.png' alt='Actual &amp; image'></body>"),
      &page)));
  Q4T_CHECK(page.url == "https://example.com/news/page.html" &&
            page.links ==
                std::vector<std::string>{"https://cdn.example.com/x?q=1&y=2"} &&
            page.images.size() == 1 &&
            page.images[0].url == "https://cdn.example.com/docs/p.png" &&
            page.images[0].caption == "Actual & image");
  return true;
}

Q4T_TEST(web_page_html_encoding_entities_and_external_entity_isolation) {
  auto input = Html("<p>caf\xe9 &copy;</p>");
  input.mime_type = "text/html; charset=iso-8859-1";
  WebPage page;
  Q4T_CHECK(Ok(Decode(input, &page)) && page.text == "café ©");
  Q4T_CHECK(Ok(
      Decode(Html("<!DOCTYPE html [<!ENTITY ext SYSTEM 'file:///etc/passwd'>]>"
                  "<html><body><p>Known &ext;</p></body></html>"),
             &page)));
  Q4T_CHECK(page.text.find("root:") == page.text.npos &&
            page.text.find("Known") != page.text.npos);
  return true;
}

Q4T_TEST(web_page_plain_text_is_not_interpreted_as_markup) {
  auto input = Html("<script>literal</script>\n中文 &amp; source\n");
  input.mime_type = "text/plain; charset=utf-8";
  WebPage page;
  Q4T_CHECK(Ok(Decode(input, &page)) && page.text == input.bytes &&
            page.links.empty() && page.title.empty());
  input.bytes = std::string("invalid\xff", 8);
  Q4T_CHECK(Decode(input, &page).code == "web_page_invalid" &&
            page.text.empty());
  return true;
}

Q4T_TEST(web_page_limits_fail_without_publishing_partial_page) {
  WebPage page;
  WebPageLimits limits;
  limits.max_text_bytes = 4;
  Q4T_CHECK(Decode(Html("<p>five!</p>"), &page, limits).http_status == 413 &&
            page.text.empty() && page.source_sha256.empty());
  limits = {};
  limits.max_links = 0;
  Q4T_CHECK(Decode(Html("<a href='/a'>real</a>"), &page, limits).http_status ==
            413);
  limits = {};
  limits.max_nodes = 3;
  Q4T_CHECK(Decode(Html("<html><body><div><p>real</p></div></body></html>"),
                   &page, limits)
                .http_status == 413);
  limits = {};
  limits.max_source_bytes = 2;
  Q4T_CHECK(Decode(Html("abc"), &page, limits).http_status == 413);
  return true;
}

Q4T_TEST(web_page_invalid_scope_url_cancel_and_deadline_start_no_workers) {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  const auto input = Html("<p>actual</p>");
  WebPage page;
  auto access = Access();
  access.scope.clear();
  Q4T_CHECK(
      ParseWebPage(input, "https://example.com/", {}, access, &page, control)
          .http_status == 503);
  access = Access();
  Q4T_CHECK(
      ParseWebPage(input, "file:///etc/passwd", {}, access, &page, control)
          .http_status == 400);
  access.cancelled = [] { return true; };
  Q4T_CHECK(
      ParseWebPage(input, "https://example.com/", {}, access, &page, control)
          .http_status == 499);
  access = Access();
  access.deadline = Clock::now() - 1s;
  Q4T_CHECK(
      ParseWebPage(input, "https://example.com/", {}, access, &page, control)
          .http_status == 408);
  Q4T_CHECK(control.counters->started == 0 && page.text.empty());
  return true;
}

Q4T_TEST(web_page_unreaped_codec_retains_slot_without_blocking_core_domain) {
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  std::unique_ptr<detail::GrammarProcess> process, core;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", control,
                                          &process));
  detail::DeferGrammarReapingForTesting(true);
  ScopeExit cleanup([&] {
    detail::DeferGrammarReapingForTesting(false);
    const auto until = Clock::now() + 2s;
    while (detail::IsolatedCleanupPending(control.domain) &&
           Clock::now() < until) {
      detail::PollRequestWorkers();
      std::this_thread::sleep_for(1ms);
    }
  });
  process.reset();
  WebPage page;
  Q4T_CHECK(Decode(Html("<p>text</p>"), &page).code ==
            "web_page_worker_capacity");
  Q4T_CHECK(
      detail::GrammarProcess::Start("--grammar-fixture-silent", {}, &core));
  core.reset();
  cleanup.Run();
  Q4T_CHECK(control.counters->started == 1 && control.counters->active == 0 &&
            control.counters->reaped == 1);
  Q4T_CHECK(Ok(Decode(Html("<p>recovered</p>"), &page)) &&
            page.text == "recovered");
  return true;
}

Q4T_TEST(web_page_actual_http_body_drives_provenance_and_parser) {
  const int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  Q4T_CHECK(fd >= 0);
  ScopeExit close_fd([&] { close(fd); });
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  socklen_t length = sizeof(address);
  Q4T_CHECK(!bind(fd, reinterpret_cast<sockaddr*>(&address), length) &&
            !getsockname(fd, reinterpret_cast<sockaddr*>(&address), &length) &&
            !listen(fd, 1));
  std::atomic<bool> served{false};
  const auto body = "<title>HTTP source</title><p>Acquired evidence 42</p>";
  std::jthread server([&](std::stop_token stop) {
    while (!stop.stop_requested()) {
      pollfd event{fd, POLLIN, 0};
      if (poll(&event, 1, 10) <= 0) continue;
      const int client =
          accept4(fd, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
      if (client < 0) continue;
      ScopeExit close_client([&] { close(client); });
      std::string request;
      const auto until = Clock::now() + 3s;
      while (!stop.stop_requested() && Clock::now() < until &&
             request.find("\r\n\r\n") == request.npos) {
        char bytes[4096];
        const auto count = recv(client, bytes, sizeof(bytes), MSG_DONTWAIT);
        if (count > 0)
          request.append(bytes, count);
        else
          std::this_thread::sleep_for(1ms);
      }
      const auto wire = std::string(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/html; "
                            "charset=utf-8\r\nContent-Length: ") +
                        std::to_string(std::strlen(body)) + "\r\n\r\n" + body;
      served = send(client, wire.data(), wire.size(), MSG_NOSIGNAL) ==
               static_cast<ssize_t>(wire.size());
      return;
    }
  });
  const auto url =
      "http://127.0.0.1:" + std::to_string(ntohs(address.sin_port)) + "/";
  FileFetchOptions policy;
  policy.public_networks = false;
  policy.allowed_networks = {"127.0.0.1/32"};
  policy.ports = {ntohs(address.sin_port)};
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = url;
  FileInputBudget budget;
  budget.max_bytes = 32768;
  FileFetchReceipt receipt;
  Q4T_CHECK(Ok(FetchFileInput(Access(), source, policy, &budget, &receipt)));
  WebPage page;
  Q4T_CHECK(Ok(
      ParseWebPage(receipt.content, receipt.urls.back(), {}, Access(), &page)));
  Q4T_CHECK(served && page.text == "Acquired evidence 42" && page.url == url &&
            page.title == "HTTP source" && page.source_sha256 == Digest(body) &&
            budget.used_files == 1 && budget.used_bytes == std::strlen(body));
  return true;
}

Q4T_TEST(web_search_cached_pages_rank_actual_english_and_chinese_without_io) {
  const std::vector<WebPage> pages{
      Page("https://example.com/a", "Thor", "Native service recovery guide"),
      Page("https://example.com/b", "恢复", "监控失能后核心业务继续服务"),
      Page("https://example.com/c", "Other", "Weather and travel")};
  WebSearchOptions options;
  options.external_web_access = false;
  std::vector<WebSearchHit> hits;
  for (const auto& [query, expected] :
       std::vector<std::pair<std::string, size_t>>{{"RECOVERY", 0},
                                                   {"监控失能", 1}}) {
    const std::vector<std::string> queries{query};
    Q4T_CHECK(Ok(SearchWebPages(pages, queries, options, Access(), 3, &hits)));
    Q4T_CHECK(hits.size() == 1 && hits[0].page == expected &&
              std::isfinite(hits[0].score) && hits[0].score > 0);
  }
  return true;
}

Q4T_TEST(web_search_filters_and_ties_keep_stable_real_source_identities) {
  const std::vector<WebPage> pages{
      Page("https://docs.example.com/z", "Guide", "LLM service"),
      Page("https://private.example.com/a", "Guide", "LLM service"),
      Page("https://docs.example.com/a", "Guide", "LLM service"),
      Page("https://notexample.com/a", "Guide", "LLM service")};
  WebSearchOptions options;
  options.allowed_domains = {"example.com"};
  options.blocked_domains = {"private.example.com"};
  const std::vector<std::string> queries{"LLM"};
  std::vector<WebSearchHit> hits;
  Q4T_CHECK(Ok(SearchWebPages(pages, queries, options, Access(), 10, &hits)));
  Q4T_CHECK(hits.size() == 2 && hits[0].page == 2 && hits[1].page == 0 &&
            hits[0].score == hits[1].score);
  return true;
}

Q4T_TEST(
    web_search_bad_snapshot_and_working_limits_never_publish_partial_hits) {
  std::vector<WebPage> pages{Page("https://example.com/a", "Guide", "LLM")};
  const std::vector<std::string> queries{"LLM"};
  std::vector<WebSearchHit> hits{{0, 1}};
  pages.push_back(pages[0]);
  Q4T_CHECK(SearchWebPages(pages, queries, {}, Access(), 10, &hits).code ==
                "web_search_index_invalid" &&
            hits.empty());
  pages.resize(1);
  pages[0].url = "not a URL";
  Q4T_CHECK(SearchWebPages(pages, queries, {}, Access(), 10, &hits).code ==
                "web_search_index_invalid" &&
            hits.empty());
  pages[0].url = "https://example.com/a";
  pages[0].source_sha256.assign(64, 'z');
  Q4T_CHECK(SearchWebPages(pages, queries, {}, Access(), 10, &hits).code ==
                "web_search_index_invalid" &&
            hits.empty());
  pages[0].source_sha256 = Digest(pages[0].text);
  pages[0].text.assign(17U << 20, 'x');
  Q4T_CHECK(
      SearchWebPages(pages, queries, {}, Access(), 10, &hits).http_status ==
          413 &&
      hits.empty());
  Q4T_CHECK(SearchWebPages({}, {}, {}, Access(), 10, &hits).http_status == 413);
  return true;
}

Q4T_TEST(web_search_cancel_expiry_and_empty_results_remain_distinct) {
  const std::vector<WebPage> pages{
      Page("https://example.com/a", "Guide", "LLM")};
  const std::vector<std::string> queries{"absentterm"};
  std::vector<WebSearchHit> hits;
  Q4T_CHECK(Ok(SearchWebPages(pages, queries, {}, Access(), 10, &hits)) &&
            hits.empty());
  auto access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(SearchWebPages(pages, queries, {}, access, 10, &hits).http_status ==
            499);
  access = Access();
  access.deadline = Clock::now() - 1s;
  Q4T_CHECK(SearchWebPages(pages, queries, {}, access, 10, &hits).http_status ==
            408);
  access = Access();
  access.scope.clear();
  Q4T_CHECK(SearchWebPages(pages, queries, {}, access, 10, &hits).code ==
            "object_scope_unavailable");
  return true;
}
