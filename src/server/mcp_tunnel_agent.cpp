#include "q4t/server/mcp_tunnel_agent.h"

#include <signal.h>

#include <algorithm>
#include <charconv>
#include <cstring>
#include <ctime>
#include <deque>
#include <limits>
#include <thread>

#include "file_fetch_internal.h"
#include "mcp_tunnel_agent_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "remote_http_internal.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using Deadline = Clock::time_point;
using namespace std::chrono_literals;
using file_detail::RemoteHeaders;
using file_detail::RemoteHttp;
using file_detail::Url;
constexpr auto kWatchdog = 35s;
volatile sig_atomic_t agent_stopped = 0;
void StopAgent(int) { agent_stopped = 1; }
bool Text(std::string_view text, size_t limit) {
  return !text.empty() && text.size() <= limit &&
         std::all_of(text.begin(), text.end(),
                     [](unsigned char c) { return c >= 0x21 && c <= 0x7e; });
}
bool SendEvent(std::string_view event) {
  auto bytes = OutputJson(Object({{"event", String(std::string(event))}}));
  Frame frame{kVersion, 1, bytes.size()};
  return detail::WorkerTransfer(&frame, sizeof(frame), true) &&
         detail::WorkerTransfer(bytes.data(), bytes.size(), true);
}
bool WorkerStopped() {
  const auto available = detail::WaitWorkerInput(0);
  if (available < 0) return true;
  if (!available) return false;
  uint8_t stop = 0;
  return !detail::WorkerTransfer(&stop, sizeof(stop), false) || stop == 1;
}
bool WorkerDelay(std::chrono::milliseconds delay, Deadline deadline) {
  const auto end = std::min(deadline, Clock::now() + delay);
  if (!SendEvent("backoff")) return false;
  while (Clock::now() < end) {
    if (WorkerStopped()) return false;
    std::this_thread::sleep_for(std::min(
        25ms,
        std::chrono::ceil<std::chrono::milliseconds>(end - Clock::now())));
  }
  return Clock::now() < deadline;
}
std::string Escape(std::string_view value) {
  constexpr char hex[] = "0123456789ABCDEF";
  std::string encoded;
  for (unsigned char c : value) {
    if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.') {
      encoded.push_back(c);
    } else {
      encoded.push_back('%');
      encoded.push_back(hex[c >> 4]);
      encoded.push_back(hex[c & 15]);
    }
  }
  return encoded;
}
std::chrono::milliseconds RetryDelay(std::string_view value, unsigned attempt,
                                     unsigned jitter) {
  const auto base = std::min(5000U, 100U << std::min(attempt, 6U));
  uint64_t seconds = 0;
  const auto parsed =
      std::from_chars(value.data(), value.data() + value.size(), seconds);
  std::chrono::milliseconds minimum{0};
  if (!value.empty() && parsed.ec == std::errc{} &&
      parsed.ptr == value.data() + value.size()) {
    minimum = std::chrono::milliseconds(1000 * std::min<uint64_t>(seconds, 30));
  } else if (value.size() < 128) {
    std::tm utc{};
    const std::string date(value);
    const auto* end = strptime(date.c_str(), "%a, %d %b %Y %H:%M:%S GMT", &utc);
    if (end && !*end) {
      const auto delta = timegm(&utc) - std::time(nullptr);
      minimum =
          std::chrono::milliseconds(1000 * std::clamp<int64_t>(delta, 0, 30));
    }
  }
  return std::max(std::chrono::milliseconds(base + jitter % 100), minimum);
}
class Agent {
 public:
  explicit Agent(McpTunnelAgentConfig config)
      : config_(std::move(config)),
        backend_(config_.target, config_.target_fetch),
        instance_(detail::NewRequestId("q4t-agent-instance-")) {
    file_detail::ParseUrl(config_.api_base, &base_);
    if (base_.target == "/") base_.target = "/v1";
    base_.target += "/tunnels/" + config_.tunnel_id;
    headers_ = {{"Authorization", "Bearer " + config_.api_key},
                {"Content-Type", "application/json"},
                {"Accept", "application/json"},
                {"X-Tunnel-Client-Instance-Id", instance_},
                {"X-Tunnel-Client-Wire-Protocol-Version", "2026-08-25"}};
  }
  int Run() {
    if (!Text(instance_, 128) || !SendEvent("ready")) return 3;
    unsigned failures = 0;
    while (!WorkerStopped()) {
      backend_.Collect();
      if (!SendEvent("polling")) return 3;
      std::unique_ptr<RemoteHttp> response;
      const auto deadline =
          Clock::now() +
          std::chrono::milliseconds(config_.control_fetch.timeout_ms);
      auto url = base_;
      url.target +=
          "/poll?limit=1&timeout_ms=1000&channel=" + Escape(config_.channel);
      auto reply = RemoteHttp::Open(url, config_.control_fetch, "GET", headers_,
                                    "", 8U << 20, deadline, &response);
      // Receipt of the HTTP response anchors every command, before body read.
      const auto received = Clock::now();
      if (reply.ok() &&
          (response->Status() == 401 || response->Status() == 403 ||
           response->Status() == 404)) {
        SendEvent("authorization_required");
        backend_.Clear();
        return 2;
      }
      if (!reply.ok() ||
          (response->Status() != 200 && response->Status() != 204)) {
        const auto delay =
            RetryDelay(response ? response->Header("retry-after") : "",
                       std::min(failures++, 6U), Jitter());
        if (!WorkerDelay(delay, Deadline::max())) break;
        continue;
      }
      if (response->Status() == 204) {
        failures = 0;
        if (!WorkerDelay(25ms, Deadline::max())) break;
        continue;
      }
      std::string body;
      Json poll;
      reply = response->ReadAll(&body);
      if (!reply.ok() || !Parse(body, &poll) || !poll.GetArray("commands") ||
          poll.GetArray("commands")->array.size() > 25) {
        if (!WorkerDelay(RetryDelay({}, std::min(failures++, 6U), Jitter()),
                         Deadline::max()))
          break;
        continue;
      }
      failures = 0;
      // A peer may legally return more than limit. Process every bounded item;
      // each retains its original receipt deadline, never a fresh allowance.
      for (const auto& command : poll.GetArray("commands")->array) {
        if (WorkerStopped()) break;
        if (!Text(command.GetString("request_id"), 128) ||
            !Text(command.GetString("shard_token"), 128) ||
            command.GetString("channel", "main") != config_.channel)
          continue;
        const auto expires = mcp_detail::TunnelCommandDeadline(
            command, received,
            std::chrono::milliseconds(config_.target_fetch.timeout_ms));
        if (Clock::now() >= expires) continue;
        if (!SendEvent("executing")) return 3;
        const auto result =
            backend_.Exchange(command, expires, [&](const Json& event) {
              return Upload(command, event, expires, true);
            });
        if (result.ok() && Clock::now() < expires)
          (void)Upload(command, result.value, expires, false);
        if (authorization_failed_) {
          SendEvent("authorization_required");
          backend_.Clear();
          return 2;
        }
        if (!SendEvent("completed")) return 3;
      }
    }
    backend_.Clear();
    SendEvent("stopped");
    return 0;
  }

