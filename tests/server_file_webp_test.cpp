#include "q4t/server/file_webp.h"

#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/file_webp_protocol.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/chat_prompt.h"
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
BinaryAccess Access() { return {"client:webp", Clock::now() + 10s, {}}; }
std::string Bytes(const char* name) {
  std::ifstream in(
      fs::path(__FILE__).parent_path() / "fixtures/file_images/webp" / name,
      std::ios::binary);
  if (!in) throw std::runtime_error("webp original fixture");
  return std::string(std::istreambuf_iterator<char>(in), {});
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
GrammarControl Control() {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  return control;
}
bool Clean(const GrammarControl& control, uint64_t count = 1) {
  detail::PollRequestWorkers();
  return control.counters->started == count &&
         control.counters->reaped == count && control.counters->active == 0;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = fs::absolute(out ? out : ".q4t-work/webp-cpu");
    fs::create_directories(root);
    auto path = (root / "webp-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("webp fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
std::string Upload(ObjectStore& objects, const std::string& bytes) {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=webp");
  const std::string start =
      "--webp\r\nContent-Disposition: form-data; "
      "name=\"purpose\"\r\n\r\nvision\r\n"
      "--webp\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"original.webp\"\r\n"
      "Content-Type: image/webp\r\n\r\n";
  auto result = upload.Feed(start, Access().deadline);
  if (result.ok()) result = upload.Feed(bytes, Access().deadline);
  if (result.ok()) result = upload.Feed("\r\n--webp--\r\n", Access().deadline);
  if (result.ok()) result = upload.Finish();
  if (!result.ok()) throw std::runtime_error(result.message);
  return result.value.GetString("id");
}
Json Items(const std::string& id) {
  return Array({Object(
      {{"id", String("msg_webp")},
       {"type", String("message")},
       {"role", String("user")},
       {"content", Array({Object({{"type", String("input_image")},
                                  {"file_id", String(id)},
                                  {"detail", String("original")}})})}})});
}
void Little(uint32_t value, size_t offset, std::string* bytes) {
  for (size_t i = 0; i < 4; ++i)
    (*bytes)[offset + i] = static_cast<char>(value >> (i * 8));
}
bool Pixels(const std::string& image, q4t::vision::ProcessedImage* output) {
  std::string error;
  q4t::vision::ProcessorOptions options;
  options.image_detail = q4t::vision::ImageDetail::kOriginal;
  return q4t::vision::ProcessImage(
      reinterpret_cast<const uint8_t*>(image.data()), image.size(), {}, output,
      &error, options);
}
}  // namespace

int WebpWorkerFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const bool hang = !std::strcmp(argv[1], "--webp-hang-fixture");
  const bool crash = !std::strcmp(argv[1], "--webp-crash-fixture");
  if (!hang && !crash) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  webp_detail::Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.bytes > kFileInputBytes)
    return 3;
  std::string bytes(request.bytes, '\0');
  if (!detail::WorkerTransfer(bytes.data(), bytes.size(), false)) return 3;
  auto ready = webp_detail::Ready::kReady;
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  if (crash) _exit(17);
  while (true) std::this_thread::sleep_for(1ms);
}

Q4T_TEST(file_webp_container_validates_actual_formats_and_complete_lengths) {
  for (const auto* name : {"lossless.webp", "lossy.webp", "alpha.webp",
                           "metadata.webp", "noise.webp"}) {
    const auto bytes = Bytes(name);
    WebpHeader header;
    Q4T_CHECK(Ok(ReadWebpHeader(bytes, &header)) && header.height == 64);
    for (size_t size = 0; size < bytes.size(); ++size) {
      Q4T_CHECK(
          !ReadWebpHeader(std::string_view(bytes).substr(0, size), &header)
               .ok());
      Q4T_CHECK(header.width == 0 && header.height == 0);
    }
    Q4T_CHECK(!ReadWebpHeader(bytes + "extra", &header).ok());
  }
  WebpHeader header;
  Q4T_CHECK(ReadWebpHeader(Bytes("animated.webp"), &header).code ==
            "media_animation_unsupported");
  auto bytes = Bytes("lossless.webp");
  Little(0xffffffff, 16, &bytes);
  Q4T_CHECK(!ReadWebpHeader(bytes, &header).ok());
  auto control = Control();
  FileWebpPixels output;
  FileWebpLimits limit;
  limit.max_pixels = 96 * 64 - 1;
  Q4T_CHECK(
      DecodeWebpImage(Bytes("lossy.webp"), limit, Access(), &output, control)
          .http_status == 413);
  Q4T_CHECK(output.png.empty() && Clean(control, 0));
  // A syntactically complete RIFF with only a lossless header has no pixels.
  // Header admission must not be mistaken for successful decompression.
  auto compressed = Bytes("lossless.webp");
  Q4T_CHECK(compressed.substr(12, 4) == "VP8L");
  compressed.resize(26);
  Little(18, 4, &compressed);
  Little(5, 16, &compressed);
  compressed[25] = 0;
  Q4T_CHECK(Ok(ReadWebpHeader(compressed, &header)));
  control = Control();
  Q4T_CHECK(
      DecodeWebpImage(compressed, {}, Access(), &output, control).http_status ==
          400 &&
      Clean(control));
  return true;
}

