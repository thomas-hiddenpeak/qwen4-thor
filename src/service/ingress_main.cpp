// Stable q4t HTTP entrypoint. Its failure/restart never owns the model lease.
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cstdio>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "q4t/server/client_identity.h"
#include "q4t/server/http_listener.h"
#include "q4t/server/http_request.h"
#include "q4t/server/model_catalog.h"
#include "q4t/service/guardian_wire.h"
#include "q4t/service/ingress_address.h"
#include "q4t/service/ingress_admin.h"
#include "q4t/service/ingress_cors.h"
#include "q4t/service/ingress_credentials.h"
#include "q4t/service/ingress_transport.h"

namespace q4t::service::ingress {
namespace {
using namespace std::chrono_literals;
std::atomic<bool> reload_credentials{false};

void Require(bool ok) {
  if (!ok) throw std::runtime_error("invalid ingress configuration");
}

uint64_t Number(const io::Json& value, const char* key, uint64_t lower,
                uint64_t upper) {
  uint64_t number = 0;
  Require(wire::GetUnsigned(value, key, &number).ok() && number >= lower &&
          number <= upper);
  return number;
}

std::string Text(const io::Json& value, const char* key, size_t limit) {
  const auto* member = value.Find(key);
  Require(member && member->IsString() && !member->str.empty() &&
          member->str.size() <= limit);
  return member->str;
}

struct Client {
  std::string id;
  std::string key;
};

struct Config {
  ProcessIdentity parent;
  std::string address;
  std::string host;
  std::string listen;
  bool tls = false;
  TlsFiles credentials;
  Cors cors;
  int port = 0;
  int backend = 0;
  int operations = 0;
  int global_inflight = 0;
  int client_inflight = 0;
  int rps = 0;
  int burst = 0;
  int idle_seconds = 0;
  size_t body_bytes = 0;
  std::vector<Network> networks;
  std::vector<Client> clients;
};

Config Load(const char* path) {
  OwnedFd fd(::open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
  struct stat info {};
  Require(fd.Get() >= 0 && ::fstat(fd.Get(), &info) == 0 &&
          S_ISREG(info.st_mode) && info.st_uid == ::geteuid() &&
          !(info.st_mode & 0077) && info.st_size > 0 &&
          info.st_size <= static_cast<off_t>(wire::kMaxFrame));
  std::string raw;
  std::array<char, 4096> buffer{};
  for (;;) {
    const ssize_t n = ::read(fd.Get(), buffer.data(), buffer.size());
    if (n < 0 && errno == EINTR) continue;
    Require(n >= 0);
    if (!n) break;
    raw.append(buffer.data(), static_cast<size_t>(n));
    Require(raw.size() <= wire::kMaxFrame);
  }
  io::Json root;
  Require(wire::Parse(raw, &root).ok());
  const auto root_version = Number(root, "version", 1, 2);
  Require(
      root_version == 1
          ? wire::HasKeys(root, {"version", "parent_pid", "parent_start_ticks",
                                 "address", "policy"})
          : wire::HasKeys(root, {"version", "parent_pid", "parent_start_ticks",
                                 "address", "policy", "tls", "tls_lease"}));
  Config config;
  config.parent.pid =
      static_cast<pid_t>(Number(root, "parent_pid", 2, INT32_MAX));
  config.parent.start_ticks = Number(root, "parent_start_ticks", 1, INT64_MAX);
  config.address = Text(root, "address", 100);
  for (unsigned char c : config.address)
    Require((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-');
  const auto& policy = *root.Find("policy");
  const auto version = Number(policy, "version", 2, 3);
  Require(
      version == 2
          ? wire::HasKeys(
                policy, {"version", "listen_host", "listen_port", "server_name",
                         "upstream_port", "upstream_operations_port", "clients",
                         "allowed_client_cidrs", "per_client_inflight",
                         "global_inflight", "per_client_rps", "burst",
                         "body_bytes", "upstream_idle_seconds"})
          : wire::HasKeys(
                policy, {"version", "listen_host", "listen_port", "server_name",
                         "upstream_port", "upstream_operations_port", "clients",
                         "allowed_client_cidrs", "per_client_inflight",
                         "global_inflight", "per_client_rps", "burst",
                         "body_bytes", "upstream_idle_seconds", "cors"}));
  config.listen = Text(policy, "listen_host", 45);
  const auto listen = Address::Parse(config.listen);
  Require(listen.family != AF_UNSPEC);
  config.host = Text(policy, "server_name", 253);
  for (unsigned char c : config.host)
    Require((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' ||
            c == '.' || (version == 3 && c == ':'));
  if (version == 3) {
    const auto* cors = policy.Find("cors");
    Require(cors && (cors->IsNull() || config.cors.Load(*cors)));
  }
  if (root_version == 2) {
    const auto* tls = root.Find("tls");
    Require(tls != nullptr);
    config.tls = !tls->IsNull();
    if (config.tls) {
      Require(wire::HasKeys(*tls, {"certificate", "key", "ca",
                                   "require_client_certificate"}) ||
              wire::HasKeys(*tls, {"certificate", "key", "ca", "crl",
                                   "require_client_certificate"}));
      config.credentials.certificate = Text(*tls, "certificate", 4096);
      config.credentials.key = Text(*tls, "key", 4096);
      const auto* ca = tls->Find("ca");
      const auto* mtls = tls->Find("require_client_certificate");
      Require(ca && ca->IsString() && ca->str.size() <= 4096 && mtls &&
              mtls->IsBool());
      config.credentials.ca = ca->str;
      config.credentials.client_certificate = mtls->boolean;
      const auto* crl = tls->Find("crl");
      if (crl) {
        Require(crl->IsString() && crl->str.size() <= 4096);
        config.credentials.crl = crl->str;
        Require(crl->str.empty() || config.credentials.client_certificate);
      }
      Require(config.credentials.client_certificate == !ca->str.empty());
      for (const auto* path :
           {&config.credentials.certificate, &config.credentials.key,
            &config.credentials.ca, &config.credentials.crl})
        Require(path->empty() || path->front() == '/');
      config.credentials.lease = Text(root, "tls_lease", 100);
      config.credentials.name = config.host;
    }
  }
  Require(config.tls || config.listen == "127.0.0.1" || config.listen == "::1");
  config.port = Number(policy, "listen_port", 1024, 65535);
  config.backend = Number(policy, "upstream_port", 1024, 65535);
  config.operations = Number(policy, "upstream_operations_port", 1024, 65535);
  Require(config.port != config.backend && config.port != config.operations &&
          config.backend != config.operations);
  config.global_inflight = Number(policy, "global_inflight", 1, 8);
  config.client_inflight =
      Number(policy, "per_client_inflight", 1, config.global_inflight);
  config.rps = Number(policy, "per_client_rps", 1, 100);
  config.burst = Number(policy, "burst", 1, 128);
  config.body_bytes =
      Number(policy, "body_bytes", 1024, server::kChatBodyBytes);
  config.idle_seconds = Number(policy, "upstream_idle_seconds", 10, 3600);
  const auto* networks = policy.GetArray("allowed_client_cidrs");
  Require(networks && !networks->array.empty() && networks->array.size() <= 32);
  for (const auto& network : networks->array) {
    Require(network.IsString());
    Network parsed;
    Require(parsed.Parse(network.str));
    config.networks.push_back(parsed);
  }
  const auto* clients = policy.GetArray("clients");
  Require(clients && !clients->array.empty() && clients->array.size() <= 64);
  for (const auto& value : clients->array) {
    Require(wire::HasKeys(value, {"id", "api_key"}));
    Client client{Text(value, "id", 32), Text(value, "api_key", 128)};
    Require(server::ValidClientId(client.id) && client.key.size() >= 32);
    for (unsigned char c : client.key)
      Require((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_');
    for (const auto& prior : config.clients)
      Require(prior.id != client.id && prior.key != client.key);
    config.clients.push_back(std::move(client));
  }
  return config;
}

bool HostMatches(std::string_view host, const std::string& expected) {
  size_t colon = host.find(':');
  if (host.starts_with("[")) {
    const auto close = host.find(']');
    if (close == host.npos || close < 2 ||
        Address::Parse(std::string(host.substr(1, close - 1))).family !=
            AF_INET6)
      return false;
    if (close + 1 != host.size() && host[close + 1] != ':') return false;
    colon = close + 1 == host.size() ? host.npos : close + 1;
  }
  if (colon != host.npos) {
    const auto port = host.substr(colon + 1);
    unsigned value = 0;
    const auto result =
        std::from_chars(port.data(), port.data() + port.size(), value);
    if (port.empty() || result.ec != std::errc{} ||
        result.ptr != port.data() + port.size() || value == 0 || value > 65535)
      return false;
    host = host.substr(0, colon);
  }
  if (host.starts_with("[")) host = host.substr(1, host.size() - 2);
  const auto literal = Address::Parse(expected);
  if (literal.family != AF_UNSPEC) {
    const auto parsed = Address::Parse(std::string(host));
    return literal.family == parsed.family && literal.bytes == parsed.bytes;
  }
  if (host.size() != expected.size()) return false;
  for (size_t i = 0; i < host.size(); ++i) {
    char c = host[i];
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c != expected[i]) return false;
  }
  return true;
}

size_t Authenticate(std::string_view authorization,
                    const std::vector<Client>& clients) {
  if (authorization.size() < 7) return clients.size();
  constexpr std::string_view scheme = "bearer ";
  for (size_t i = 0; i < scheme.size(); ++i) {
    char c = authorization[i];
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c != scheme[i]) return clients.size();
  }
  const auto key = authorization.substr(7);
  if (key.size() < 32 || key.size() > 128) return clients.size();
  size_t matched = clients.size();
  for (size_t i = 0; i < clients.size(); ++i) {
    const auto& candidate = clients[i].key;
    unsigned difference = static_cast<unsigned>(key.size() ^ candidate.size());
    for (size_t j = 0; j < 128; ++j) {
      const unsigned a =
          j < key.size() ? static_cast<unsigned char>(key[j]) : 0;
      const unsigned b =
          j < candidate.size() ? static_cast<unsigned char>(candidate[j]) : 0;
      difference |= a ^ b;
    }
    if (!difference) matched = i;
  }
  return matched;
}

struct Usage {
  int active[2] = {0, 0};
  double tokens[2] = {0, 0};
  Clock::time_point updated[2] = {Clock::now(), Clock::now()};
};

class Capacity {
 public:
  explicit Capacity(const Config& config)
      : config_(config), usage_(config.clients.size()) {
    for (auto& used : usage_) {
      used.tokens[0] = config.burst + 1;
      used.tokens[1] = 21;
    }
  }
  bool Acquire(size_t client, bool control) {
    std::lock_guard guard(mutex_);
    const size_t lane = control ? 1 : 0;
    auto& used = usage_[client];
    const auto now = Clock::now();
    const double elapsed =
        std::chrono::duration<double>(now - used.updated[lane]).count();
    used.updated[lane] = now;
    used.tokens[lane] = std::min<double>(
        control ? 21 : config_.burst + 1,
        used.tokens[lane] + elapsed * (control ? 10 : config_.rps));
    if (used.active[lane] >= (control ? 4 : config_.client_inflight) ||
        active_[lane] >= (control ? 8 : config_.global_inflight) ||
        used.tokens[lane] < 1)
      return false;
    ++used.active[lane];
    ++active_[lane];
    used.tokens[lane] -= 1;
    return true;
  }
  void Release(size_t client, bool control) {
    std::lock_guard guard(mutex_);
    --usage_[client].active[control ? 1 : 0];
    --active_[control ? 1 : 0];
  }

 private:
  const Config& config_;
  std::mutex mutex_;
  std::vector<Usage> usage_;
  int active_[2] = {0, 0};
};

struct Lease {
  Capacity& capacity;
  size_t client = 0;
  bool control = false;
  bool acquired = false;
  ~Lease() {
    if (acquired) capacity.Release(client, control);
  }
};

struct RouteSpec {
  std::string_view method;
  std::string_view path;
  bool control;
};
constexpr RouteSpec kRoutes[] = {
    {"POST", "/v1/completions", false},
    {"POST", "/v1/chat/completions", false},
    {"POST", "/v1/chat/preflight", false},
    {"POST", "/v1/requests/cancel", true},
    {"GET", "/v1/models", true},
    {"GET", "/v1/capabilities", true},
    {"GET", "/livez", true},
    {"GET", "/readyz", true},
    {"GET", "/healthz", true},
};

bool Route(const std::string& method, const std::string& path, bool* control) {
  if ((method == "GET" || method == "DELETE") &&
      server::IsModelDetailPath(path) && path.find_first_of("?#") == path.npos) {
    *control = true;
    return true;
  }
  for (const auto& route : kRoutes) {
    if (method == route.method && path == route.path) {
      *control = route.control;
      return true;
    }
  }
  return false;
}

void Handle(Connection& client, const Config& config, Capacity& capacity,
            server::HttpBodyBudget& budget, Address source, bool* started) {
  std::string method, path, body;
  std::string request_id = RequestId();
  if (request_id.empty()) return;
  server::HttpRequestHeaders headers;
  server::HttpBodyLease body_lease(budget);
  Lease lease{capacity};
  Decision binding;
  int status = 400;
  bool rejected = false;
  const auto reject = [&](int code, const char* reason) {
    rejected = true;
    Error(client, code, reason, request_id, started);
    return false;
  };
  const auto authorize = [&] {
    return Authorize(config.address, config.parent.pid,
                     config.clients[lease.client].id, lease.control,
                     lease.control ? config.operations : config.backend);
  };
  const auto admit = [&] {
    if (!headers.request_id.empty()) request_id = headers.request_id;
    const bool source_allowed = std::any_of(
        config.networks.begin(), config.networks.end(),
        [&](const auto& network) { return network.Contains(source); });
    if (!source_allowed) return reject(403, "gateway_source_denied");
    if (!HostMatches(headers.host, config.host))
      return reject(400, "gateway_bad_request");
    if (!config.cors.Response(headers.origin, &client.extra_headers))
      return reject(403, "cors_origin_denied");
    if (method == "OPTIONS") {
      bool control = false;
      if (headers.origin.empty() || headers.cors_method.empty() ||
          headers.content_length || !headers.client_id.empty() ||
          !headers.gateway_token.empty())
        return reject(400, "cors_invalid_preflight");
      if (!Route(headers.cors_method, path, &control) ||
          !config.cors.Preflight(headers.cors_method, headers.cors_headers,
                                 &client.extra_headers))
        return reject(403, "cors_preflight_denied");
      Reply(client, 204, "", request_id, started);
      rejected = true;  // Completed locally, with no body/admission/quota work.
      return false;
    }
    if (!headers.cors_method.empty() || !headers.cors_headers.empty())
      return reject(400, "cors_invalid_preflight");
    lease.client = Authenticate(headers.authorization, config.clients);
    if (lease.client == config.clients.size())
      return reject(401, "gateway_unauthorized");
    if (!headers.client_id.empty() || !headers.gateway_token.empty())
      return reject(400, "invalid_client_header");
    if (!Route(method, path, &lease.control))
      return reject(404, "gateway_route_not_found");
    if (headers.content_length > config.body_bytes)
      return reject(413, "gateway_body_too_large");
    if (method == "GET" && headers.content_length)
      return reject(400, "gateway_bad_request");
    lease.acquired = capacity.Acquire(lease.client, lease.control);
    if (!lease.acquired) return reject(429, "gateway_rate_limit");
    if (path == "/livez") return true;
    binding = authorize();
    return binding.port != 0 || reject(503, binding.error.c_str());
  };
  const auto receive = [&](void* data, size_t size,
                           Clock::time_point deadline) {
    return client.Read(data, size, deadline);
  };
  if (!server::ReadHttpRequest(client.Fd(), &method, &path, &body, 10000,
                               &body_lease, &status, &headers, admit,
                               receive)) {
    if (!rejected) {
      config.cors.Response(headers.origin, &client.extra_headers);
      if (headers.failure == server::HttpReceiveFailure::kBodyTooLarge)
        status = 413;
      Error(client, status,
            headers.error_code.empty() ? "gateway_bad_request"
                                       : headers.error_code,
            request_id, started);
    }
    return;
  }
  if (path == "/livez") {
    Reply(client, 200, "{\"live\":true,\"scope\":\"service_entrypoint\"}",
          request_id, started);
    return;
  }
  const auto current = authorize();
  if (!current.port || current != binding) {
    Error(client, 503, current.port ? "service_recovering" : current.error,
          request_id, started);
    return;
  }
  auto upstream = Connect(binding.port, client);
  if (upstream.Get() < 0) {
    Error(client, 503, "service_binding_unavailable", request_id, started);
    return;
  }
  const std::string head =
      method + " " + path +
      " HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" +
      "Content-Length: " + std::to_string(body.size()) + "\r\n" +
      "X-Request-Id: " + request_id +
      "\r\nX-Q4T-Client-Id: " + config.clients[lease.client].id +
      "\r\nX-Q4T-Gateway-Token: " + binding.token + "\r\n" +
      (headers.content_type.empty()
           ? ""
           : "Content-Type: " + headers.content_type + "\r\n") +
      "\r\n";
  const auto deadline = Clock::now() + 15s;
  if (!Write(upstream.Get(), head, deadline, &client) ||
      !Write(upstream.Get(), body, deadline, &client)) {
    Error(client, 503, "service_binding_unavailable", request_id, started);
    return;
  }
  Relay(upstream.Get(), client, config.idle_seconds, request_id, started);
}

struct Slot {
  std::thread thread;
  std::atomic<bool> done{true};
  std::mutex mutex;
  int fd = -1;
};

int CheckTls(const Config& config) {
  Require(config.tls);
  Credentials credentials(config.credentials);
  const auto deadline = Clock::now() + 3s;
  do {
    credentials.Step(false);
    if (credentials.FinishedInitial()) break;
    ::poll(nullptr, 0, 10);
  } while (Clock::now() < deadline && !stopped.load());
  std::string status;
  Require(wire::Encode(credentials.Status(), &status).ok());
  std::puts(status.c_str());
  return credentials.Context() ? 0 : 1;
}

int Run(const Config& config) {
  OwnedFd parent;
  Require(::prctl(PR_SET_PDEATHSIG, SIGTERM) == 0 &&
          config.parent.pid == ::getppid() &&
          OpenProcess(config.parent, &parent).ok());
  server::HttpListeners listeners;
  IngressAdmin admin;
  Require(admin.Open(config.address + "-admin"));
  std::unique_ptr<Credentials> credentials;
  if (config.tls) {
    credentials = std::make_unique<Credentials>(config.credentials);
    do {
      credentials->Step(false);
      admin.Step(credentials.get());
      if (credentials->FinishedInitial()) break;
      pollfd parent_event{parent.Get(), POLLIN, 0};
      if (::poll(&parent_event, 1, 10) > 0) return 1;
    } while (!stopped.load());
    Require(credentials->Context() != nullptr);
  }
  Require(listeners.Open(config.listen, config.port, 0).ok());
  Capacity capacity(config);
  server::HttpBodyBudget budget(server::kAggregateChatBodyBytes);
  std::array<Slot, 32> slots;
  while (!stopped.load(std::memory_order_relaxed)) {
    if (credentials) credentials->Step(reload_credentials.exchange(false));
    admin.Step(credentials.get());
    pollfd events[2] = {{listeners.Public(), POLLIN, 0},
                        {parent.Get(), POLLIN, 0}};
    const int ready = ::poll(events, 2, 100);
    if (ready < 0 && errno == EINTR) continue;
    if (ready < 0 || events[1].revents) break;
    if (!(events[0].revents & POLLIN)) continue;
    sockaddr_storage peer{};
    socklen_t peer_size = sizeof(peer);
    OwnedFd accepted(::accept4(listeners.Public(),
                               reinterpret_cast<sockaddr*>(&peer), &peer_size,
                               SOCK_NONBLOCK | SOCK_CLOEXEC));
    if (accepted.Get() < 0) continue;
    const auto source = Address::Peer(peer, peer_size);
    if (source.family == AF_UNSPEC) continue;
    auto slot = std::find_if(slots.begin(), slots.end(),
                             [](const auto& item) { return item.done.load(); });
    if (slot == slots.end()) {
      constexpr std::string_view busy =
          "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
          "Retry-After: 1\r\nContent-Length: 0\r\n\r\n";
      if (!config.tls)
        ::send(accepted.Get(), busy.data(), busy.size(),
               MSG_NOSIGNAL | MSG_DONTWAIT);
      continue;
    }
    if (slot->thread.joinable()) slot->thread.join();
    slot->fd = accepted.Release();
    slot->done.store(false);
    const auto context = credentials ? credentials->Context() : nullptr;
    try {
      slot->thread = std::thread([&, slot, source, context] {
        bool started = false;
        try {
          Connection client(slot->fd);
          if (!config.tls || client.Handshake(context))
            Handle(client, config, capacity, budget, source, &started);
        } catch (...) {
          if (!started && !config.tls) {
            constexpr std::string_view failure =
                "HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                "Retry-After: 1\r\nContent-Length: 0\r\n\r\n";
            ::send(slot->fd, failure.data(), failure.size(),
                   MSG_NOSIGNAL | MSG_DONTWAIT);
          }
        }
        {
          std::lock_guard guard(slot->mutex);
          ::close(slot->fd);
          slot->fd = -1;
        }
        slot->done.store(true);
      });
    } catch (...) {
      ::close(slot->fd);
      slot->fd = -1;
      slot->done.store(true);
    }
  }
  stopped.store(true);
  listeners.Close();
  for (auto& slot : slots) {
    std::lock_guard guard(slot.mutex);
    if (slot.fd >= 0) ::shutdown(slot.fd, SHUT_RDWR);
  }
  for (auto& slot : slots)
    if (slot.thread.joinable()) slot.thread.join();
  return 0;
}
}  // namespace
}  // namespace q4t::service::ingress

int main(int argc, char** argv) {
  using namespace q4t::service::ingress;
  ::signal(SIGPIPE, SIG_IGN);
  if (argc == 3 && std::string_view(argv[1]) == "--tls-reader") {
    int parent = 0;
    const std::string_view number(argv[2]);
    const auto parsed =
        std::from_chars(number.data(), number.data() + number.size(), parent);
    if (parsed.ec != std::errc{} ||
        parsed.ptr != number.data() + number.size() || parent < 2)
      return 2;
    return CredentialReader(3, parent);
  }
  if (argc != 3 || (std::string_view(argv[1]) != "--config" &&
                    std::string_view(argv[1]) != "--check-tls")) {
    std::fputs("Usage: q4t-ingress --config FILE | --check-tls FILE\n", stderr);
    return 2;
  }
  struct sigaction action {};
  action.sa_handler = [](int) {
    stopped.store(true, std::memory_order_relaxed);
  };
  ::sigemptyset(&action.sa_mask);
  ::sigaction(SIGTERM, &action, nullptr);
  ::sigaction(SIGINT, &action, nullptr);
  action.sa_handler = [](int) { reload_credentials.store(true); };
  ::sigaction(SIGHUP, &action, nullptr);
  try {
    const auto config = Load(argv[2]);
    return std::string_view(argv[1]) == "--check-tls" ? CheckTls(config)
                                                      : Run(config);
  } catch (...) {
    // Config contains private credentials. Never print its contents/errors.
    std::fputs("q4t-ingress: configuration or startup failed\n", stderr);
    return 1;
  }
}
