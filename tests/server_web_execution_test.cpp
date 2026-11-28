#include "q4t/server/web_execution.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access(std::string scope = "client:web-execution") {
  return {std::move(scope), Clock::now() + 10s, {}};
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s: %s\n", result.code.c_str(),
                 result.message.c_str());
  return result.ok();
}
struct Directory {
  std::string path;
  ObjectStoreOptions objects;
  Directory() {
    const auto* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root =
        std::filesystem::absolute(env ? env : ".q4t-work/web-execution-cpu");
    std::filesystem::create_directories(root);
    path = (root / "web-execution-XXXXXX").string();
    if (!mkdtemp(path.data()))
      throw std::runtime_error("web fixture directory");
    objects.directory = path + "/objects";
    objects.restart_backoff_ms = 0;
  }
};
class Site {
 public:
  Site() {
    fd_ = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t size = sizeof(address);
    if (fd_ < 0 || bind(fd_, reinterpret_cast<sockaddr*>(&address), size) ||
        getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &size) ||
        listen(fd_, 4))
      throw std::runtime_error("web site fixture listener");
    port = ntohs(address.sin_port);
    thread_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        pollfd event{fd_, POLLIN, 0};
        if (poll(&event, 1, 10) <= 0) continue;
        const int client =
            accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (client < 0) continue;
        ScopeExit close_client([&] { close(client); });
        const auto until = Clock::now() + 2s;
        std::string request;
        while (!stop.stop_requested() && Clock::now() < until &&
               request.size() < 32768 &&
               request.find("\r\n\r\n") == request.npos) {
          char bytes[2048];
          auto n = recv(client, bytes, sizeof(bytes), MSG_DONTWAIT);
          if (n > 0)
            request.append(bytes, n);
          else if (!n)
            break;
          else
            std::this_thread::sleep_for(1ms);
        }
        std::string reply;
        {
          std::lock_guard lock(mutex_);
          const auto begin = request.find(' '),
                     end = request.find(' ', begin + 1);
          auto found =
              pages_.find(begin == request.npos
                              ? ""
                              : request.substr(begin + 1, end - begin - 1));
          reply = found == pages_.end()
                      ? "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
                      : found->second;
          ++requests;
        }
        std::string_view bytes(reply);
        while (!bytes.empty() && !stop.stop_requested() &&
               Clock::now() < until) {
          const auto n = send(client, bytes.data(), bytes.size(),
                              MSG_DONTWAIT | MSG_NOSIGNAL);
          if (n > 0)
            bytes.remove_prefix(n);
          else if (n < 0 && (errno == EINTR || errno == EAGAIN))
            std::this_thread::sleep_for(1ms);
          else
            break;
        }
      }
    });
  }
  ~Site() {
    thread_.request_stop();
    thread_.join();
    close(fd_);
  }
  std::string Url(std::string path = "/") const {
    return "http://127.0.0.1:" + std::to_string(port) + path;
  }
  void Wire(std::string path, std::string reply) {
    std::lock_guard lock(mutex_);
    pages_[std::move(path)] = std::move(reply);
  }
  void Page(std::string path, std::string body) {
    Wire(std::move(path),
         "HTTP/1.1 200 OK\r\nContent-Type: text/html; "
         "charset=utf-8\r\nContent-Length: " +
             std::to_string(body.size()) + "\r\n\r\n" + body);
  }
  void Discovery(std::string query, std::string body) {
    Wire("/api/search/?q=" + query,
         "HTTP/1.1 200 OK\r\nContent-Type: "
         "application/json\r\nContent-Length: " +
             std::to_string(body.size()) + "\r\n\r\n" + body);
  }
  Json Config() const {
    return Object(
        {{"version", Number(1)},
         {"seeds", Array({String(Url())})},
         {"scopes",
          Array({String("client:web-execution"), String("client:web-b")})},
         {"fetch",
          Object({{"public_networks", Boolean(false)},
                  {"allowed_networks", Array({String("127.0.0.1/32")})},
                  {"ports", Array({Number(port)})}})}});
  }
  WebDeploymentPolicy Policy() const {
    WebDeploymentPolicy policy;
    if (!ParseWebDeploymentPolicy(OutputJson(Config()), &policy))
      throw std::runtime_error("web fixture policy");
    return policy;
  }
  int port = 0;
  std::atomic<size_t> requests = 0;

 private:
  int fd_ = -1;
  std::mutex mutex_;
  std::map<std::string, std::string> pages_;
  std::jthread thread_;
};
Json ProviderConfig(const Site& provider, const Site& page) {
  auto config = page.Config();
  std::erase_if(config.object,
                [](const auto& pair) { return pair.first == "seeds"; });
  auto fetch = *provider.Config().Find("fetch");
  Set(&fetch, "allow_http", Boolean(true));
  Set(&config, "provider",
      Object({{"type", String("mwmbl_v2")},
              {"endpoint", String(provider.Url("/api/search/"))},
              {"fetch", fetch}}));
  return config;
}
WebDeploymentPolicy Configured(const Json& config) {
  WebDeploymentPolicy policy;
  if (!ParseWebDeploymentPolicy(OutputJson(config), &policy))
    throw std::runtime_error("invalid fixture source policy");
  return policy;
}
std::string Results(const std::string& query,
                    std::initializer_list<std::string> urls) {
  auto rows = Array();
  for (const auto& url : urls)
    rows.array.push_back(Object(
        {{"url", String(url)}, {"content", String("UNVERIFIED_SNIPPET")}}));
  return OutputJson(Object({{"query", String(query)}, {"results", rows}}));
}
WebAction Query(std::string text = "alpha") {
  WebAction action;
  action.queries.push_back(std::move(text));
  return action;
}
ObjectReply Run(ObjectStore& objects, const WebDeploymentPolicy& policy,
                WebExecutionBudget* budget, WebExecutionResult* output,
                WebSearchOptions options = {}, WebAction action = Query(),
                BinaryAccess access = Access()) {
  return ExecuteWebAction(objects, access, policy, options, action, {}, budget,
                          output);
}
}  // namespace

