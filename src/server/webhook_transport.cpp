#include "q4t/server/webhook.h"

#include <signal.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/native_crypto.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/realtime_transcription_protocol.h"
#include "remote_http_internal.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
constexpr size_t kWireBytes = 128 * 1024;

ObjectReply Failure(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Webhook delivery cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(504, "webhook_timeout", "Subscriber did not respond in time");
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "webhook_capacity", "Webhook sender is busy");
  return Error(503, "webhook_unavailable", "Webhook sender unavailable");
}

ObjectReply Send(const Json& request) {
  FileFetchOptions network;
  file_detail::Url url;
  const auto* options = request.Find("network");
  const auto* event = request.Find("event");
  const auto* secrets = request.GetArray("secrets");
  ChatPlanError error;
  if (!options || !ParseFileFetchOptions(*options, &network).ok() ||
      !file_detail::ParseUrl(request.GetString("url"), &url) || !url.tls ||
      !file_detail::AllowedUrl(url, network) || !event ||
      !ValidateWebhook(*event, "event", &error) || !secrets ||
      secrets->array.empty() || secrets->array.size() > 8)
    return Error(503, "webhook_record_invalid", "Invalid webhook delivery");

  const auto id = event->GetString("id");
  if (!id.starts_with("evt_") || !ValidId(id))
    return Error(503, "webhook_record_invalid", "Invalid webhook event ID");
  const auto timestamp = std::to_string(std::time(nullptr));
  const auto body = OutputJson(*event);
  const auto message = id + "." + timestamp + "." + body;
  std::string signatures;
  for (const auto& secret : secrets->array) {
    std::string key, signature;
    if (!secret.IsString() || !secret.str.starts_with("whsec_") ||
        secret.str.size() != 50 ||
        !DecodeRealtimeAudioBase64(std::string_view(secret.str).substr(6), &key)
             .ok() ||
        key.size() != 32)
      return Error(503, "webhook_record_invalid", "Invalid webhook key");
    const auto signed_ok = CryptoHmacSha256(key, message, &signature);
    CryptoWipe(&key);
    if (signed_ok != CryptoResult::kOk)
      return Error(503, "webhook_signing_unavailable", "Signing unavailable");
    if (!signatures.empty()) signatures += ' ';
    signatures += "v1," + signature;
  }
  file_detail::RemoteHeaders headers{{"Content-Type", "application/json"},
                                     {"webhook-id", id},
                                     {"webhook-timestamp", timestamp},
                                     {"webhook-signature", signatures}};
  std::unique_ptr<file_detail::RemoteHttp> response;
  const auto deadline =
      Clock::now() + std::chrono::milliseconds(network.timeout_ms);
  auto sent = file_detail::RemoteHttp::Open(url, network, "POST", headers, body,
                                            65536, deadline, &response);
  if (!sent.ok())
    return Error(502, "webhook_delivery_failed", "No subscriber HTTP response");
  ObjectReply reply;
  reply.value = Number(response->Status());
  return reply;
}
}  // namespace

Status LoadWebhookPolicy(const std::string& path, FileFetchOptions* network,
                         std::string* source_sha256) {
  std::string raw;
  Json config;
  FileFetchOptions parsed;
  if (!ReadMcpPrivateConfig(path, &raw) || !Parse(raw, &config) ||
      config.object.size() != 2 || config.GetInt("version") != 1 ||
      !config.Find("network") ||
      !ParseFileFetchOptions(*config.Find("network"), &parsed).ok() ||
      parsed.allowed_hosts.empty())
    return Status::Fail("Invalid webhook delivery policy");
  parsed.allow_http = false;
  parsed.max_redirects = 0;
  if (source_sha256) *source_sha256 = Digest(raw);
  *network = std::move(parsed);
  return {};
}

