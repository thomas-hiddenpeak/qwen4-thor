#include "q4t/server/file_video.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/file_video_protocol.h"
#include "../src/server/file_video_geometry.h"
#include "../src/server/file_video_orientation.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"
#include "q4t/vision/processor.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:video", Clock::now() + 5s, {}}; }
std::string Bytes(const char* name) {
  std::ifstream input(std::filesystem::path(__FILE__).parent_path() /
                          "fixtures/file_video" / name,
                      std::ios::binary);
  if (!input) throw std::runtime_error("video original fixture missing");
  return std::string(std::istreambuf_iterator<char>(input), {});
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
bool Clean(const GrammarControl& control, uint64_t expected = 1) {
  detail::PollRequestWorkers();
  return control.counters->started == expected &&
         control.counters->reaped == expected && control.counters->active == 0;
}
bool Actual(const char* filename) {
  const auto bytes = Bytes(filename);
  auto control = Control();
  FileVideoPixels output;
  FileVideoLimits limits;
  limits.sample_frames = 4;
  Q4T_CHECK(
      Ok(DecodeVideoContainer(bytes, limits, Access(), &output, control)));
  Q4T_CHECK(Clean(control) && output.width == 96 && output.height == 64 &&
            output.scanned_frames == 6 && output.frames.size() == 4 &&
            output.duration_ns == 3000000000ULL);
  const uint64_t indices[] = {0, 2, 4, 5};
  const int colors[][3] = {
      {255, 0, 0}, {0, 0, 255}, {0, 255, 255}, {255, 0, 255}};
  for (size_t i = 0; i < output.frames.size(); ++i) {
    const auto& frame = output.frames[i];
    Q4T_CHECK(frame.source_index == indices[i] &&
              frame.timestamp_ns == indices[i] * 500000000ULL &&
              frame.duration_ns == 500000000ULL);
    q4t::vision::ProcessedImage image;
    q4t::vision::ProcessorOptions options;
    options.image_detail = q4t::vision::ImageDetail::kOriginal;
    std::string error;
    Q4T_CHECK(q4t::vision::ProcessImage(
        reinterpret_cast<const uint8_t*>(frame.png.data()), frame.png.size(),
        {}, &image, &error, options));
    for (size_t patch = 0; patch < static_cast<size_t>(image.L()); ++patch)
      for (size_t c = 0; c < 3; ++c) {
        const float expected = colors[i][c] / 127.5f - 1.f;
        for (size_t pixel = 0; pixel < 512; ++pixel)
          Q4T_CHECK(
              std::abs(image.pixel_values[patch * 1536 + c * 512 + pixel] -
                       expected) < 0.07f);
      }
  }
  Q4T_CHECK(Ok(ValidateVideoPixels(bytes, limits, output)) &&
            output.conversion.GetString("source_sha256") == Digest(bytes));
  const auto* modules = output.conversion.GetArray("modules");
  Q4T_CHECK(modules && modules->array.size() >= 7);
  return true;
}
bool Display(const char* filename, uint32_t width, uint32_t height,
             const std::array<int, 4>& corners,
             std::string_view format = "q4t.cpu_video_png.v3") {
  const auto bytes = Bytes(filename);
  FileVideoPixels output;
  auto control = Control();
  FileVideoLimits limits;
  limits.sample_frames = 2;
  Q4T_CHECK(
      Ok(DecodeVideoContainer(bytes, limits, Access(), &output, control)));
  Q4T_CHECK(Clean(control) && output.width == width &&
            output.height == height && output.scanned_frames == 4 &&
            output.frames.size() == 2 && output.duration_ns == 2000000000ULL);
  Q4T_CHECK(output.conversion.GetString("format") == format &&
            Ok(ValidateVideoPixels(bytes, limits, output)));
  const int colors[][3] = {
      {255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 0}};
  for (size_t i = 0; i < output.frames.size(); ++i) {
    const auto& frame = output.frames[i];
    Q4T_CHECK(frame.source_index == i * 3 &&
              frame.timestamp_ns == i * 1500000000ULL &&
              frame.duration_ns == 500000000ULL);
    q4t::vision::ProcessedImage image;
    q4t::vision::ProcessorOptions options;
    options.image_detail = q4t::vision::ImageDetail::kOriginal;
    std::string error;
    Q4T_CHECK(q4t::vision::ProcessImage(
        reinterpret_cast<const uint8_t*>(frame.png.data()), frame.png.size(),
        {}, &image, &error, options));
    Q4T_CHECK(image.grid_w * 16 == int(width) &&
              image.grid_h * 16 == int(height));
    for (size_t corner = 0; corner < corners.size(); ++corner) {
      const auto px = (width * ((corner % 2) ? 3 : 1) / 4) / 16;
      const auto py = (height * ((corner / 2) ? 3 : 1) / 4) / 16;
      const auto patch =
          ((py / 2) * (image.grid_w / 2) + px / 2) * 4 + (py % 2) * 2 + px % 2;
      for (size_t channel = 0; channel < 3; ++channel) {
        const float expected = colors[corners[corner]][channel] / 127.5f - 1;
        const auto pixel = patch * 1536 + channel * 512 + 8 * 16 + 8;
        if (std::abs(image.pixel_values[pixel] - expected) >= 0.08f)
          std::fprintf(stderr, "%s corner=%zu channel=%zu got=%f want=%f\n",
                       filename, corner, channel, image.pixel_values[pixel],
                       expected);
        Q4T_CHECK(std::abs(image.pixel_values[pixel] - expected) < 0.08f);
      }
    }
  }
  return true;
}
}  // namespace