Q4T_TEST(web_policy_strict_fields_numbers_and_destination_contract) {
  Site site;
  const auto good = site.Config();
  WebDeploymentPolicy policy;
  Q4T_CHECK(ParseWebDeploymentPolicy(OutputJson(good), &policy));
  const auto identity = policy.sha256;
  for (const auto& [field, value] : std::vector<std::pair<std::string, Json>>{
           {"unknown", Boolean(true)},
           {"max_pages", Number(0)},
           {"max_pages", Number(33)},
           {"max_depth", Number(3)},
           {"max_depth", String("1")},
           {"refresh_seconds", Number(86401)},
           {"retention_seconds", Number(0)},
           {"scopes", Array({String("*")})},
           {"seeds", Array({String("file:///etc/passwd")})},
           {"seeds", Array({String(site.Url()), String(site.Url())})}}) {
    auto invalid = good;
    Set(&invalid, field, value);
    Q4T_CHECK(!ParseWebDeploymentPolicy(OutputJson(invalid), &policy));
    Q4T_CHECK(policy.sha256 == identity);
  }
  auto invalid = good;
  Set(&invalid, "fetch", Object({{"allow_http", Boolean(false)}}));
  Q4T_CHECK(!ParseWebDeploymentPolicy(OutputJson(invalid), &policy));
  Q4T_CHECK(site.requests == 0);
  return true;
}

Q4T_TEST(web_policy_identity_separates_acquisition_from_output_options) {
  Site site;
  auto policy = site.Policy();
  WebSearchOptions options;
  const auto identity = WebIndexIdentity(policy, options);
  options.external_web_access = false;
  options.context_size = "high";
  options.location = Object({{"city", String("Shanghai")}});
  Q4T_CHECK(WebIndexIdentity(policy, options) == identity);
  options.blocked_domains = {"example.com"};
  Q4T_CHECK(WebIndexIdentity(policy, options) != identity);
  options.blocked_domains.clear();
  policy.fetch.allowed_networks.clear();
  Q4T_CHECK(WebIndexIdentity(policy, options) != identity);
  policy = site.Policy();
  policy.max_depth = 0;
  Q4T_CHECK(WebIndexIdentity(policy, options) != identity);
  return true;
}

Q4T_TEST(web_policy_private_regular_file_loading) {
  Directory directory;
  Site site;
  const auto path = directory.path + "/web.json";
  {
    std::ofstream out(path);
    out << OutputJson(site.Config());
  }
  Q4T_CHECK(chmod(path.c_str(), 0600) == 0);
  WebDeploymentPolicy policy;
  Q4T_CHECK(LoadWebDeploymentPolicy(path, &policy));
  Q4T_CHECK(chmod(path.c_str(), 0644) == 0 &&
            !LoadWebDeploymentPolicy(path, &policy));
  Q4T_CHECK(chmod(path.c_str(), 0600) == 0);
  const auto alias = directory.path + "/alias";
  Q4T_CHECK(symlink(path.c_str(), alias.c_str()) == 0 &&
            !LoadWebDeploymentPolicy(alias, &policy));
  Q4T_CHECK(site.requests == 0);
  return true;
}

