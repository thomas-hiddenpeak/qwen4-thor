// Audit probe: production contract/template/parser and HTTP dispatch, with an
// unloaded backend. Never calls Start(), Run(), CUDA, or model generation.
// The synthetic output is supplied by the fixture, not by an LLM.
#include "q4t/server/chat_server.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/chat_template.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_json.h"

#include <sys/socket.h>
#include <unistd.h>

#include <iostream>
#include <stdexcept>

// Same unloaded ownership seam as tools/serve_reliability/http_lifecycle_test.
namespace q4t::model {
ModelOwner::~ModelOwner() = default;
DecodeMetadataUpload::~DecodeMetadataUpload() = default;
MetadataUploadBatch::~MetadataUploadBatch() = default;
}  // namespace q4t::model
namespace q4t::trace {
RouterCollector::~RouterCollector() = default;
}  // namespace q4t::trace
namespace q4t::server {
ChatServer::~ChatServer() = default;
SessionCache::~SessionCache() = default;
SessionCache::Entry::~Entry() = default;
void ChatServer::StopScheduler() {}
void ChatServer::HandleConfig(int fd) {
  detail::SendError(fd, 501, "audit fixture has no runtime configuration");
}
void ChatServer::HandlePreflight(int fd, const std::string&) {
  detail::SendError(fd, 501, "audit fixture has no planning backend");
}
void ChatServer::HandleChat(int fd, const std::string&) {
  // Reaching this stub proves dispatch only, NOT generation acceptance.
  detail::SendSimple(fd, 200, "OK", R"({"backend":"audit_stub"})",
                     "application/json");
}
struct ServeContractAccess {
  static void Handle(ChatServer& server, int fd) { server.HandleClient(fd); }
  static std::shared_ptr<RequestControl> Register(ChatServer& server) {
    return server.requests_.Register(
        "audit-request", std::string(64, 'a'),
        std::chrono::steady_clock::now() + std::chrono::seconds(5));
  }
};
}  // namespace q4t::server

namespace {
using q4t::io::Json;
using namespace q4t::server;

std::string Quote(const std::string& text) {
  return '"' + detail::JsonEscape(text) + '"';
}
std::string Stage(const char* name, const q4t::Status& status) {
  return Quote(name) + ":" + (status ? "true" : "false") + ',' +
         Quote(std::string(name) + "_message") + ':' + Quote(status.message());
}

std::string Chat(const Json& fixture) {
  const auto* req = fixture.Find("request");
  if (!req) throw std::runtime_error("missing fixture request");
  auto status = ValidateChatContract(*req, "qwen3.8-flash-next", false);
  std::string out = "{" + Stage("contract", status);
  if (!status) return out + '}';
  OutputConstraints plan;
  status = PlanOutputConstraints(*req, &plan);
  out += ',' + Stage("plan", status) + ",\"constrained\":" +
         (plan.active ? "true" : "false");
  std::string prompt;
  if (req->Has("messages")) {
    std::vector<std::string> contents;
    for (const auto& message : req->GetArray("messages")->array) {
      const auto* content = message.Find("content");
      if (content && !content->IsString() && !content->IsNull())
        throw std::runtime_error("probe accepts string/null contents only");
      contents.push_back(message.GetString("content"));
    }
    const auto rendered = ConstraintTemplateRequest(*req, plan);
    status = RenderChatPrompt(rendered, contents, &prompt);
  } else {
    prompt = req->GetString("prompt");
  }
  out += ',' + Stage("render", status) + ",\"prompt\":" + Quote(prompt);
  if (!status) return out + '}';
  ChatResponseParser parser;
  status = parser.Init(*req, prompt, "audit", true);
  out += ',' + Stage("parser_init", status);
  if (!status || !fixture.Has("generated")) return out + '}';
  std::vector<ResponseDelta> deltas;
  status = parser.Append(fixture.GetString("generated"), &deltas);
  out += ',' + Stage("append", status);
  status = parser.Finish(fixture.GetBool("length_limited"), &deltas);
  out += ',' + Stage("finish", status) + ",\"message\":" +
         parser.MessageJson();
  return out + '}';
}

std::string Http(const Json& fixture) {
  int sockets[2];
  if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets))
    throw std::runtime_error("socketpair failed");
  // All fixture packets/responses are small enough for the socket buffers.
  const timeval timeout{2, 0};
  for (int fd : sockets)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  ChatServer server;
  auto request = ServeContractAccess::Register(server);
  if (!detail::WriteAll(sockets[0], fixture.GetString("wire")))
    throw std::runtime_error("fixture write failed");
  ServeContractAccess::Handle(server, sockets[1]);
  shutdown(sockets[1], SHUT_WR);
  std::string response;
  char buffer[4096];
  for (;;) {
    const ssize_t n = recv(sockets[0], buffer, sizeof(buffer), 0);
    if (n == 0) break;
    if (n < 0) throw std::runtime_error("response read failed");
    response.append(buffer, static_cast<size_t>(n));
  }
  close(sockets[0]);
  close(sockets[1]);
  return "{\"wire\":" + Quote(response) + ",\"cancelled\":" +
         (request->Cancelled() ? "true" : "false") + '}';
}
}  // namespace

int main() {
  try {
    std::string line;
    while (std::getline(std::cin, line)) {
      Json fixture;
      const auto status = q4t::io::ParseJson(
          line, &fixture, true, q4t::server::kRequestJsonLimits);
      if (!status) throw std::runtime_error(status.message());
      const auto op = fixture.GetString("op");
      const auto result = op == "chat" ? Chat(fixture)
                          : op == "http" ? Http(fixture)
                                         : throw std::runtime_error("bad op");
      std::cout << "{\"case\":" << Quote(fixture.GetString("case"))
                << ",\"observation\":" << result << "}\n";
    }
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
}
