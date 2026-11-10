#include "q4t/server/file_fetch.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstring>
#include <functional>
#include <mutex>
#include <thread>

#include "../src/server/file_fetch_internal.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using detail::GrammarProcess;
using q4t::service::ingress::Address;
class Server {
 public:
  explicit Server(std::vector<std::string> responses)
      : responses_(std::move(responses)) {
    fd_ = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t size = sizeof(address);
    if (fd_ < 0 || bind(fd_, reinterpret_cast<sockaddr*>(&address), size) ||
        getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &size) ||
        listen(fd_, 4))
      throw std::runtime_error("file fetch test listener");
    port = ntohs(address.sin_port);
    thread_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        pollfd event{fd_, POLLIN, 0};
        if (poll(&event, 1, 10) <= 0) continue;
        const int client =
            accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (client < 0) continue;
        ScopeExit close_client([&] { close(client); });
        std::string request;
        const auto deadline = Clock::now() + 2s;
        while (!stop.stop_requested() && Clock::now() < deadline &&
               request.find("\r\n\r\n") == request.npos &&
               request.size() < 32768) {
          char bytes[1024];
          const auto n = recv(client, bytes, sizeof(bytes), MSG_DONTWAIT);
          if (n > 0)
            request.append(bytes, n);
          else if (!n)
            break;
          else
            std::this_thread::sleep_for(1ms);
        }
        {
          std::lock_guard lock(mutex_);
          requests_.push_back(request);
        }
        const size_t index = requests.fetch_add(1);
        const auto wire = index < responses_.size() ? responses_[index] : "";
        if (wire == "stall") {
          while (!stop.stop_requested() && Clock::now() < deadline)
            std::this_thread::sleep_for(1ms);
        } else {
          std::string_view remaining(wire);
          while (!remaining.empty() && !stop.stop_requested() &&
                 Clock::now() < deadline) {
            const auto n = send(client, remaining.data(), remaining.size(),
                                MSG_DONTWAIT | MSG_NOSIGNAL);
            if (n > 0)
              remaining.remove_prefix(n);
            else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK ||
                               errno == EINTR))
              std::this_thread::sleep_for(1ms);
            else
              break;
          }
        }
      }
    });
  }
  ~Server() {
    thread_.request_stop();
    thread_.join();
    close(fd_);
  }
  std::string Url(std::string_view path = "/input.txt") const {
    return "http://127.0.0.1:" + std::to_string(port) + std::string(path);
  }
  FileFetchOptions Options() const {
    FileFetchOptions options;
    options.public_networks = false;
    options.allowed_networks = {"127.0.0.1/32"};
    options.ports = {port};
    return options;
  }
  std::vector<std::string> Requests() {
    std::lock_guard lock(mutex_);
    return requests_;
  }
  std::atomic<size_t> requests{0};
  int port = 0;

 private:
  int fd_ = -1;
  std::vector<std::string> responses_, requests_;
  std::mutex mutex_;
  std::jthread thread_;
};
BinaryAccess Access() { return {"client:file-fetch", Clock::now() + 3s, {}}; }
ObjectReply Fetch(Server& server, FileInputBudget* budget,
                  FileFetchReceipt* receipt, BinaryAccess access = Access(),
                  GrammarControl control = {}) {
  const auto url = server.Url();
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = url;
  source.param = "input[0].content[0].file_url";
  return FetchFileInput(access, source, server.Options(), budget, receipt,
                        std::move(control));
}
std::string Response(std::string_view bytes) {
  return "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
         "Content-Length: " +
         std::to_string(bytes.size()) + "\r\n\r\n" + std::string(bytes);
}
}  // namespace

// A real independent fetch entry for the local Python TLS server fixture.
int FileFetchFixture(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--file-fetch-fixture")) return -1;
  if (argc != 4) return 2;
  Json policy;
  FileFetchOptions options;
  if (!Parse(argv[3], &policy) || !ParseFileFetchOptions(policy, &options).ok())
    return 2;
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = argv[2];
  source.param = "file_url";
  FileInputBudget budget;
  FileFetchReceipt receipt;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  auto access = Access();
  access.deadline = Clock::now() + 10s;
  const auto result =
      FetchFileInput(access, source, options, &budget, &receipt, control);
  auto output = Object({{"status", Number(result.http_status)},
                        {"code", String(result.code)},
                        {"bytes", Number(receipt.content.bytes.size())},
                        {"sha256", String(receipt.content.sha256)},
                        {"mime_type", String(receipt.content.mime_type)},
                        {"hops", Number(receipt.urls.size())},
                        {"started", Number(control.counters->started.load())},
                        {"reaped", Number(control.counters->reaped.load())},
                        {"active", Number(control.counters->active.load())}});
  std::puts(OutputJson(output).c_str());
  return 0;
}

