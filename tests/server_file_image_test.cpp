#include "q4t/server/file_image.h"

#include <arpa/inet.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/file_expansion.h"
#include "q4t/server/files.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/media_admission.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
BinaryAccess Access() { return {"client:image", Clock::now() + 10s, {}}; }
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(out ? out : ".q4t-work/file-image-cpu");
    fs::create_directories(root);
    auto path = (root / "file-image-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("image fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
std::string Bytes(const char* name = "original.png") {
  std::ifstream in(
      fs::path(__FILE__).parent_path() / "fixtures/file_images" / name,
      std::ios::binary);
  if (!in) throw std::runtime_error("original image");
  return std::string(std::istreambuf_iterator<char>(in), {});
}
std::string Upload(ObjectStore& objects, std::string_view bytes,
                   std::string name = "original.png") {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=image");
  const auto prefix =
      "--image\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nvision\r\n"
      "--image\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" +
      name +
      "\"\r\n"
      "Content-Type: application/octet-stream\r\n\r\n";
  auto reply = upload.Feed(prefix, Access().deadline);
  if (reply.ok()) reply = upload.Feed(bytes, Access().deadline);
  if (reply.ok()) reply = upload.Feed("\r\n--image--\r\n", Access().deadline);
  if (reply.ok()) reply = upload.Finish();
  if (!reply.ok()) throw std::runtime_error(reply.message);
  return reply.value.GetString("id");
}
Json Part(std::string field, std::string value, std::string detail = "auto") {
  return Object({{"type", String("input_image")},
                 {field, String(value)},
                 {"detail", String(detail)}});
}
Json Items(Json part) {
  return Array({Object({{"id", String("msg_image")},
                        {"type", String("message")},
                        {"role", String("user")},
                        {"status", String("completed")},
                        {"content", Array({std::move(part)})}})});
}
ObjectReply Source(const Json& part, FileInputSource* source) {
  return ParseImageInputSource(part, FileInputProfile::kResponses,
                               "input[0].content[0]", source);
}
class Server {
 public:
  Server() {
    fd_ = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t size = sizeof(address);
    if (fd_ < 0 || bind(fd_, reinterpret_cast<sockaddr*>(&address), size) ||
        getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &size) ||
        listen(fd_, 4))
      throw std::runtime_error("image listener");
    port = ntohs(address.sin_port);
    worker_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        pollfd event{fd_, POLLIN, 0};
        if (poll(&event, 1, 10) <= 0) continue;
        const int peer =
            accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (peer < 0) continue;
        ScopeExit close_peer([&] { close(peer); });
        const auto deadline = Clock::now() + 2s;
        std::string request;
        while (!stop.stop_requested() && Clock::now() < deadline &&
               request.find("\r\n\r\n") == request.npos &&
               request.size() < 32768) {
          char buffer[1024];
          const auto n = recv(peer, buffer, sizeof(buffer), MSG_DONTWAIT);
          if (n > 0)
            request.append(buffer, n);
          else if (!n)
            break;
          else
            std::this_thread::sleep_for(1ms);
        }
        const auto index = requests.fetch_add(1);
        auto body = Bytes();
        const auto wire =
            index == 0
                ? std::string(
                      "HTTP/1.1 302 Move\r\nLocation: "
                      "/actual?token=PRIVATE\r\nContent-Length: 0\r\n\r\n")
                : "HTTP/1.1 200 OK\r\nContent-Type: "
                  "image/png\r\nContent-Length: " +
                      std::to_string(body.size()) + "\r\n\r\n" + body;
        std::string_view rest(wire);
        while (!rest.empty() && !stop.stop_requested() &&
               Clock::now() < deadline) {
          const auto n =
              send(peer, rest.data(), rest.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
          if (n > 0)
            rest.remove_prefix(n);
          else if (n < 0 && (errno == EAGAIN || errno == EINTR))
            std::this_thread::sleep_for(1ms);
          else
            break;
        }
      }
    });
  }
  ~Server() {
    worker_.request_stop();
    worker_.join();
    close(fd_);
  }
  std::string Url() const {
    return "http://127.0.0.1:" + std::to_string(port) +
           "/image?signature=SECRET";
  }
  FileFetchOptions Policy() const {
    FileFetchOptions options;
    options.public_networks = false;
    options.allowed_networks = {"127.0.0.1/32"};
    options.ports = {port};
    return options;
  }
  int port = 0;
  std::atomic<int> requests{0};

 private:
  int fd_ = -1;
  std::jthread worker_;
};
}  // namespace