int VideoWorkerFixture(int argc, char** argv) {
  if (argc < 2) return -1;
  const bool sandbox = !std::strcmp(argv[1], "--video-sandbox-fixture");
  const bool hang = !std::strcmp(argv[1], "--video-hang-fixture");
  const bool crash = !std::strcmp(argv[1], "--video-crash-fixture");
  if (!sandbox && !hang && !crash) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  if (sandbox) {
    int listener = video_detail::EnterVideoSandbox();
    if (listener < 0 || !detail::SendWorkerIoListener(listener)) return 3;
    close(listener);
    uint64_t report[8]{};
    report[0] = open("/etc/hostname", O_RDONLY) < 0 && errno == EPERM;
    report[1] = socket(AF_INET, SOCK_STREAM, 0) < 0 && errno == EPERM;
    char missing[] = "/q4t-video-no-program";
    char* args[] = {missing, nullptr};
    char* env[] = {nullptr};
    report[2] = execve(missing, args, env) < 0 && errno == EPERM;
    report[3] = fork() < 0 && errno == EPERM;
    void* memory = mmap(nullptr, 4096, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    report[4] = memory != MAP_FAILED;
    if (memory != MAP_FAILED) {
      report[5] =
          mprotect(memory, 4096, PROT_READ | PROT_EXEC) < 0 && errno == EPERM;
      munmap(memory, 4096);
    }
    std::thread thread([&] {
      report[6] = 1;
      report[7] = open("/etc/hostname", O_RDONLY) < 0 && errno == EPERM;
    });
    thread.join();
    return detail::WorkerTransfer(report, sizeof(report), true) ? 0 : 3;
  }
  video_detail::Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.bytes > kFileInputBytes)
    return 3;
  std::string source(request.bytes, '\0');
  if (!detail::WorkerTransfer(source.data(), source.size(), false)) return 3;
  auto ready = video_detail::Ready::kReady;
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  int listener = video_detail::EnterVideoSandbox();
  if (listener < 0 || !detail::SendWorkerIoListener(listener)) return 3;
  close(listener);
  if (crash) _exit(17);
  while (true) std::this_thread::sleep_for(1ms);
}