Q4T_TEST(file_fetch_url_and_relative_redirect_contracts) {
  file_detail::Url parsed, next;
  Q4T_CHECK(file_detail::ParseUrl("https://Example.COM:443/a/b.pdf?q=1#part",
                                  &parsed));
  Q4T_CHECK(parsed.tls && parsed.port == 443 && parsed.host == "example.com" &&
            parsed.target == "/a/b.pdf?q=1");
  Q4T_CHECK(file_detail::Redirect(parsed, "../c.txt?x=2", &next) &&
            next.Text() == "https://example.com:443/c.txt?x=2");
  Q4T_CHECK(file_detail::Redirect(parsed, "?new=1", &next) &&
            next.target == "/a/b.pdf?new=1");
  Q4T_CHECK(!file_detail::Redirect(parsed, "http://example.com/a", &next));
  Q4T_CHECK(file_detail::ParseUrl("http://[::1]:8080?x=1", &next) &&
            next.host == "::1" && next.port == 8080 && next.target == "/?x=1");
  for (const auto* value :
       {"file:///etc/passwd", "http://u:p@localhost/a",
        "http://127.0.0.1%00.example.com/", "http://a:0/", "http://a:65536/",
        "http://a:/", "http://a\r\n/b", "http://[fe80::1%lo]/", "http://a\\b/"})
    Q4T_CHECK(!file_detail::ParseUrl(value, &next));
  return true;
}
Q4T_TEST(file_fetch_address_policy_blocks_private_and_mapped_bypasses) {
  FileFetchOptions options;
  for (const auto* address :
       {"127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.1.1",
        "169.254.169.254", "100.64.0.1", "224.0.0.1", "198.18.0.1", "::1",
        "::", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "2002:7f00:1::1",
        "64:ff9b::7f00:1", "2001:db8::1", "3fff::1"})
    Q4T_CHECK(!file_detail::AllowedAddress(Address::Parse(address), options));
  Q4T_CHECK(file_detail::AllowedAddress(Address::Parse("8.8.8.8"), options));
  Q4T_CHECK(
      file_detail::AllowedAddress(Address::Parse("2606:4700::1111"), options));
  options.public_networks = false;
  options.allowed_networks = {"127.0.0.1/32"};
  Q4T_CHECK(
      file_detail::AllowedAddress(Address::Parse("::ffff:127.0.0.1"), options));
  Q4T_CHECK(!file_detail::AllowedAddress(Address::Parse("127.0.0.2"), options));
  return true;
}
Q4T_TEST(file_fetch_deployment_policy_is_strict_and_roundtrips) {
  FileFetchOptions options;
  auto wire = Object({{"allowed_hosts", Array({String("example.com")})},
                      {"ports", Array({Number(443)})},
                      {"allow_http", Boolean(false)}});
  Q4T_CHECK(ParseFileFetchOptions(wire, &options).ok());
  file_detail::Url url;
  Q4T_CHECK(file_detail::ParseUrl("https://example.com./a", &url) &&
            file_detail::AllowedUrl(url, options));
  Q4T_CHECK(file_detail::ParseUrl("https://example.net/a", &url) &&
            !file_detail::AllowedUrl(url, options));
  Q4T_CHECK(
      ParseFileFetchOptions(file_detail::OptionsJson(options), &options).ok());
  for (const auto& invalid :
       {Object({{"public_networks", String("true")}}),
        Object({{"proxy", String("http://localhost")}}),
        Object({{"ports", Array({Number(80), Number(80)})}}),
        Object({{"timeout_ms", Number(30001)}}),
        Object({{"max_redirects", Number(11)}}),
        Object({{"ca_file", String("relative-ca")}}),
        Object({{"allowed_networks", Array({String("127.0.0.1/24")})}})})
    Q4T_CHECK(ParseFileFetchOptions(invalid, &options).http_status == 400);
  return true;
}
Q4T_TEST(file_fetch_http_content_length_chunked_and_close_exact_bytes) {
  const std::string body(
      "a\0\xff"
      "b",
      4);
  for (const auto& wire :
       {Response(body),
        "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        "4;tag=ok\r\n" +
            body + "\r\n0\r\nX-Note: end\r\n\r\n",
        "HTTP/1.0 200 OK\r\n\r\n" + body}) {
    Server server({wire});
    FileInputBudget budget{7, 3, 2, 1};
    FileFetchReceipt receipt;
    Q4T_CHECK(Fetch(server, &budget, &receipt).ok());
    Q4T_CHECK(receipt.content.bytes == body &&
              receipt.content.sha256 == Digest(body) &&
              budget.used_bytes == 7 && budget.used_files == 2 &&
              receipt.urls.size() == 1);
  }
  return true;
}
Q4T_TEST(file_fetch_http_rejects_ambiguous_truncated_or_partial_file) {
  for (const auto* wire :
       {"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: "
        "chunked\r\n\r\nx",
        "HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
        "HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx",
        "HTTP/1.1 200 OK\r\nTransfer-Encoding: "
        "chunked\r\n\r\n1\r\nx\r\n0\r\nContent-Type: bad\r\n\r\n",
        "HTTP/1.1 200 OK\r\nContent-Range: bytes 0-0/8\r\nContent-Length: "
        "1\r\n\r\nx",
        "HTTP/1.1 200 OK\r\nConnection: Content-Length\r\nContent-Length: "
        "1\r\n\r\nx",
        "HTTP/1.1 101 Switch\r\n\r\n",
        "HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
        "1\r\n\r\nx"}) {
    Server server({wire});
    FileInputBudget budget;
    FileFetchReceipt receipt;
    const auto result = Fetch(server, &budget, &receipt);
    Q4T_CHECK(result.http_status == 502 && receipt.content.bytes.empty() &&
              receipt.content.sha256.empty() && budget.used_files == 1);
  }
  Server server(
      {"HTTP/1.1 103 Early\r\nLink: </style>\r\n\r\n" + Response("ok")});
  FileInputBudget budget;
  FileFetchReceipt receipt;
  Q4T_CHECK(Fetch(server, &budget, &receipt).ok() &&
            receipt.content.bytes == "ok");
  return true;
}
Q4T_TEST(file_fetch_redirects_revalidate_target_and_never_forward_credentials) {
  Server server(
      {"HTTP/1.1 302 Move\r\nLocation: ../final.txt\r\nSet-Cookie: "
       "secret=1\r\nContent-Length: 0\r\n\r\n",
       Response("original file")});
  FileInputBudget budget;
  FileFetchReceipt receipt;
  Q4T_CHECK(Fetch(server, &budget, &receipt).ok() && receipt.urls.size() == 2 &&
            receipt.content.filename == "final.txt" &&
            receipt.content.bytes == "original file");
  const auto requests = server.Requests();
  Q4T_CHECK(requests.size() == 2 &&
            requests[1].starts_with("GET /final.txt HTTP/1.1\r\n") &&
            requests[1].find("Cookie:") == std::string::npos &&
            requests[1].find("Authorization:") == std::string::npos);
  Server denied(
      {"HTTP/1.1 302 Move\r\nLocation: http://127.0.0.2/input\r\n\r\n"});
  auto options = denied.Options();
  options.ports.push_back(80);
  const auto url = denied.Url();
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = url;
  budget = {};
  Q4T_CHECK(FetchFileInput(Access(), source, options, &budget, &receipt)
                    .http_status == 403 &&
            receipt.content.bytes.empty() && denied.requests == 1);
  return true;
}
Q4T_TEST(file_fetch_size_errors_empty_files_and_failed_remote_status) {
  for (const auto& wire : {Response("abcd"),
                           std::string("HTTP/1.1 200 OK\r\nTransfer-Encoding: "
                                       "chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n"),
                           std::string("HTTP/1.0 200 OK\r\n\r\nabcd")}) {
    Server server({wire});
    FileInputBudget budget{3};
    FileFetchReceipt receipt;
    Q4T_CHECK(Fetch(server, &budget, &receipt).http_status == 413 &&
              receipt.content.bytes.empty() && budget.used_bytes == 3);
  }
  Server empty({Response("")});
  FileInputBudget budget{0};
  FileFetchReceipt receipt;
  Q4T_CHECK(Fetch(empty, &budget, &receipt).ok() &&
            receipt.content.bytes.empty() &&
            receipt.content.sha256 == Digest("") && budget.used_bytes == 0);
  Server missing({"HTTP/1.1 404 Missing\r\nContent-Length: 0\r\n\r\n"});
  budget = {};
  auto failed = Fetch(missing, &budget, &receipt);
  Q4T_CHECK(failed.http_status == 400 &&
            failed.value.GetInt("upstream_status") == 404 &&
            receipt.content.sha256.empty());
  return true;
}
Q4T_TEST(file_fetch_redirect_loops_and_limits_do_not_publish_content) {
  Server loop({"HTTP/1.1 302 Move\r\nLocation: /input.txt\r\n\r\n"});
  FileInputBudget budget;
  FileFetchReceipt receipt;
  auto reply = Fetch(loop, &budget, &receipt);
  Q4T_CHECK(reply.code == "file_fetch_redirect_loop" && loop.requests == 1 &&
            receipt.content.bytes.empty() && reply.value.IsObject());
  Server limited({"HTTP/1.1 302 Move\r\nLocation: /next\r\n\r\n"});
  auto options = limited.Options();
  options.max_redirects = 0;
  const auto url = limited.Url();
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = url;
  budget = {};
  reply = FetchFileInput(Access(), source, options, &budget, &receipt);
  Q4T_CHECK(reply.code == "file_fetch_redirect_limit" &&
            limited.requests == 1 && receipt.content.sha256.empty());
  return true;
}
Q4T_TEST(file_fetch_deadline_reaps_stalled_network_without_partial_output) {
  Server server({"stall"});
  FileInputBudget budget;
  FileFetchReceipt receipt;
  auto access = Access();
  access.deadline = Clock::now() + 150ms;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  const auto start = Clock::now();
  const auto result = Fetch(server, &budget, &receipt, access, control);
  Q4T_CHECK(result.http_status == 408 && Clock::now() - start < 1s &&
            receipt.content.bytes.empty() && server.requests == 1 &&
            control.counters->started == 1 && control.counters->reaped == 1 &&
            control.counters->active == 0);
  return true;
}
Q4T_TEST(file_fetch_cancellation_interrupts_actual_network_wait) {
  Server server({"stall"});
  FileInputBudget budget;
  FileFetchReceipt receipt;
  auto access = Access();
  access.cancelled = [&] { return server.requests.load() > 0; };
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  const auto result = Fetch(server, &budget, &receipt, access, control);
  Q4T_CHECK(result.http_status == 499 && receipt.content.bytes.empty() &&
            control.counters->started == 1 && control.counters->reaped == 1 &&
            control.counters->active == 0 &&
            budget.used_bytes == budget.max_bytes);
  return true;
}
Q4T_TEST(
    file_fetch_unreaped_domain_keeps_its_slot_without_blocking_other_domains) {
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kFileFetch;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  std::unique_ptr<GrammarProcess> first, denied, grammar, objects;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-silent", control, &first));
  detail::DeferGrammarReapingForTesting(true);
  ScopeExit restore([&] {
    detail::DeferGrammarReapingForTesting(false);
    const auto until = Clock::now() + 300ms;
    while (detail::IsolatedCleanupPending(control.domain) &&
           Clock::now() < until)
      std::this_thread::sleep_for(1ms);
  });
  first.reset();
  Q4T_CHECK(detail::IsolatedCleanupPending(control.domain));
  GrammarFailure failure;
  Q4T_CHECK(!GrammarProcess::Start("--grammar-fixture-silent", control, &denied,
                                   &failure) &&
            failure == GrammarFailure::kUnavailable);
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-silent", {}, &grammar));
  GrammarControl object_control;
  object_control.domain = IsolatedWorkerDomain::kObjects;
  Q4T_CHECK(GrammarProcess::Start("--grammar-fixture-silent", object_control,
                                  &objects));
  detail::DeferGrammarReapingForTesting(false);
  grammar.reset();
  objects.reset();
  const auto until = Clock::now() + 300ms;
  while (control.counters->reaped == 0 && Clock::now() < until) {
    detail::PollRequestWorkers();
    std::this_thread::sleep_for(1ms);
  }
  Q4T_CHECK(control.counters->reaped == 1 && control.counters->active == 0 &&
            !detail::IsolatedCleanupPending(control.domain));
  return true;
}
Q4T_TEST(file_fetch_unowned_invalid_and_denied_inputs_start_no_process) {
  FileInputSource source;
  source.kind = FileInputSource::Kind::kUrl;
  source.value = "https://example.com/a";
  FileFetchOptions options;
  FileInputBudget budget;
  FileFetchReceipt receipt;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  auto access = Access();
  access.scope.clear();
  Q4T_CHECK(FetchFileInput(access, source, options, &budget, &receipt, control)
                .http_status == 503);
  options.ports = {80};
  Q4T_CHECK(
      FetchFileInput(Access(), source, options, &budget, &receipt, control)
          .http_status == 403);
  options = {};
  source.value = "file:///etc/passwd";
  Q4T_CHECK(
      FetchFileInput(Access(), source, options, &budget, &receipt, control)
          .http_status == 400);
  Q4T_CHECK(control.counters->started == 0 && budget.used_files == 0 &&
            receipt.content.bytes.empty());
  return true;
}