Q4T_TEST(web_execution_actual_crawl_source_hash_and_same_origin_boundary) {
  Directory directory;
  Site site, foreign;
  const auto body =
      "<title>Alpha</title>alpha root<a href='/child'>child</a><a "
      "href='/child#part'>same</a><a href='" +
      foreign.Url() + "'>outside</a>";
  site.Page("/", body);
  site.Page("/child", "alpha child");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result)));
  Q4T_CHECK(result.sources.size() == 2 && !result.cached &&
            site.requests == 2 && foreign.requests == 0);
  bool actual = false;
  for (const auto& source : result.sources)
    if (source.url == site.Url()) actual = source.source_sha256 == Digest(body);
  Q4T_CHECK(actual && budget.acquisition.used_files == 2 &&
            result.context.GetString("coverage") == "configured_sites");
  return true;
}

Q4T_TEST(web_execution_offline_survives_restart_without_http_or_ttl_extension) {
  Directory directory;
  Site site;
  site.Page("/", "alpha source");
  auto policy = site.Policy();
  WebExecutionBudget budget;
  WebExecutionResult result;
  std::string first;
  {
    ObjectStore objects(directory.objects);
    Q4T_CHECK(Ok(Run(objects, policy, &budget, &result)));
    first = result.sources[0].source_sha256;
  }
  ObjectStore objects(directory.objects);
  budget = {};
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result, offline)) &&
            result.cached && result.sources[0].source_sha256 == first &&
            budget.acquisition.used_files == 0 && site.requests == 1);
  return true;
}

Q4T_TEST(
    web_execution_scope_isolation_and_missing_offline_are_not_empty_success) {
  Directory directory;
  Site site;
  site.Page("/", "alpha private");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result)));
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Run(objects, site.Policy(), &budget, &result, offline, Query(),
                Access("client:web-b"))
                .code == "web_index_unavailable");
  Q4T_CHECK(result.sources.empty());
  Q4T_CHECK(Run(objects, site.Policy(), &budget, &result, {}, Query(),
                Access("client:unconfigured"))
                .http_status == 403);
  Q4T_CHECK(site.requests == 1);
  return true;
}

Q4T_TEST(web_execution_refresh_failure_keeps_old_complete_index) {
  Directory directory;
  Site site;
  site.Page("/", "alpha original");
  auto policy = site.Policy();
  policy.refresh_seconds = 0;
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result)));
  site.Page("/", "alpha replacement<a href='/missing'>follow</a>");
  Q4T_CHECK(!Run(objects, policy, &budget, &result).ok() &&
            result.sources.empty());
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result, offline)) &&
            result.sources.size() == 1 &&
            result.sources[0].text.find("original") != std::string::npos &&
            site.requests == 3);
  return true;
}

Q4T_TEST(web_execution_open_and_find_preserve_unicode_source_coordinates) {
  Directory directory;
  Site site;
  site.Page("/", "<p>前言 alpha 中文匹配 omega 中文匹配</p>");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  WebAction open;
  open.kind = WebAction::Kind::kOpen;
  open.url = site.Url();
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, {}, open)) &&
            site.requests == 1);
  auto sources = result.sources;
  WebAction find = open;
  find.kind = WebAction::Kind::kFind;
  find.pattern = "中文匹配";
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Ok(ExecuteWebAction(objects, Access(), site.Policy(), offline, find,
                                sources, &budget, &result)));
  const auto& item = result.context.GetArray("results")->array[0];
  const auto* matches = item.GetArray("matches");
  Q4T_CHECK(matches && matches->array.size() == 2 &&
            matches->array[0].GetInt("start_index") == 9 &&
            matches->array[0].GetInt("end_index") == 13 && site.requests == 1);
  find.pattern = "nonexistent";
  Q4T_CHECK(Ok(ExecuteWebAction(objects, Access(), site.Policy(), offline, find,
                                sources, &budget, &result)) &&
            result.context.GetArray("results")
                ->array[0]
                .GetArray("matches")
                ->array.empty());
  return true;
}

Q4T_TEST(web_execution_result_content_and_global_image_budget) {
  Directory directory;
  Site site;
  site.Page("/",
            "alpha root<img src='/a.png' alt='first'><img src='/b.png' "
            "alt='second'><a href='/child'>link</a>");
  site.Page("/child", "alpha child<img src='/c.png' alt='third'>");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  WebSearchOptions options;
  options.text = false;
  options.images = true;
  options.captions = true;
  options.max_images = 1;
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, options)));
  size_t images = 0;
  for (const auto& item : result.context.GetArray("results")->array) {
    Q4T_CHECK(!item.Has("text"));
    for (const auto& image : item.GetArray("images")->array) {
      ++images;
      Q4T_CHECK(!image.GetString("caption").empty());
    }
  }
  Q4T_CHECK(images == 1 && site.requests == 2);
  return true;
}

