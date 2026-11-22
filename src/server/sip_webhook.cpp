// Private, deployment-configured webhook sender. DNS, TLS, signing and a
// stalled subscriber stay in this process, outside the SIP transaction loop.
#include "q4t/server/sip_webhook.h"

#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstring>

#include "q4t/server/grammar_process.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/native_crypto.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/realtime_transcription_protocol.h"
#include "remote_http_internal.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr int kSocket = 3;
constexpr size_t kPacket = 66560;
using Clock = std::chrono::steady_clock;

struct Subscription {
  file_detail::Url url;
  FileFetchOptions network;
  std::string scope, key;
  ~Subscription() { CryptoWipe(&key); }
};

bool Configure(const std::string& text, Subscription* sub) {
  Json j;
  if (!io::ParseJson(text, &j, false, {8, 512, 32768, true}) || !j.IsObject() ||
      j.object.size() != 4)
    return false;
  const auto* network = j.Find("network");
  sub->scope = j.GetString("scope");
  if (sub->scope.empty() || sub->scope.size() > 128 ||
      !std::all_of(sub->scope.begin(), sub->scope.end(), http::IsToken) ||
      !network || !ParseFileFetchOptions(*network, &sub->network).ok() ||
      !file_detail::ParseUrl(j.GetString("url"), &sub->url) || !sub->url.tls ||
      !file_detail::AllowedUrl(sub->url, sub->network))
    return false;
  for (auto& [name, value] : j.object) {
    if (name != "secret" || !value.IsString() || value.str.size() > 4096)
      continue;
    if (value.str.starts_with("whsec_")) {
      const auto decoded = DecodeRealtimeAudioBase64(
          std::string_view(value.str).substr(6), &sub->key);
      CryptoWipe(&value.str);
      return decoded.ok() && !sub->key.empty();
    }
    sub->key = std::move(value.str);
    return !sub->key.empty();
  }
  return false;
}

bool Headers(std::string_view wire, Json* array) {
  std::string name, value;
  auto finish = [&] {
    const auto lower = http::Lower(name);
    if (!name.empty() && lower != "authorization" &&
        lower != "proxy-authorization")
      array->array.push_back(
          Object({{"name", String(name)},
                  {"value", String(std::string(http::Trim(value)))}}));
  };
  while (!wire.empty()) {
    auto end = wire.find("\r\n");
    if (end == wire.npos || !end) return false;
    auto line = wire.substr(0, end);
    wire.remove_prefix(end + 2);
    for (unsigned char c : line)
      if ((c < 0x20 && c != '\t') || c == 0x7f) return false;
    if (line.front() == ' ' || line.front() == '\t') {
      if (name.empty()) return false;
      value += ' ';
      value += http::Trim(line);
    } else {
      finish();
      const auto colon = line.find(':');
      if (colon == line.npos) return false;
      name = http::Trim(line.substr(0, colon));
      if (name.empty() || !std::all_of(name.begin(), name.end(), http::IsToken))
        return false;
      value = http::Trim(line.substr(colon + 1));
    }
  }
  finish();
  return !array->array.empty();
}

int64_t Seconds() {
  return std::chrono::duration_cast<std::chrono::seconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::string Publish(const Subscription& sub, std::string_view packet) {
  // The immutable creation timestamp and event ID survive an explicit retry.
  // The delivery timestamp is fresh and covers the exact serialized body.
  const auto lf = packet.find('\n');
  if (lf == packet.npos) return "ERROR invalid_event";
  auto command = packet.substr(0, lf);
  auto wire = packet.substr(lf + 1);
  const std::string prefix = "EVENT " + sub.scope + " ";
  if (!command.starts_with(prefix)) return "ERROR invalid_event";
  command.remove_prefix(prefix.size());
  auto space = command.find(' ');
  if (space != 37 || !command.starts_with("live_"))
    return "ERROR invalid_event";
  const auto id = command.substr(0, space);
  if (id.substr(5).find_first_not_of("0123456789abcdef") != id.npos)
    return "ERROR invalid_event";
  const std::string receipt =
      "WEBHOOK " + sub.scope + " " + std::string(id) + " ";
  command.remove_prefix(space + 1);
  int64_t created = 0;
  const auto [end, ec] =
      std::from_chars(command.data(), command.data() + command.size(), created);
  Json headers = Array();
  if (ec != std::errc{} || end != command.data() + command.size() ||
      created <= 0 || detail::NormalizeUtf8(wire) != wire ||
      !Headers(wire, &headers))
    return receipt + "invalid";
  const std::string event_id = "evt_" + std::string(id.substr(5));
  const auto data = Object({{"type", String("sip")},
                            {"session_id", String(std::string(id))},
                            {"sip_headers", std::move(headers)}});
  const auto body =
      OutputJson(Object({{"id", String(event_id)},
                         {"object", String("event")},
                         {"type", String("live.transport.incoming")},
                         {"created_at", Number(created)},
                         {"data", data}}));
  const auto timestamp = std::to_string(Seconds());
  std::string signature;
  if (CryptoHmacSha256(sub.key, event_id + "." + timestamp + "." + body,
                       &signature) != CryptoResult::kOk)
    return receipt + "unavailable";
  file_detail::RemoteHeaders http_headers{
      {"Content-Type", "application/json"},
      {"webhook-id", event_id},
      {"webhook-timestamp", timestamp},
      {"webhook-signature", "v1," + signature}};
  std::unique_ptr<file_detail::RemoteHttp> response;
  const auto deadline =
      Clock::now() + std::chrono::milliseconds(sub.network.timeout_ms);
  // Name resolution can block in the system resolver outside poll deadlines.
  alarm((sub.network.timeout_ms + 999) / 1000 + 1);
  const auto reply =
      file_detail::RemoteHttp::Open(sub.url, sub.network, "POST", http_headers,
                                    body, 65536, deadline, &response);
  alarm(0);
  if (!reply.ok()) return receipt + "unknown";
  const int status = response->Status();
  return receipt +
         (status >= 200 && status < 300 ? "delivered " : "rejected ") +
         std::to_string(status);
}

bool Send(const std::string& message) {
  return send(kSocket, message.data(), message.size(), MSG_NOSIGNAL) ==
         static_cast<ssize_t>(message.size());
}

int Run() {
  int type = 0;
  socklen_t length = sizeof(type);
  if (getsockopt(kSocket, SOL_SOCKET, SO_TYPE, &type, &length) ||
      type != SOCK_SEQPACKET)
    return 2;
  Subscription sub;
  std::string packet(kPacket, '\0');
  bool configured = false;
  for (;;) {
    packet.resize(kPacket);
    const auto n = recv(kSocket, packet.data(), packet.size(), MSG_TRUNC);
    if (n < 0 && errno == EINTR) continue;
    if (n == 0) return 0;
    if (n < 0 || size_t(n) > packet.size()) return 2;
    packet.resize(n);
    if (!configured) {
      configured = Configure(packet, &sub);
      CryptoWipe(&packet);
      if (!configured || !Send("READY " + sub.scope)) return 2;
    } else if (!Send(Publish(sub, packet))) {
      return 1;
    }
  }
}
}  // namespace

int MaybeRunSipWebhookWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--sip-webhook-worker")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  signal(SIGPIPE, SIG_IGN);
  signal(SIGALRM, SIG_DFL);
  sigset_t mask;
  sigemptyset(&mask);
  sigaddset(&mask, SIGALRM);
  sigprocmask(SIG_UNBLOCK, &mask, nullptr);
  try {
    return Run();
  } catch (...) {
    return 1;
  }
}
}  // namespace q4t::server