Q4T_TEST(file_image_source_contract_preserves_borrowed_payload_and_detail) {
  FileInputSource source;
  auto part = Part("image_url", "data:image/png;base64,eA==", "original");
  Q4T_CHECK(Ok(Source(part, &source)) && source.value == "eA==" &&
            source.detail == "original" &&
            source.value.data() >= part.Find("image_url")->str.data() &&
            source.value.data() < part.Find("image_url")->str.data() +
                                      part.GetString("image_url").size());
  for (const auto* detail : {"low", "high", "auto", "original"}) {
    part = Part("file_id", "file_owned", detail);
    Set(&part, "image_url", Json());
    Q4T_CHECK(Ok(Source(part, &source)) &&
              source.kind == FileInputSource::Kind::kStored &&
              source.detail == detail);
  }
  for (auto bad : {Part("image_url", ""), Part("file_id", "../other"),
                   Part("image_url", "http://user:pass@example.com/image"),
                   Part("image_url", "data:image/png,eA=="),
                   Part("file_id", "file_x", "unknown")})
    Q4T_CHECK(!Source(bad, &source).ok());
  part = Part("file_id", "file_x");
  Set(&part, "image_url", String("https://example.com/image.png"));
  Q4T_CHECK(!Source(part, &source).ok());
  const auto chat = Object(
      {{"type", String("image_url")},
       {"image_url", Object({{"url", String("https://example.com/image.png")},
                             {"detail", String("high")}})}});
  Q4T_CHECK(Ok(ParseImageInputSource(chat, FileInputProfile::kChat,
                                     "messages[0].content[0]", &source)) &&
            source.kind == FileInputSource::Kind::kUrl &&
            source.detail == "high");
  return true;
}

Q4T_TEST(file_image_actual_png_jpeg_files_reach_real_cpu_pixel_processor) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  for (const auto* name : {"original.png", "original.jpg"}) {
    const auto raw = Bytes(name), id = Upload(objects, raw, name);
    const auto part = Part("file_id", id);
    FileInputSource source;
    Q4T_CHECK(Ok(Source(part, &source)));
    FileInputBudget budget;
    FileImageInput image;
    Q4T_CHECK(Ok(ReadFileImage(&objects, Access(), source, {}, {}, &budget,
                               &image)) &&
              image.source.bytes == raw && image.source.sha256 == Digest(raw) &&
              image.source.file_id == id && image.header.width == 64 &&
              image.header.height == 32);
    q4t::vision::ProcessorConfig config;
    config.min_pixels = config.max_pixels = 2048;
    q4t::vision::ProcessedImage pixels;
    std::string error;
    Q4T_CHECK(q4t::vision::ProcessImage(
                  reinterpret_cast<const uint8_t*>(image.source.bytes.data()),
                  image.source.bytes.size(), config, &pixels, &error) &&
              pixels.grid_w == 4 && pixels.grid_h == 2 &&
              pixels.pixel_values.size() == 8 * 3 * 2 * 16 * 16);
  }
  return true;
}

Q4T_TEST(
    file_image_snapshot_restores_deleted_source_and_rechecks_header_and_budgets) {
  Fixture fixture;
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  const auto identity = Digest("original image part");
  FileImageInput expected;
  {
    ObjectStore objects(fixture.options);
    InputSnapshotStore snapshots(objects);
    const auto id = Upload(objects, Bytes());
    const auto part = Part("file_id", id, "high");
    FileInputSource source;
    FileInputBudget budget;
    Q4T_CHECK(Ok(Source(part, &source)) &&
              Ok(ReadFileImage(&objects, Access(), source, {}, {}, &budget,
                               &expected)) &&
              Ok(SaveFileImageSnapshot(snapshots, Access(), identity, expected,
                                       {}, {}, 60, &reference, &hold)) &&
              Ok(Files(objects).Erase(Access(), id)));
  }
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  FileImageInput restored;
  Q4T_CHECK(Ok(ReadFileImageSnapshot(snapshots, Access(), reference, identity,
                                     {}, {}, &restored)) &&
            restored.source.bytes == expected.source.bytes &&
            restored.detail == "high");
  FileImageLimits lowered;
  lowered.max_pixels = 2047;
  Q4T_CHECK(ReadFileImageSnapshot(snapshots, Access(), reference, identity,
                                  lowered, {}, &restored)
                    .http_status == 413 &&
            restored.source.bytes.empty());
  auto other = Access();
  other.scope = "client:other";
  Q4T_CHECK(ReadFileImageSnapshot(snapshots, other, reference, identity, {}, {},
                                  &restored)
                .http_status == 404);
  InputSnapshot raw;
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, identity, {}, &raw)));
  Set(&raw.metadata, "width", Number(65));
  const std::string_view bytes(raw.parts[0]);
  InputSnapshotRef bad;
  InputSnapshotHold bad_hold;
  Q4T_CHECK(Ok(snapshots.Save(Access(), identity, raw.metadata,
                              std::span(&bytes, 1), {}, 60, &bad, &bad_hold)));
  Q4T_CHECK(ReadFileImageSnapshot(snapshots, Access(), bad, identity, {}, {},
                                  &restored)
                    .code == "file_image_snapshot_inconsistent" &&
            restored.source.bytes.empty());
  Q4T_CHECK(Ok(snapshots.Release(Access(), &bad_hold)) &&
            Ok(snapshots.Release(Access(), &hold)));
  return true;
}