Q4T_TEST(web_execution_context_budget_truncates_only_at_utf8_boundaries) {
  Directory directory;
  Site site;
  std::string body = "alpha ";
  for (int i = 0; i < 6000; ++i) body += "中文";
  site.Page("/", body);
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  WebSearchOptions options;
  options.context_size = "low";
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, options)));
  auto item = result.context.GetArray("results")->array[0];
  Q4T_CHECK(item.GetBool("text_truncated") &&
            item.GetString("text").size() <= 8192 &&
            item.GetString("text").ends_with("中文"));
  options.unlimited_return_tokens = true;
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, options)));
  item = result.context.GetArray("results")->array[0];
  Q4T_CHECK(!item.GetBool("text_truncated") && item.GetString("text") == body &&
            site.requests == 1);
  return true;
}

Q4T_TEST(web_execution_cancel_and_cumulative_capacity_do_not_fetch) {
  Directory directory;
  Site site;
  site.Page("/", "alpha source");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  auto access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(Run(objects, site.Policy(), &budget, &result, {}, Query(), access)
                .http_status == 499);
  budget.actions = 16;
  Q4T_CHECK(Run(objects, site.Policy(), &budget, &result).http_status == 413);
  budget = {};
  budget.acquisition.max_bytes = 1;
  Q4T_CHECK(!Run(objects, site.Policy(), &budget, &result).ok() &&
            result.sources.empty());
  const auto attempted = site.requests.load();
  Q4T_CHECK(budget.acquisition.used_bytes == 1);
  Q4T_CHECK(!Run(objects, site.Policy(), &budget, &result).ok() &&
            site.requests == attempted);
  return true;
}

Q4T_TEST(web_execution_redirect_keeps_observed_url_and_offline_open) {
  Directory directory;
  Site site;
  site.Wire(
      "/",
      "HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 0\r\n\r\n");
  site.Page("/final",
            "<link rel='canonical' href='https://forged.invalid/'>alpha exact");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result)) &&
            result.sources[0].url == site.Url("/final"));
  WebSearchOptions offline;
  offline.external_web_access = false;
  WebAction action;
  action.kind = WebAction::Kind::kOpen;
  action.url = site.Url("/final");
  Q4T_CHECK(
      Ok(Run(objects, site.Policy(), &budget, &result, offline, action)) &&
      result.cached && site.requests == 2);
  action.url = site.Url("/never-fetched");
  Q4T_CHECK(
      Run(objects, site.Policy(), &budget, &result, offline, action).code ==
          "web_source_unavailable" &&
      site.requests == 2);
  return true;
}

Q4T_TEST(web_execution_domain_filters_prevent_crawl_and_open) {
  Directory directory;
  Site site;
  site.Page("/", "alpha denied");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  WebSearchOptions options;
  options.blocked_domains = {"127.0.0.1"};
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, options)) &&
            result.sources.empty() && site.requests == 0);
  WebAction open;
  open.kind = WebAction::Kind::kOpen;
  open.url = site.Url();
  Q4T_CHECK(Run(objects, site.Policy(), &budget, &result, options, open)
                    .http_status == 403 &&
            site.requests == 0);
  options.blocked_domains.clear();
  options.external_web_access = false;
  Q4T_CHECK(Run(objects, site.Policy(), &budget, &result, options).code ==
            "web_index_unavailable");
  return true;
}

Q4T_TEST(
    web_execution_location_refines_query_matches_without_inventing_sources) {
  Directory directory;
  Site site;
  site.Page(
      "/",
      "alpha Paris<a href='/shanghai'>link</a><a href='/unrelated'>link</a>");
  site.Page("/shanghai", "alpha Shanghai");
  site.Page("/unrelated", "unrelated Shanghai");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  WebSearchOptions options;
  options.location =
      Object({{"type", String("approximate")}, {"city", String("Shanghai")}});
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, options)) &&
            result.sources.size() == 2);
  Q4T_CHECK(result.sources[0].url == site.Url("/shanghai"));
  for (const auto& source : result.sources)
    Q4T_CHECK(source.url != site.Url("/unrelated"));
  Q4T_CHECK(result.context.Find("user_location")->GetString("city") ==
            "Shanghai");
  return true;
}