 private:
  unsigned Jitter() {
    // Scheduling jitter needs no cryptographic identity or shared RNG state.
    return static_cast<unsigned>(std::hash<std::string>{}(instance_) +
                                 ++sequence_ * 37);
  }
  bool Upload(const Json& command, const Json& response, Deadline deadline,
              bool progress) {
    if (WorkerStopped() || Clock::now() >= deadline) return false;
    auto url = base_;
    url.target += "/response";
    auto headers = headers_;
    headers["X-Tunnel-Shard-Token"] = command.GetString("shard_token");
    const auto body = OutputJson(response);
    if (body.size() > (4U << 20)) return false;
    for (unsigned attempt = 0; attempt < 3 && Clock::now() < deadline;
         ++attempt) {
      std::unique_ptr<RemoteHttp> reply;
      const auto status = RemoteHttp::Open(
          url, config_.control_fetch, "POST", headers, body, 65536,
          std::min(deadline, Clock::now() + 2s), &reply, 4U << 20);
      const int code = reply ? reply->Status() : 0;
      if (code == 401 || code == 403) authorization_failed_ = true;
      if (status.ok() && code >= 200 && code < 300) return true;
      if (code == 404) return !progress;
      // A notification with an ambiguous write must not be replayed. Skipping
      // it never suppresses the backend's eventual terminal response.
      const bool retry = progress
                             ? code == 429
                             : !status.ok() || code == 408 || code == 429 ||
                                   code == 502 || code == 503 || code == 504;
      if (!retry || attempt == 2 ||
          !WorkerDelay(RetryDelay(reply ? reply->Header("retry-after") : "",
                                  attempt, Jitter()),
                       deadline))
        return false;
    }
    return false;
  }
  McpTunnelAgentConfig config_;
  mcp_detail::TunnelAgentBackend backend_;
  std::string instance_;
  Url base_;
  RemoteHeaders headers_;
  unsigned sequence_ = 0;
  bool authorization_failed_ = false;
};
}  // namespace