Q4T_TEST(
    file_image_url_policy_redirect_and_frozen_expansion_do_not_leak_or_refetch) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto server = std::make_unique<Server>();
  const auto part = Part("image_url", server->Url(), "original");
  FileInputSource source;
  Q4T_CHECK(Ok(Source(part, &source)));
  FileInputBudget budget;
  FileImageInput denied;
  auto blocked = server->Policy();
  blocked.allowed_networks.clear();
  Q4T_CHECK(
      !ReadFileImage(&objects, Access(), source, blocked, {}, &budget, &denied)
           .ok() &&
      server->requests == 0);
  FileExpansionOptions options;
  options.fetch = server->Policy();
  options.hold_seconds = 60;
  FileExpansion expansion(&objects, options);
  const auto items = Items(part);
  Q4T_CHECK(Ok(expansion.Prepare(items, FileInputProfile::kResponsesStored,
                                 Json(), Access())) &&
            Ok(expansion.Persist(Access())) && server->requests == 2);
  server.reset();
  FileExpansion restore(&objects, options);
  Json rendered;
  Q4T_CHECK(Ok(restore.Prepare(items, FileInputProfile::kResponsesStored,
                               expansion.Bindings(), Access(), true)) &&
            Ok(restore.Render(items, &rendered)));
  const auto& image = rendered.array[0].GetArray("content")->array[0];
  std::string decoded;
  const char* code = nullptr;
  Q4T_CHECK(image.GetString("detail") == "original" &&
            DecodeMediaUrl(image.GetString("image_url"), &decoded, &code) &&
            decoded == Bytes());
  const auto wire = OutputJson(rendered);
  Q4T_CHECK(wire.find("SECRET") == wire.npos &&
            wire.find("PRIVATE") == wire.npos &&
            wire.find("127.0.0.1") == wire.npos &&
            restore.Usage().images == 1 && restore.Usage().pixels == 2048);
  return true;
}

Q4T_TEST(
    file_image_shared_expansion_source_pixel_and_prevalidation_boundaries) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto id = Upload(objects, Bytes());
  auto items = Items(Part("file_id", id));
  auto second = items.array[0];
  Set(&second, "id", String("msg_other"));
  items.array.push_back(second);
  FileExpansionOptions options;
  options.extraction.pdf.max_pixels = 3000;
  FileExpansion total(&objects, options);
  Q4T_CHECK(
      total.Prepare(items, FileInputProfile::kResponsesStored, Json(), Access())
              .http_status == 413 &&
      total.Usage().images == 0);
  FileExpansionOptions ordinary;
  FileExpansion single(&objects, ordinary);
  Json rendered;
  const auto chat = Items(
      Object({{"type", String("image_url")},
              {"image_url",
               Object({{"url", String("data:image/png;base64,eA==")}})}}));
  Q4T_CHECK(
      single.Prepare(chat, FileInputProfile::kChat, Json(), Access()).code ==
      "invalid_file_image");
  FileExpansion validation(&objects, ordinary);
  auto bad = Part("image_url", "data:image/png,no-base64");
  auto invalid = Items(Part("file_id", "file_missing"));
  invalid.array.push_back(Items(bad).array[0]);
  Q4T_CHECK(validation
                .Prepare(invalid, FileInputProfile::kResponsesStored, Json(),
                         Access())
                .http_status == 400);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  FileInputSource source;
  const auto part = Part("file_id", id);
  FileInputBudget budget;
  FileImageInput image;
  Q4T_CHECK(Ok(Source(part, &source)) &&
            ReadFileImage(&objects, cancelled, source, {}, {}, &budget, &image)
                    .http_status == 499 &&
            image.source.bytes.empty());
  return true;
}