Q4T_TEST(
    web_execution_expired_index_can_be_replaced_without_reviving_old_data) {
  Directory directory;
  Site site;
  site.Page("/", "alpha old");
  auto policy = site.Policy();
  policy.refresh_seconds = 0;
  policy.retention_seconds = 5;
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result)));
  WebIndexSnapshot snapshot;
  Q4T_CHECK(Ok(WebIndexStore(objects).Read(
      Access(), WebIndexIdentity(policy, {}), &snapshot)));
  const auto until = Clock::now() + 6s;
  while (std::time(nullptr) < snapshot.expires_at && Clock::now() < until)
    std::this_thread::sleep_for(10ms);
  Q4T_CHECK(std::time(nullptr) >= snapshot.expires_at);
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Run(objects, policy, &budget, &result, offline).code ==
            "web_index_unavailable");
  site.Page("/", "alpha renewed");
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result)) &&
            result.sources[0].text.find("renewed") != std::string::npos &&
            site.requests == 2);
  return true;
}

Q4T_TEST(web_execution_lost_publication_receipt_does_not_repeat_acquisition) {
  Directory directory;
  Site site;
  site.Page("/", "alpha committed");
  ObjectWorkerEntryForTesting("--q4t-object-worker-lost-ack");
  ScopeExit restore([] { ObjectWorkerEntryForTesting({}); });
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  const auto reply = Run(objects, site.Policy(), &budget, &result);
  Q4T_CHECK(reply.commit_unknown && site.requests == 1 &&
            result.sources.empty());
  const auto until = Clock::now() + 3s;
  while (objects.Health().pending && Clock::now() < until)
    std::this_thread::sleep_for(1ms);
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Ok(Run(objects, site.Policy(), &budget, &result, offline)) &&
            result.cached &&
            result.sources[0].text.find("committed") != std::string::npos &&
            site.requests == 1);
  return true;
}

Q4T_TEST(web_execution_configured_source_preflight) {
  Directory directory;
  Site local;
  const auto* policy_path = std::getenv("Q4T_WEB_PREFLIGHT_POLICY");
  const auto* source_path = std::getenv("Q4T_WEB_PREFLIGHT_SOURCE");
  Q4T_CHECK(bool(policy_path) == bool(source_path));
  std::string bytes =
      "<title>Example Domain</title><p>documentation examples</p>";
  WebDeploymentPolicy policy;
  if (policy_path) {
    Q4T_CHECK(LoadWebDeploymentPolicy(policy_path, &policy));
    std::ifstream input(source_path, std::ios::binary | std::ios::ate);
    Q4T_CHECK(input && input.tellg() > 0 && input.tellg() <= (1 << 20));
    bytes.resize(input.tellg());
    input.seekg(0);
    Q4T_CHECK(bool(input.read(bytes.data(), bytes.size())));
  } else {
    local.Page("/", bytes);
    policy = local.Policy();
    policy.max_depth = 0;
  }
  Q4T_CHECK(policy.seeds.size() == 1 && !policy.scopes.empty());
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult result;
  auto access = Access(policy.scopes.front());
  access.deadline = Clock::now() + 30s;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result, {},
                   Query("documentation examples"), access)));
  Q4T_CHECK(result.sources.size() == 1 && !result.cached &&
            result.sources[0].url == policy.seeds[0] &&
            result.sources[0].source_sha256 == Digest(bytes) &&
            result.sources[0].title == "Example Domain" &&
            result.sources[0].text.find("documentation examples") !=
                std::string::npos);
  const auto acquired = budget.acquisition.used_files;
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result, offline,
                   Query("documentation examples"), access)));
  Q4T_CHECK(result.cached && budget.acquisition.used_files == acquired &&
            result.sources.size() == 1 &&
            result.sources[0].source_sha256 == Digest(bytes));
  return true;
}

Q4T_TEST(web_provider_private_selection_and_redaction) {
  Site provider, page;
  const auto config = ProviderConfig(provider, page);
  const auto policy = Configured(config);
  Q4T_CHECK(policy.provider && !policy.provider->fetch.max_redirects &&
            policy.seeds.empty() && !policy.max_depth);
  const auto description = WebDeploymentPolicyDescription(&policy);
  Q4T_CHECK(description.GetString("backend") == "mwmbl_v2" &&
            OutputJson(description).find("127.0.0.1") == std::string::npos);
  for (const auto& [key, value] : std::vector<std::pair<std::string, Json>>{
           {"endpoint", String(provider.Url("/?q=private"))},
           {"endpoint", String(provider.Url("/#fragment"))},
           {"endpoint", String("http://user@private.example/")},
           {"type", String("unknown")},
           {"headers", Object()}}) {
    auto invalid = config;
    Set(&Field(invalid, "provider"), key, value);
    WebDeploymentPolicy out;
    Q4T_CHECK(!ParseWebDeploymentPolicy(OutputJson(invalid), &out));
  }
  for (auto item : {"seeds", "max_depth", "max_redirects", "allow_http"}) {
    auto invalid = config;
    auto& fetch = Field(Field(invalid, "provider"), "fetch");
    if (std::string_view(item) == "seeds")
      Set(&invalid, item, Array({String(page.Url())}));
    else if (std::string_view(item) == "max_depth")
      Set(&invalid, item, Number(1));
    else
      Set(&fetch, item,
          std::string_view(item) == "allow_http" ? Boolean(false) : Number(1));
    WebDeploymentPolicy out;
    Q4T_CHECK(!ParseWebDeploymentPolicy(OutputJson(invalid), &out));
  }
  Q4T_CHECK(provider.requests == 0 && page.requests == 0);
  return true;
}