Q4T_TEST(file_video_actual_h264_mp4_preserves_colors_order_and_pts) {
  return Actual("h264.mp4");
}
Q4T_TEST(file_video_actual_vp8_webm_preserves_colors_order_and_pts) {
  return Actual("vp8.webm");
}
Q4T_TEST(file_video_actual_vp9_webm_preserves_colors_order_and_pts) {
  return Actual("vp9.webm");
}
Q4T_TEST(file_video_actual_track_rotations_and_reflections_transform_pixels) {
  return Display("rotate-90.mp4", 64, 128, {2, 0, 3, 1}) &&
         Display("rotate-180.mp4", 128, 64, {3, 2, 1, 0}) &&
         Display("rotate-270.mp4", 64, 128, {1, 3, 0, 2}) &&
         Display("mirror-x.mp4", 128, 64, {1, 0, 3, 2}) &&
         Display("mirror-y.mp4", 128, 64, {2, 3, 0, 1}) &&
         Display("transpose.mp4", 64, 128, {0, 2, 1, 3}) &&
         Display("anti-transpose.mp4", 64, 128, {3, 1, 2, 0});
}
Q4T_TEST(file_video_actual_non_square_pixels_preserve_display_aspect) {
  return Display("wide-pixels.mp4", 256, 64, {0, 1, 2, 3}) &&
         Display("tall-pixels.mp4", 64, 64, {0, 1, 2, 3},
                 "q4t.cpu_video_png.v8") &&
         Display("wide-pixels.webm", 256, 64, {0, 1, 2, 3});
}
Q4T_TEST(file_video_tall_pixels_frozen_v3_keeps_original_grid) {
  // The original decoder expanded coded 128x64 to 128x128 for 1:2 PAR.
  // Stored v3 pixels and their source receipt must survive the grid change.
  FileVideoPixels stored;
  Q4T_CHECK(q4t::io::ParseJson(Bytes("tall-pixels-v3.json"),
                               &stored.conversion));
  stored.width = stored.height = 128;
  stored.duration_ns = 2000000000ULL;
  stored.scanned_frames = 4;
  const auto png = Bytes("tall-pixels-v3.png");
  for (uint64_t index : {0, 2, 3})
    stored.frames.push_back({png, index * 500000000, index, 500000000});
  const auto source = Bytes("tall-pixels.mp4");
  FileVideoLimits limits;
  limits.sample_frames = 4;
  Q4T_CHECK(Ok(ValidateVideoPixels(source, limits, stored)));
  stored.frames.back().png.back() ^= 1;
  Q4T_CHECK(!ValidateVideoPixels(source, limits, stored).ok());
  return true;
}
Q4T_TEST(file_video_actual_pixel_aspect_and_orientation_compose) {
  return Display("wide-rotate-90.mp4", 64, 256, {2, 0, 3, 1});
}
Q4T_TEST(file_video_display_geometry_rejects_overflow_and_expansion_budget) {
  using namespace video_detail;
  FileVideoLimits limits;
  DisplayGeometry shape;
  Q4T_CHECK(SquarePixelGeometry(128, 64, 0, 1, limits, &shape) ==
            Failure::kInvalid);
  Q4T_CHECK(SquarePixelGeometry(128, 64, 1, -1, limits, &shape) ==
            Failure::kInvalid);
  Q4T_CHECK(SquarePixelGeometry(128, 64, 2147483648LL, 1, limits, &shape) ==
            Failure::kInvalid);
  Q4T_CHECK(SquarePixelGeometry(128, 64, 2147483647, 1, limits, &shape) ==
            Failure::kCapacity);
  Q4T_CHECK(SquarePixelGeometry(128, 64, 1, 2147483647, limits, &shape) ==
            Failure::kCapacity);
  Q4T_CHECK(SquarePixelGeometry(3, 2, 3, 2, limits, &shape) == Failure::kNone &&
            shape.square_width == 5 && shape.square_height == 2);
  limits.max_frame_pixels = 128 * 64;
  auto control = Control();
  FileVideoPixels output;
  const auto reply = DecodeVideoContainer(Bytes("wide-pixels.mp4"), limits,
                                          Access(), &output, control);
  const bool clean = Clean(control);
  if (reply.http_status != 413 || !output.frames.empty() || !clean)
    std::fprintf(
        stderr, "geometry budget status=%d code=%s frames=%zu clean=%d\n",
        reply.http_status, reply.code.c_str(), output.frames.size(), clean);
  Q4T_CHECK(reply.http_status == 413 && output.frames.empty() && clean);
  return true;
}
Q4T_TEST(file_video_display_metadata_is_validated_before_frozen_replay) {
  const auto bytes = Bytes("wide-rotate-90.mp4");
  FileVideoPixels output;
  Q4T_CHECK(Ok(DecodeVideoContainer(bytes, {}, Access(), &output)));
  for (int mode = 0; mode < 5; ++mode) {
    auto changed = output;
    auto& geometry = Field(changed.conversion, "display_geometry");
    if (mode == 0) Set(&geometry, "pixel_aspect_num", Number(1));
    if (mode == 1) Set(&geometry, "pixel_aspect_den", Number(0));
    if (mode == 2) Set(&geometry, "coded_width", Number(0.5));
    if (mode == 3) Set(&geometry, "policy", String("ignored"));
    if (mode == 4) Set(&geometry, "coded_height", Number(8193));
    Q4T_CHECK(!ValidateVideoPixels(bytes, {}, changed).ok());
  }
  return true;
}
Q4T_TEST(file_video_alternate_track_selection_keeps_pixels_and_timeline) {
  const auto input = Bytes("alternate-tracks.mp4");
  const auto first = input.find("tkhd");
  const auto second = input.find("tkhd", first + 4);
  Q4T_CHECK(first != input.npos && second != input.npos &&
            input.find("tkhd", second + 4) == input.npos);
  const FileVideoLimits limits;
  for (unsigned selection = 0; selection < 4; ++selection) {
    auto movie = input;
    for (const auto& [offset, mask] :
         {std::pair{first, 1u}, std::pair{second, 2u}}) {
      auto& flags = movie[offset + 7];
      flags =
          (static_cast<unsigned char>(flags) & ~1u) | ((selection & mask) != 0);
    }
    auto control = Control();
    FileVideoPixels actual;
    const auto reply =
        DecodeVideoContainer(movie, limits, Access(), &actual, control);
    Q4T_CHECK(Clean(control));
    if (selection == 0) {
      Q4T_CHECK(reply.http_status == 400);
      continue;
    }
    Q4T_CHECK(Ok(reply) && Ok(ValidateVideoPixels(movie, limits, actual)));
    if (selection == 3) {
      Q4T_CHECK(actual.conversion.GetString("format") ==
                "q4t.cpu_video_png.v9");
      const auto* layers =
          actual.conversion.Find("display_geometry")->GetArray("layers");
      Q4T_CHECK(layers && layers->array.size() == 2 &&
                actual.duration_ns == 3000000000ULL);
      continue;
    }
    const auto reference = Bytes(selection == 1 ? "h264.mp4" : "rotate-90.mp4");
    FileVideoPixels expected;
    auto baseline = Control();
    Q4T_CHECK(Ok(DecodeVideoContainer(reference, limits, Access(), &expected,
                                      baseline)) &&
              Clean(baseline));
    Q4T_CHECK(actual.width == expected.width &&
              actual.height == expected.height &&
              actual.duration_ns == expected.duration_ns &&
              actual.scanned_frames == expected.scanned_frames &&
              actual.frames.size() == expected.frames.size());
    for (size_t i = 0; i < actual.frames.size(); ++i) {
      const auto& a = actual.frames[i];
      const auto& b = expected.frames[i];
      Q4T_CHECK(a.png == b.png && a.timestamp_ns == b.timestamp_ns &&
                a.source_index == b.source_index &&
                a.duration_ns == b.duration_ns);
    }
  }
  return true;
}
Q4T_TEST(file_video_layered_track_budget_binding_and_alternates) {
  auto movie = Bytes("alternate-tracks.mp4");
  const auto first = movie.find("tkhd"), second = movie.find("tkhd", first + 4);
  Q4T_CHECK(first != movie.npos && second != movie.npos);
  movie[second + 7] |= 1;
  FileVideoLimits limits;
  limits.sample_frames = 4;
  FileVideoPixels actual;
  Q4T_CHECK(Ok(DecodeVideoContainer(movie, limits, Access(), &actual)));
  const auto work = actual.Work();
  Q4T_CHECK(work.scanned_frames == 20 &&
            work.decoded_pixels >
                actual.scanned_frames * actual.width * actual.height);
  limits.max_scanned_frames = work.scanned_frames;
  limits.max_decoded_pixels = work.decoded_pixels;
  Q4T_CHECK(Ok(ValidateVideoSnapshot(Digest(movie), limits, actual)));
  FileVideoPixels repeated;
  Q4T_CHECK(Ok(DecodeVideoContainer(movie, limits, Access(), &repeated)));
  for (bool frame_limit : {false, true}) {
    auto reduced = limits;
    if (frame_limit)
      --reduced.max_scanned_frames;
    else
      --reduced.max_decoded_pixels;
    FileVideoPixels empty;
    Q4T_CHECK(ValidateVideoPixels(movie, reduced, actual).http_status == 413);
    Q4T_CHECK(
        DecodeVideoContainer(movie, reduced, Access(), &empty).http_status ==
            413 &&
        empty.frames.empty());
  }
  for (int mode = 0; mode < 6; ++mode) {
    auto changed = actual;
    auto& geometry = Field(changed.conversion, "display_geometry");
    auto& layers = Field(geometry, "layers").array;
    auto& source = Field(layers.front(), "source");
    if (mode == 0)
      Set(&source, "track_sha256", String(Digest("another track")));
    if (mode == 1)
      Set(&geometry, "decoded_frames", Number(work.scanned_frames - 1));
    if (mode == 2)
      Set(&geometry, "decoded_pixels", Number(work.decoded_pixels - 1));
    if (mode == 3)
      Field(layers.front(), "samples").array.front().array[1] = Number(1);
    if (mode == 4) Set(&source, "track_id", Number(99));
    if (mode == 5) std::swap(layers.front(), layers.back());
    Q4T_CHECK(!ValidateVideoPixels(movie, limits, changed).ok());
  }
  // Members of one nonzero alternate group are alternatives, not overlays.
  movie[first + 4 + 35] = movie[second + 4 + 35] = 1;
  FileVideoPixels selected;
  Q4T_CHECK(Ok(DecodeVideoContainer(movie, {}, Access(), &selected)));
  const auto& alternatives =
      Field(Field(selected.conversion, "display_geometry"), "layers").array;
  Q4T_CHECK(alternatives.size() == 1 &&
            alternatives[0].Find("source")->GetInt("track_id") == 1);
  movie[second + 7] &= ~1;
  FileVideoPixels single;
  Q4T_CHECK(Ok(DecodeVideoContainer(movie, {}, Access(), &single)) &&
            selected.frames.size() == single.frames.size());
  for (size_t i = 0; i < single.frames.size(); ++i)
    Q4T_CHECK(selected.frames[i].png == single.frames[i].png &&
              selected.frames[i].timestamp_ns == single.frames[i].timestamp_ns);
  movie[second + 7] |= 1;
  Set(&single.conversion, "source_sha256", String(Digest(movie)));
  Q4T_CHECK(!ValidateVideoPixels(movie, {}, single).ok());
  return true;
}
Q4T_TEST(file_video_display_matrix_bounds_versions_and_composition) {
  using namespace video_detail;
  Q4T_CHECK(!MovieDisplay().Read(Bytes("vp8.webm")));
  auto original = Bytes("rotate-90.mp4");
  const auto movie = original.find("mvhd"), track = original.find("tkhd");
  Q4T_CHECK(movie != original.npos && track != original.npos);
  // Both fixture headers are v0; applying movie rotation after track rotation
  // composes two quarter turns. No codec or GPU is needed for this boundary.
  auto combined = original;
  combined.replace(movie + 4 + 36, 36, original.substr(track + 4 + 40, 36));
  Q4T_CHECK(MovieDisplay().Read(combined) == DisplayDirection::kHalf);
  for (int mode = 0; mode < 4; ++mode) {
    auto bad = original;
    if (mode == 0) bad[track + 4] = 2;           // Unknown version.
    if (mode == 1) bad[track + 4 + 40 + 8] = 1;  // Perspective component.
    if (mode == 2) bad[track + 4 + 40 + 2] = 1;  // Non-unit transform.
    if (mode == 3) bad.resize(track + 12);       // Truncated nested atom.
    bool rejected = false;
    try {
      (void)MovieDisplay().Read(bad);
    } catch (const std::runtime_error&) {
      rejected = true;
    }
    Q4T_CHECK(rejected);
  }
  return true;
}
Q4T_TEST(file_video_sandbox_permits_threads_without_external_capabilities) {
  auto control = Control();
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> worker;
  Q4T_CHECK(detail::GrammarProcess::Start("--video-sandbox-fixture", control,
                                          &worker));
  Q4T_CHECK(worker->ReceiveIoBudget({.write_bytes = 0,
                                     .file_creations = 0,
                                     .thread_creations = 1,
                                     .notifications = 4},
                                    Clock::now() + 2s));
  uint64_t report[8]{};
  Q4T_CHECK(worker->Transfer(report, sizeof(report), false, Clock::now() + 2s));
  for (auto passed : report) Q4T_CHECK(passed == 1);
  std::string trailing;
  Q4T_CHECK(worker->CollectOutput(&trailing, 0, Clock::now() + 2s));
  worker.reset();
  Q4T_CHECK(Clean(control));
  return true;
}
Q4T_TEST(file_video_preflight_rejects_bad_scope_source_cancel_and_deadline) {
  const auto source = Bytes("vp8.webm");
  FileVideoPixels output;
  auto control = Control();
  Q4T_CHECK(DecodeVideoContainer("not video", {}, Access(), &output, control)
                .http_status == 400);
  auto limits = FileVideoLimits{};
  limits.max_source_bytes = source.size() - 1;
  Q4T_CHECK(DecodeVideoContainer(source, limits, Access(), &output, control)
                .http_status == 413);
  limits.sample_frames = 1;
  Q4T_CHECK(DecodeVideoContainer(source, limits, Access(), &output, control)
                .http_status == 500);
  auto access = Access();
  access.scope.clear();
  Q4T_CHECK(DecodeVideoContainer(source, {}, access, &output, control).code ==
            "object_scope_unavailable");
  access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(
      DecodeVideoContainer(source, {}, access, &output, control).http_status ==
      499);
  access = Access();
  access.deadline = Clock::now() - 1ms;
  Q4T_CHECK(
      DecodeVideoContainer(source, {}, access, &output, control).http_status ==
      408);
  Q4T_CHECK(output.frames.empty() && Clean(control, 0));
  return true;
}
Q4T_TEST(file_video_actual_limits_reject_without_publishing_partial_frames) {
  const auto source = Bytes("vp8.webm");
  std::vector<FileVideoLimits> limits(5);
  limits[0].max_edge = 95;
  limits[1].max_duration_ns = 2999999999;
  limits[2].max_scanned_frames = 5;
  limits[3].max_decoded_pixels = 96 * 64 * 6 - 1;
  limits[4].max_output_bytes = 1024;
  for (const auto& limit : limits) {
    auto control = Control();
    FileVideoPixels output;
    const auto reply =
        DecodeVideoContainer(source, limit, Access(), &output, control);
    if (reply.http_status != 413) Ok(reply);
    Q4T_CHECK(reply.http_status == 413 && output.frames.empty() &&
              Clean(control));
  }
  return true;
}
Q4T_TEST(file_video_corrupt_container_is_request_local_and_reaped) {
  for (const auto* name : {"vp8.webm", "h264.mp4"}) {
    auto source = Bytes(name);
    source.resize(16);
    auto control = Control();
    FileVideoPixels output;
    const auto reply =
        DecodeVideoContainer(source, {}, Access(), &output, control);
    if (reply.http_status != 400) Ok(reply);
    Q4T_CHECK(reply.http_status == 400 && output.frames.empty() &&
              Clean(control, 0));
  }
  return true;
}
Q4T_TEST(file_video_missing_codec_keeps_native_text_and_worker_slots_healthy) {
  ScopeExit reset([] { detail::SetVideoWorkerEntryForTesting(""); });
  detail::SetVideoWorkerEntryForTesting("--file-video-missing-worker");
  auto control = Control();
  FileVideoPixels output;
  Q4T_CHECK(
      DecodeVideoContainer(Bytes("vp8.webm"), {}, Access(), &output, control)
              .code == "file_video_dependency_unavailable" &&
      output.frames.empty() && Clean(control));
  control = Control();
  FileInputContent input;
  input.bytes = "healthy text after missing codec";
  input.filename = "text.txt";
  FileDocument text;
  Q4T_CHECK(ParseNativeFileDocument(input, {}, Access(), &text, control).ok() &&
            text.text == input.bytes && Clean(control));
  return true;
}
Q4T_TEST(file_video_hang_crash_and_inflight_cancel_have_bounded_cleanup) {
  ScopeExit reset([] { detail::SetVideoWorkerEntryForTesting(""); });
  const auto source = Bytes("vp8.webm");
  for (int mode = 0; mode < 3; ++mode) {
    detail::SetVideoWorkerEntryForTesting(mode == 1 ? "--video-crash-fixture"
                                                    : "--video-hang-fixture");
    auto control = Control();
    auto access = Access();
    bool ready = false;
    if (mode == 0) access.deadline = Clock::now() + 100ms;
    if (mode == 2) {
      control.worker_ready = [&] { ready = true; };
      access.cancelled = [&] { return ready; };
    }
    FileVideoPixels output;
    const auto started = Clock::now();
    const auto reply =
        DecodeVideoContainer(source, {}, access, &output, control);
    Q4T_CHECK(reply.http_status == (mode == 0   ? 408
                                    : mode == 1 ? 503
                                                : 499) &&
              Clock::now() - started < 2s && output.frames.empty() &&
              Clean(control));
  }
  return true;
}
Q4T_TEST(file_video_unreaped_worker_retains_shared_document_capacity) {
  auto control = Control();
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> worker;
  Q4T_CHECK(detail::GrammarProcess::Start("--grammar-fixture-silent", control,
                                          &worker));
  detail::DeferGrammarReapingForTesting(true);
  ScopeExit reset([&] {
    detail::DeferGrammarReapingForTesting(false);
    const auto until = Clock::now() + 300ms;
    while (control.counters->reaped == 0 && Clock::now() < until) {
      detail::PollRequestWorkers();
      std::this_thread::sleep_for(1ms);
    }
  });
  worker.reset();
  FileVideoPixels output;
  Q4T_CHECK(
      DecodeVideoContainer(Bytes("vp8.webm"), {}, Access(), &output).code ==
          "file_document_worker_capacity" &&
      control.counters->started == 1 && control.counters->active == 1);
  return true;
}
Q4T_TEST(file_video_frozen_metadata_rejects_source_pts_and_frame_tampering) {
  const auto source = Bytes("vp8.webm");
  FileVideoPixels original;
  Q4T_CHECK(Ok(DecodeVideoContainer(source, {}, Access(), &original)));
  auto changed = original;
  changed.frames[1].timestamp_ns = changed.frames[0].timestamp_ns;
  Q4T_CHECK(!ValidateVideoPixels(source, {}, changed).ok());
  changed = original;
  changed.frames[0].png.back() ^= 1;
  Q4T_CHECK(!ValidateVideoPixels(source, {}, changed).ok());
  changed = original;
  Set(&changed.conversion, "width", Number(1));
  Q4T_CHECK(!ValidateVideoPixels(source, {}, changed).ok());
  Q4T_CHECK(!ValidateVideoPixels(source + "changed", {}, original).ok());
  changed = original;
  changed.frames.back().duration_ns = 1;
  Q4T_CHECK(!ValidateVideoPixels(source, {}, changed).ok());
  Set(&Field(changed.conversion, "frames").array.back(), "duration_ns",
      Number(1000000000));
  changed.frames.back().duration_ns = 1000000000;
  Q4T_CHECK(!ValidateVideoPixels(source, {}, changed).ok());
  // Older frozen inputs have no duration; an unknown duration must not be
  // silently extended to the end of the whole container.
  changed = original;
  Set(&changed.conversion, "format", String("q4t.cpu_video_png.v1"));
  std::erase_if(changed.conversion.object, [](const auto& entry) {
    return entry.first == "display_geometry";
  });
  for (auto& frame : changed.frames) frame.duration_ns = 0;
  for (auto& frame : Field(changed.conversion, "frames").array)
    std::erase_if(frame.object,
                  [](const auto& field) { return field.first == "duration_ns"; });
  Q4T_CHECK(Ok(ValidateVideoPixels(source, {}, changed)));
  changed.frames.back().duration_ns = 500000000;
  Q4T_CHECK(!ValidateVideoPixels(source, {}, changed).ok());
  return true;
}
Q4T_TEST(file_video_thread_budget_exhaustion_is_bounded_and_request_local) {
  auto control = Control();
  FileVideoLimits limits;
  limits.max_thread_creations = 0;
  FileVideoPixels output;
  Q4T_CHECK(DecodeVideoContainer(Bytes("vp8.webm"), limits, Access(), &output,
                                 control)
                    .http_status == 413 &&
            output.frames.empty() && Clean(control));
  return true;
}

Q4T_TEST(file_video_unknown_duration_does_not_use_container_end) {
  const auto source = Bytes("vp9.webm");
  FileVideoPixels output;
  Q4T_CHECK(Ok(DecodeVideoContainer(source, {}, Access(), &output)));
  auto& last = output.frames.back();
  auto& metadata = Field(output.conversion, "frames").array.back();
  last.duration_ns = 0;
  Set(&metadata, "duration_ns", Number(0));
  Q4T_CHECK(Ok(ValidateVideoPixels(source, {}, output)) &&
            last.timestamp_ns == 2500000000ULL &&
            output.duration_ns == 3000000000ULL && !last.duration_ns);
  auto fractional = Number(0);
  fractional.number = 0.5;
  fractional.str = "0.5";
  Set(&metadata, "duration_ns", fractional);
  Q4T_CHECK(!ValidateVideoPixels(source, {}, output).ok());
  return true;
}