Deadline mcp_detail::TunnelCommandDeadline(
    const Json& command, Deadline received,
    std::chrono::milliseconds local_limit) {
  auto duration =
      std::chrono::duration_cast<std::chrono::nanoseconds>(local_limit);
  const auto* value = command.Find("response_timeout");
  if (!value || !value->IsString()) return received + duration;
  const auto& text = value->str;
  const auto end = text.find_first_not_of("0123456789");
  if (!end || end == text.npos) return received + duration;
  uint64_t count = 0, unit = 0;
  const auto parsed = std::from_chars(text.data(), text.data() + end, count);
  if (parsed.ec != std::errc{} || parsed.ptr != text.data() + end)
    return received + duration;
  const auto suffix = std::string_view(text).substr(end);
  if (suffix == "ns") unit = 1;
  if (suffix == "us") unit = 1000;
  if (suffix == "ms") unit = 1000000;
  if (suffix == "s") unit = 1000000000;
  if (suffix == "m") unit = 60000000000;
  if (suffix == "h") unit = 3600000000000;
  if (!unit ||
      count > static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) / unit)
    return received + duration;
  return received + std::min(duration, std::chrono::nanoseconds(count * unit));
}
int RunMcpTunnelAgent(const McpTunnelAgentConfig& config,
                      const std::function<bool()>& stop) {
  const auto bytes = SerializeMcpTunnelAgentConfig(config);
  McpTunnelAgentConfig checked;
  if (!ParseMcpTunnelAgentConfig(bytes, &checked)) return 2;
  std::deque<Deadline> launches;
  const auto stopped = [&] { return stop && stop(); };
  while (!stopped()) {
    detail::PollIsolatedWorkers(IsolatedWorkerDomain::kMcp);
    const auto now = Clock::now();
    while (!launches.empty() && now - launches.front() >= 5min)
      launches.pop_front();
    if (launches.size() > config.restart_limit ||
        detail::IsolatedCleanupPending(IsolatedWorkerDomain::kMcp)) {
      std::this_thread::sleep_for(25ms);
      continue;
    }
    GrammarControl control;
    control.domain = IsolatedWorkerDomain::kMcp;
    control.address_space_bytes = kMcpWorkerAddressSpaceLimit;
    control.compile_deadline = Clock::now() + 3s;
    std::unique_ptr<detail::GrammarProcess> worker;
    launches.push_back(now);
    if (!detail::GrammarProcess::Start("--mcp-tunnel-agent-worker", control,
                                       &worker)) {
      std::this_thread::sleep_for(100ms);
      continue;
    }
    auto input = bytes;
    Frame frame{kVersion, 1, input.size()};
    bool alive = worker->Transfer(&frame, sizeof(frame), true,
                                  Clock::now() + 3s, stopped) &&
                 worker->Transfer(input.data(), input.size(), true,
                                  Clock::now() + 3s, stopped);
    auto watchdog = Clock::now() + kWatchdog;
    while (alive && !stopped()) {
      if (Clock::now() >= watchdog) break;
      // A terminal event can be buffered when pidfd reports exit. Drain the
      // owned channel first so an authorization failure cannot trigger
      // recovery.
      if (!worker->OutputReady()) {
        if (worker->Exited()) break;
        std::this_thread::sleep_for(10ms);
        continue;
      }
      const auto deadline = std::min(watchdog, Clock::now() + 2s);
      alive = worker->Transfer(&frame, sizeof(frame), false, deadline).ok();
      if (!alive || frame.version != kVersion || frame.sequence != 1 ||
          !frame.bytes || frame.bytes > 4096)
        break;
      std::string body(frame.bytes, '\0');
      Json event;
      if (!worker->Transfer(body.data(), body.size(), false, deadline) ||
          !Parse(body, &event))
        break;
      const auto state = event.GetString("event");
      if (state == "authorization_required") {
        // Cleanup is auxiliary. Its timeout cannot erase a known terminal
        // configuration failure or spend another recovery attempt.
        const auto end = Clock::now() + 2s;
        while (!worker->Exited() && Clock::now() < end)
          std::this_thread::sleep_for(5ms);
        return 2;
      }
      if (state == "stopped") return 0;
      if (state != "ready" && state != "polling" && state != "backoff" &&
          state != "executing" && state != "completed")
        break;
      watchdog = Clock::now() + kWatchdog;
    }
    if (stopped()) {
      // Best-effort graceful cleanup, with a fixed independent deadline.
      const auto end = Clock::now() + 2s;
      uint8_t quit = 1;
      if (worker->Transfer(&quit, sizeof(quit), true, end))
        while (!worker->Exited() && Clock::now() < end)
          std::this_thread::sleep_for(5ms);
      worker.reset();
      return 0;
    }
    worker.reset();  // Existing bounded reap retains an unreaped worker slot.
    std::this_thread::sleep_for(100ms);
  }
  return 0;
}
int MaybeRunMcpTunnelAgentWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--mcp-tunnel-agent-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  struct sigaction ignored {};
  ignored.sa_handler = SIG_IGN;
  sigemptyset(&ignored.sa_mask);
  if (sigaction(SIGPIPE, &ignored, nullptr)) return 2;
  try {
    Frame frame;
    if (!detail::WorkerTransfer(&frame, sizeof(frame), false) ||
        frame.version != kVersion || frame.sequence != 1 || !frame.bytes ||
        frame.bytes > kMcpTunnelAgentConfigBytes)
      return 3;
    std::string bytes(frame.bytes, '\0');
    McpTunnelAgentConfig config;
    if (!detail::WorkerTransfer(bytes.data(), bytes.size(), false) ||
        !ParseMcpTunnelAgentConfig(bytes, &config))
      return 3;
    return Agent(std::move(config)).Run();
  } catch (...) {
    return 4;
  }
}
int RunMcpTunnelAgentCommand(int argc, char** argv) {
  if (argc != 4 ||
      (std::strcmp(argv[2], "check") && std::strcmp(argv[2], "run"))) {
    std::fprintf(stderr, "Usage: q4t mcp-tunnel-agent (check|run) FILE\n");
    return 2;
  }
  McpTunnelAgentConfig config;
  if (!LoadMcpTunnelAgentConfig(argv[3], &config)) {
    std::fprintf(stderr, "Invalid private MCP tunnel agent configuration\n");
    return 2;
  }
  if (!std::strcmp(argv[2], "check")) {
    std::printf("%s\n", OutputJson(McpTunnelAgentDescription(config)).c_str());
    return 0;
  }
  struct sigaction action {
  }, old_int{}, old_term{};
  action.sa_handler = StopAgent;
  sigemptyset(&action.sa_mask);
  agent_stopped = 0;
  if (sigaction(SIGINT, &action, &old_int)) return 2;
  ScopeExit restore_int([&] { sigaction(SIGINT, &old_int, nullptr); });
  if (sigaction(SIGTERM, &action, &old_term)) return 2;
  ScopeExit restore_term([&] { sigaction(SIGTERM, &old_term, nullptr); });
  return RunMcpTunnelAgent(config, [] { return agent_stopped != 0; });
}
}  // namespace q4t::server