Q4T_TEST(web_provider_verified_sources_query_cache_and_offline_url) {
  Directory directory;
  Site provider, page;
  const auto policy = Configured(ProviderConfig(provider, page));
  const std::string query = "中文 &q=red#";
  const std::string body = "<title>Original</title>独立正文";
  page.Page("/source", body);
  provider.Discovery(
      "%E4%B8%AD%E6%96%87%20%26q%3Dred%23",
      Results(query, {page.Url("/source"), page.Url("/source#same")}));
  WebExecutionBudget budget;
  WebExecutionResult output;
  {
    ObjectStore objects(directory.objects);
    Q4T_CHECK(Ok(Run(objects, policy, &budget, &output, {}, Query(query))));
    Q4T_CHECK(output.sources.size() == 1 && !output.cached &&
              output.sources.front().source_sha256 == Digest(body) &&
              output.sources.front().title == "Original" &&
              output.sources.front().text.find("独立正文") !=
                  std::string::npos &&
              OutputJson(output.context).find("UNVERIFIED_SNIPPET") ==
                  std::string::npos);
  }
  ObjectStore restarted(directory.objects);
  WebSearchOptions offline;
  offline.external_web_access = false;
  const auto calls = budget.acquisition.used_files;
  Q4T_CHECK(
      Ok(Run(restarted, policy, &budget, &output, offline, Query(query))) &&
      output.cached && output.sources[0].source_sha256 == Digest(body));
  Q4T_CHECK(
      Run(restarted, policy, &budget, &output, offline, Query("other")).code ==
      "web_index_unavailable");
  WebAction open{WebAction::Kind::kFind, {}, page.Url("/source"), "独立"};
  Q4T_CHECK(Ok(Run(restarted, policy, &budget, &output, offline, open)) &&
            output.cached && output.sources[0].source_sha256 == Digest(body));
  Q4T_CHECK(Run(restarted, policy, &budget, &output, offline, open,
                Access("client:web-b"))
                .code == "web_source_unavailable");
  Q4T_CHECK(provider.requests == 1 && page.requests == 1 && calls == 2 &&
            budget.acquisition.used_files == calls);
  return true;
}

Q4T_TEST(
    web_provider_multiple_queries_share_limit_and_respect_destination_policy) {
  Directory directory;
  Site provider, page, outside;
  auto config = ProviderConfig(provider, page);
  Set(&config, "max_pages", Number(2));
  auto policy = Configured(config);
  provider.Discovery("first", Results("first", {outside.Url(), page.Url("/a"),
                                                page.Url("/b")}));
  provider.Discovery("second", Results("second", {page.Url("/c")}));
  page.Page("/a", "A");
  page.Page("/b", "B");
  page.Page("/c", "C");
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult output;
  WebAction search{WebAction::Kind::kSearch, {"first", "second"}, {}, {}};
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &output, {}, search)) &&
            output.sources.size() == 2 &&
            output.sources[0].url == page.Url("/a") &&
            output.sources[1].url == page.Url("/c"));
  WebSearchOptions filters;
  filters.allowed_domains = {"example.org"};
  Q4T_CHECK(
      Ok(Run(objects, policy, &budget, &output, filters, Query("first"))) &&
      output.sources.empty());
  filters.empty_domain_allowlist = true;
  Q4T_CHECK(
      Ok(Run(objects, policy, &budget, &output, filters, Query("unused"))) &&
      output.sources.empty());
  Q4T_CHECK(outside.requests == 0 && provider.requests == 3 &&
            page.requests == 2);
  return true;
}