Q4T_TEST(file_webp_actual_owned_decode_matches_pillow_and_codec_identity) {
  for (const auto* name : {"lossless", "lossy", "alpha", "metadata", "noise"}) {
    const auto bytes = Bytes((std::string(name) + ".webp").c_str());
    auto control = Control();
    FileWebpPixels output;
    Q4T_CHECK(Ok(DecodeWebpImage(bytes, {}, Access(), &output, control)) &&
              Clean(control));
    q4t::vision::ProcessedImage actual, reference;
    Q4T_CHECK(Pixels(output.png, &actual) &&
              Pixels(Bytes((std::string(name) + ".png").c_str()), &reference));
    Q4T_CHECK(actual.grid_h == reference.grid_h &&
              actual.grid_w == reference.grid_w &&
              actual.pixel_values == reference.pixel_values);
    Q4T_CHECK(output.conversion.GetString("source_sha256") == Digest(bytes));
    q4t::trace::Digest decoder, encoder;
    Q4T_CHECK(q4t::trace::HashFile(Q4T_SYSTEM_WEBP_LIBRARY, &decoder) &&
              q4t::trace::HashFile(Q4T_SYSTEM_ZLIB_LIBRARY, &encoder));
    Q4T_CHECK(output.conversion.Find("decoder")->GetString("sha256") ==
                  q4t::trace::HexDigest(decoder) &&
              output.conversion.Find("encoder")->GetString("sha256") ==
                  q4t::trace::HexDigest(encoder));
    WebpHeader header;
    Set(&output.conversion, "width", Number(1));
    Q4T_CHECK(!ValidateWebpPixels(bytes, {}, output, &header).ok() &&
              header.width == 0);
  }
  return true;
}

Q4T_TEST(file_webp_budget_cancel_timeout_and_worker_failure_release_ownership) {
  const auto source = Bytes("noise.webp");
  FileWebpPixels output;
  auto control = Control();
  FileWebpLimits limit;
  limit.max_output_bytes = 1024;
  Q4T_CHECK(
      DecodeWebpImage(source, limit, Access(), &output, control).http_status ==
          413 &&
      Clean(control));
  Q4T_CHECK(output.png.empty());
  auto access = Access();
  access.cancelled = [] { return true; };
  control = Control();
  Q4T_CHECK(DecodeWebpImage(source, {}, access, &output, control).http_status ==
                499 &&
            Clean(control, 0));
  control = Control();
  bool cancel = false;
  control.cancelled = [&] { return cancel; };
  control.worker_ready = [&] { cancel = true; };
  Q4T_CHECK(
      DecodeWebpImage(source, {}, Access(), &output, control).http_status ==
          499 &&
      Clean(control));
  ScopeExit reset([] { detail::SetWebpWorkerEntryForTesting({}); });
  for (const auto* entry : {"--webp-hang-fixture", "--webp-crash-fixture"}) {
    detail::SetWebpWorkerEntryForTesting(entry);
    limit = {};
    limit.timeout_ms = 250;
    control = Control();
    const auto reply =
        DecodeWebpImage(source, limit, Access(), &output, control);
    Q4T_CHECK(reply.http_status == (std::strstr(entry, "hang") ? 408 : 503));
    Q4T_CHECK(output.png.empty() && Clean(control));
  }
  return true;
}

Q4T_TEST(file_webp_missing_optional_codec_preserves_builtin_pixels_and_text) {
  detail::SetWebpWorkerEntryForTesting("--file-webp-missing-worker");
  ScopeExit reset([] { detail::SetWebpWorkerEntryForTesting({}); });
  auto control = Control();
  FileWebpPixels output;
  Q4T_CHECK(DecodeWebpImage(Bytes("lossy.webp"), {}, Access(), &output, control)
                    .code == "file_image_dependency_unavailable" &&
            Clean(control));
  Q4T_CHECK(output.png.empty());
  q4t::vision::ProcessedImage image;
  Q4T_CHECK(Pixels(Bytes("lossless.png"), &image));
  const auto request = Object(
      {{"messages", Array({Object({{"role", String("user")},
                                   {"content", String("ordinary text")}})})}});
  std::string prompt, error;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(request, {}, &prompt, &media, &error) &&
            media.empty() && prompt.find("ordinary text") != prompt.npos);
  return true;
}