int RunWebhookPolicyCheck(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "Usage: q4t webhook-policy-check FILE\n");
    return 2;
  }
  FileFetchOptions network;
  std::string digest;
  if (!LoadWebhookPolicy(argv[2], &network, &digest).ok()) {
    std::fprintf(stderr, "Invalid webhook delivery policy\n");
    return 2;
  }
  const auto binding = Object({
      {"profile", String("q4t-webhook-delivery-policy-v1")},
      {"configured", Boolean(true)}, {"sha256", String(digest)},
      {"request_override", Boolean(false)}, {"https_only", Boolean(true)},
      {"redirects", Number(0)}, {"allowed_host_count", Number(network.allowed_hosts.size())},
      {"core_readiness_dependency", Boolean(false)}});
  std::printf("%s\n", OutputJson(binding).c_str());
  return 0;
}

ObjectReply DeliverWebhook(const StateAccess& access,
                           const FileFetchOptions& network,
                           const std::string& url, const Json& event,
                           const std::vector<std::string>& signing_secrets) {
  if (access.cancelled && access.cancelled())
    return Failure(GrammarFailure::kCancelled);
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kWebhook;
  control.compile_deadline =
      std::min(access.deadline,
               Clock::now() + std::chrono::milliseconds(network.timeout_ms));
  if (Clock::now() >= control.compile_deadline)
    return Failure(GrammarFailure::kTimeout);
  control.cancelled = access.cancelled;
  auto secrets = Array();
  for (const auto& key : signing_secrets) secrets.array.push_back(String(key));
  auto wire = OutputJson(Object({{"network", file_detail::OptionsJson(network)},
                                 {"url", String(url)},
                                 {"event", event},
                                 {"secrets", std::move(secrets)}}));
  if (wire.empty() || wire.size() > kWireBytes) {
    CryptoWipe(&wire);
    return Error(413, "webhook_delivery_limit", "Webhook delivery too large");
  }
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  if (!detail::GrammarProcess::Start("--webhook-worker", control, &process,
                                     &failure)) {
    CryptoWipe(&wire);
    return Failure(failure);
  }
  const auto transfer = [&](void* bytes, size_t count, bool write) {
    return process->Transfer(bytes, count, write, control.compile_deadline,
                             access.cancelled);
  };
  Frame frame{kVersion, 1, wire.size()};
  const auto sent = transfer(&frame, sizeof(frame), true) &&
                    transfer(wire.data(), wire.size(), true);
  CryptoWipe(&wire);
  if (!sent || !transfer(&frame, sizeof(frame), false))
    return Failure(process->Failure());
  if (frame.version != kVersion || frame.sequence != 1 || !frame.bytes ||
      frame.bytes > 4096)
    return Failure(GrammarFailure::kWorker);
  wire.resize(frame.bytes);
  Json reply;
  if (!transfer(wire.data(), wire.size(), false))
    return Failure(process->Failure());
  if (!Parse(wire, &reply)) return Failure(GrammarFailure::kWorker);
  auto decoded = DecodeReply(reply);
  if (decoded.ok() &&
      (!decoded.value.IsNumber() || decoded.value.AsInt() < 100 ||
       decoded.value.AsInt() > 599))
    return Failure(GrammarFailure::kWorker);
  return decoded;
}

int MaybeRunWebhookWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--webhook-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  struct sigaction ignored {};
  ignored.sa_handler = SIG_IGN;
  sigemptyset(&ignored.sa_mask);
  if (sigaction(SIGPIPE, &ignored, nullptr)) return 2;
  try {
    Frame frame;
    if (!detail::WorkerTransfer(&frame, sizeof(frame), false) ||
        frame.version != kVersion || frame.sequence != 1 || !frame.bytes ||
        frame.bytes > kWireBytes)
      return 3;
    std::string wire(frame.bytes, '\0');
    if (!detail::WorkerTransfer(wire.data(), wire.size(), false)) return 3;
    Json request;
    if (!Parse(wire, &request)) return 3;
    CryptoWipe(&wire);
    wire = OutputJson(WireReply(Send(request)));
    frame.bytes = wire.size();
    return detail::WorkerTransfer(&frame, sizeof(frame), true) &&
                   detail::WorkerTransfer(wire.data(), wire.size(), true)
               ? 0
               : 3;
  } catch (...) {
    return 4;
  }
}
}  // namespace q4t::server