Q4T_TEST(web_provider_failure_and_partial_search_keep_complete_cache) {
  Directory directory;
  Site provider, page;
  auto policy = Configured(ProviderConfig(provider, page));
  policy.refresh_seconds = 0;
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult output;
  page.Page("/kept", "Original");
  provider.Discovery("alpha", Results("alpha", {page.Url("/kept")}));
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &output)));
  page.Page("/fresh", "New");
  page.Wire(
      "/dead",
      "HTTP/1.1 301 Moved Permanently\r\n"
      "Location: ftp://not-a-web-source.invalid/\r\nContent-Length: 0\r\n\r\n");
  provider.Discovery("alpha",
                     Results("alpha", {page.Url("/dead"), page.Url("/fresh")}));
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &output)) &&
            output.sources.size() == 1 &&
            output.sources[0].url == page.Url("/fresh") &&
            output.context.GetInt("unavailable_sources") == 1);
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &output, offline)) &&
            output.cached && output.sources[0].url == page.Url("/kept"));
  provider.Discovery("alpha", Results("alpha", {page.Url("/dead")}));
  Q4T_CHECK(!Run(objects, policy, &budget, &output).ok() &&
            output.sources.empty());
  provider.Discovery("alpha", Results("different", {}));
  Q4T_CHECK(Run(objects, policy, &budget, &output).code ==
            "web_search_provider_invalid");
  provider.Discovery("alpha", R"({"query":"alpha","results":{},"results":[]})");
  Q4T_CHECK(Run(objects, policy, &budget, &output).code ==
            "web_search_provider_invalid");
  provider.Discovery("alpha", Results("alpha", {}));
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &output)) &&
            output.sources.empty());
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &output, offline)) &&
            output.cached && output.sources.empty());
  return true;
}

Q4T_TEST(web_provider_egress_budget_redirect_and_cancellation) {
  Directory directory;
  Site provider, page, other;
  auto policy = Configured(ProviderConfig(provider, page));
  WebExecutionBudget budget;
  WebExecutionResult output;
  ObjectStore objects(directory.objects);
  provider.Wire("/api/search/?q=alpha",
                "HTTP/1.1 302 Found\r\nLocation: " + other.Url("/again") +
                    "\r\nContent-Length: 0\r\n\r\n");
  Q4T_CHECK(Run(objects, policy, &budget, &output).code ==
                "file_fetch_redirect_limit" &&
            other.requests == 0 && budget.acquisition.used_files == 1 &&
            budget.acquisition.used_bytes > 0);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  const auto attempts = provider.requests.load();
  Q4T_CHECK(Run(objects, policy, &budget, &output, {}, Query(), cancelled)
                    .http_status == 499 &&
            provider.requests == attempts);
  budget = {};
  budget.acquisition.max_files = 1;
  provider.Discovery("alpha", Results("alpha", {page.Url()}));
  Q4T_CHECK(Run(objects, policy, &budget, &output).http_status == 413 &&
            page.requests == 0 && budget.acquisition.used_files == 1);
  provider.Discovery("long", std::string(4096, 'x'));
  budget = {};
  budget.acquisition.max_bytes = 128;
  Q4T_CHECK(!Run(objects, policy, &budget, &output, {}, Query("long")).ok() &&
            budget.acquisition.used_bytes == 128);
  return true;
}

Q4T_TEST(web_provider_unavailable_is_distinct_from_an_empty_result) {
  Directory directory;
  Site page;
  WebDeploymentPolicy policy;
  ObjectStore objects(directory.objects);
  WebExecutionBudget budget;
  WebExecutionResult output;
  {
    Site provider;
    policy = Configured(ProviderConfig(provider, page));
    provider.Wire("/api/search/?q=alpha",
                  "HTTP/1.1 503 Service Unavailable\r\n"
                  "Content-Length: 0\r\n\r\n");
    auto failed = Run(objects, policy, &budget, &output);
    Q4T_CHECK(failed.http_status == 503 &&
              failed.code == "web_search_provider_unavailable");
  }
  auto failed = Run(objects, policy, &budget, &output);
  Q4T_CHECK(failed.http_status == 503 &&
            failed.code == "web_search_provider_unavailable" &&
            budget.acquisition.used_files == 2 && output.sources.empty() &&
            page.requests == 0);
  WebSearchOptions offline;
  offline.external_web_access = false;
  Q4T_CHECK(Run(objects, policy, &budget, &output, offline).code ==
            "web_index_unavailable");
  return true;
}