Q4T_TEST(
    file_webp_frozen_snapshot_keeps_original_and_decoded_pixels_without_codec) {
  Fixture fixture;
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  const auto identity = Digest("webp part");
  FileImageInput expected;
  {
    ObjectStore objects(fixture.options);
    const auto raw = Bytes("alpha.webp");
    const auto id = Upload(objects, raw);
    const auto part = Items(id).array[0].Find("content")->array[0];
    FileInputSource source;
    FileInputBudget budget;
    Q4T_CHECK(Ok(ParseImageInputSource(part, FileInputProfile::kResponsesStored,
                                       "input[0]", &source)));
    Q4T_CHECK(Ok(ReadFileImage(&objects, Access(), source, {}, {}, &budget,
                               &expected)) &&
              expected.webp);
    InputSnapshotStore snapshots(objects);
    Q4T_CHECK(Ok(SaveFileImageSnapshot(snapshots, Access(), identity, expected,
                                       {}, {}, 60, &reference, &hold)));
    Q4T_CHECK(Ok(Files(objects).Erase(Access(), id)));
  }
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  detail::SetWebpWorkerEntryForTesting("--file-webp-missing-worker");
  ScopeExit reset([] { detail::SetWebpWorkerEntryForTesting({}); });
  FileImageInput actual;
  Q4T_CHECK(Ok(ReadFileImageSnapshot(snapshots, Access(), reference, identity,
                                     {}, {}, &actual)) &&
            actual.webp);
  Q4T_CHECK(actual.source.bytes == expected.source.bytes &&
            actual.webp->png == expected.webp->png &&
            OutputJson(actual.webp->conversion) ==
                OutputJson(expected.webp->conversion));
  q4t::vision::ProcessedImage pixels, expected_pixels;
  Q4T_CHECK(Pixels(actual.webp->png, &pixels) &&
            Pixels(Bytes("alpha.png"), &expected_pixels) &&
            pixels.pixel_values == expected_pixels.pixel_values);
  FileImageLimits lower;
  lower.max_conversion_bytes = 1024;
  Q4T_CHECK(ReadFileImageSnapshot(snapshots, Access(), reference, identity,
                                  lower, {}, &actual)
                .http_status == 413);
  Q4T_CHECK(actual.source.bytes.empty() && !actual.webp);
  auto other = Access();
  other.scope = "client:foreign";
  Q4T_CHECK(ReadFileImageSnapshot(snapshots, other, reference, identity, {}, {},
                                  &actual)
                .http_status == 404);
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  return true;
}

Q4T_TEST(
    file_webp_shared_expansion_charges_both_representations_and_frozen_metadata) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto source = Bytes("lossless.webp");
  const auto id = Upload(objects, source);
  const auto items = Items(id);
  FileExpansion preparation(&objects, {});
  Q4T_CHECK(Ok(preparation.Prepare(items, FileInputProfile::kResponsesStored,
                                   {}, Access())) &&
            Ok(preparation.Persist(Access())));
  Json expanded;
  Q4T_CHECK(Ok(preparation.Render(items, &expanded)));
  const auto& image = expanded.array[0].Find("content")->array[0];
  Q4T_CHECK(image.GetString("image_url").starts_with("data:image/png;base64,"));
  std::string png;
  const char* code;
  Q4T_CHECK(DecodeMediaUrl(image.GetString("image_url"), &png, &code));
  Q4T_CHECK(preparation.Usage().source_bytes == source.size() &&
            preparation.Usage().image_bytes == source.size() + png.size());
  detail::SetWebpWorkerEntryForTesting("--file-webp-missing-worker");
  ScopeExit reset([] { detail::SetWebpWorkerEntryForTesting({}); });
  Q4T_CHECK(Ok(Files(objects).Erase(Access(), id)));
  FileExpansionOptions limits;
  limits.extraction.pdf.max_image_bytes = preparation.Usage().image_bytes - 1;
  FileExpansion too_large(&objects, limits);
  Q4T_CHECK(too_large
                .Prepare(items, FileInputProfile::kResponsesStored,
                         preparation.Bindings(), Access(), true)
                .http_status == 413);
  FileExpansion replay(&objects, {});
  Q4T_CHECK(Ok(replay.Prepare(items, FileInputProfile::kResponsesStored,
                              preparation.Bindings(), Access(), true)));
  Json restored;
  Q4T_CHECK(Ok(replay.Render(items, &restored)) &&
            OutputJson(restored) == OutputJson(expanded));
  const auto& binding = preparation.Bindings().array[0];
  InputSnapshotRef reference;
  Q4T_CHECK(ParseInputSnapshotReference(*binding.Find("snapshot"), &reference));
  InputSnapshotStore snapshots(objects);
  InputSnapshot raw;
  const auto digest = binding.GetString("binding_sha256");
  Q4T_CHECK(Ok(snapshots.Read(Access(), reference, digest, {}, &raw)) &&
            raw.parts.size() == 2);
  auto conversion = *raw.metadata.Find("conversion");
  Set(&conversion, "output_sha256", String(std::string(64, '0')));
  Set(&raw.metadata, "conversion", conversion);
  const std::string_view parts[]{raw.parts[0], raw.parts[1]};
  InputSnapshotRef corrupt;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(snapshots.Save(Access(), digest, raw.metadata, parts, {}, 60,
                              &corrupt, &hold)));
  FileImageInput bad;
  Q4T_CHECK(
      ReadFileImageSnapshot(snapshots, Access(), corrupt, digest, {}, {}, &bad)
          .code == "file_image_snapshot_inconsistent");
  Q4T_CHECK(bad.source.bytes.empty() && !bad.webp);
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)) &&
            Ok(preparation.Release(Access())));
  return true;
}