Q4T_TEST(web_provider_source_preflight) {
  Directory directory;
  Site local_index, local_page;
  const auto* policy_path = std::getenv("Q4T_WEB_PROVIDER_PREFLIGHT_POLICY");
  const auto* source_path = std::getenv("Q4T_WEB_PROVIDER_PREFLIGHT_SOURCE");
  const auto* source_url = std::getenv("Q4T_WEB_PROVIDER_PREFLIGHT_URL");
  const std::string query = "RFC 2606 reserved example domains";
  Q4T_CHECK(bool(policy_path) == bool(source_path) &&
            bool(policy_path) == bool(source_url));
  std::string body = "<title>Example Domains</title><p>documentation</p>";
  std::string expected = local_page.Url();
  WebDeploymentPolicy policy;
  if (policy_path) {
    Q4T_CHECK(LoadWebDeploymentPolicy(policy_path, &policy));
    std::ifstream file(source_path, std::ios::binary | std::ios::ate);
    Q4T_CHECK(file && file.tellg() > 0 && file.tellg() <= (2 << 20));
    body.resize(file.tellg());
    file.seekg(0);
    Q4T_CHECK(bool(file.read(body.data(), body.size())));
    expected = source_url;
  } else {
    auto config = ProviderConfig(local_index, local_page);
    Set(&config, "max_pages", Number(1));
    policy = Configured(config);
    local_index.Discovery("RFC%202606%20reserved%20example%20domains",
                          Results(query, {expected}));
    local_page.Page("/", body);
  }
  Q4T_CHECK(policy.provider && policy.max_pages == 1 && !policy.scopes.empty());
  WebExecutionBudget budget;
  WebExecutionResult output;
  auto access = Access(policy.scopes.front());
  access.deadline = Clock::now() + 45s;
  WebSearchOptions options;
  {
    ObjectStore objects(directory.objects);
    Q4T_CHECK(Ok(
        Run(objects, policy, &budget, &output, options, Query(query), access)));
    Q4T_CHECK(!output.cached && output.sources.size() == 1 &&
              output.sources[0].url == expected &&
              output.sources[0].source_sha256 == Digest(body) &&
              output.sources[0].text.find("documentation") !=
                  std::string::npos &&
              budget.acquisition.used_files == 2);
  }
  const auto bytes = budget.acquisition.used_bytes;
  ObjectStore reopened(directory.objects);
  options.external_web_access = false;
  Q4T_CHECK(Ok(Run(reopened, policy, &budget, &output, options, Query(query),
                   access)) &&
            output.cached && output.sources.size() == 1 &&
            output.sources[0].source_sha256 == Digest(body));
  WebAction open{WebAction::Kind::kOpen, {}, expected, {}};
  Q4T_CHECK(
      Ok(Run(reopened, policy, &budget, &output, options, open, access)) &&
      output.cached && output.sources[0].url == expected &&
      budget.acquisition.used_bytes == bytes &&
      budget.acquisition.used_files == 2);
  if (policy_path)
    std::printf(
        "verified public source SHA256=%s content_bytes=%zu "
        "total_received=%llu\n",
        output.sources[0].source_sha256.c_str(), body.size(),
        static_cast<unsigned long long>(bytes));
  return true;
}

Q4T_TEST(web_search_retains_relevant_late_passage_with_correct_coordinates) {
  Directory directory;
  Site provider, page;
  const auto policy = Configured(ProviderConfig(provider, page));
  const std::string query = "RFC 2606 reserved example domains";
  std::string body = "<title>RFC 2606</title><p>example.com ";
  for (size_t i = 0; i < 3300; ++i) body += "前 ";
  body += "reserved example domains: example.com example.net example.org</p>";
  if (const auto* source = std::getenv("Q4T_WEB_PASSAGE_REFERENCE")) {
    std::ifstream file(source, std::ios::ate | std::ios::binary);
    Q4T_CHECK(file && file.tellg() > 0 && file.tellg() <= (2 << 20));
    body.resize(file.tellg());
    file.seekg(0);
    Q4T_CHECK(bool(file.read(body.data(), body.size())));
  }
  provider.Discovery("RFC%202606%20reserved%20example%20domains",
                     Results(query, {page.Url()}));
  page.Page("/", body);
  ObjectStore objects(directory.objects);
  WebSearchOptions options;
  options.context_size = "low";
  WebExecutionBudget budget;
  WebExecutionResult result;
  Q4T_CHECK(Ok(Run(objects, policy, &budget, &result, options, Query(query))));
  const auto& item = result.context.GetArray("results")->array[0];
  const auto* passages = item.GetArray("additional_passages");
  Q4T_CHECK(passages && passages->array.size() == 1 &&
            item.GetBool("text_truncated"));
  const auto first = item.GetString("text");
  const auto& extra = passages->array[0];
  const auto second = extra.GetString("text");
  const auto& full = result.sources[0].text;
  const auto at = full.find(second);
  Q4T_CHECK(!second.empty() && at != std::string::npos && at >= first.size() &&
            full.starts_with(first) && first.size() + second.size() <= 8192 &&
            extra.GetInt("start_index") ==
                static_cast<int64_t>(
                    Utf8Characters(std::string_view(full).substr(0, at))) &&
            second.find("example.net") != std::string::npos &&
            second.find("example.org") != std::string::npos);
  return true;
}
