#include "q4t/test.h"
#include "q4t/vision/timing.h"

#include <stdexcept>
#include <string>

namespace {
using q4t::vision::StageTimings;
using q4t::vision::TimingScope;
using q4t::vision::TimingStage;

std::string Read(FILE* file) {
  std::rewind(file);
  std::string result;
  char buffer[2048];
  while (std::fgets(buffer, sizeof(buffer), file)) result += buffer;
  return result;
}
}  // namespace

Q4T_TEST(vision_timing_disabled_is_silent) {
  StageTimings timing(false);
  Q4T_CHECK(timing.Active() == nullptr);
  { TimingScope scope(timing.Active(), TimingStage::kForward); }
  Q4T_CHECK(timing.Calls(TimingStage::kForward) == 0);
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  timing.Report(file, "disabled", true, 1, 1, 64);
  const auto output = Read(file);
  std::fclose(file);
  Q4T_CHECK(output.empty());
  return true;
}

Q4T_TEST(vision_timing_accumulates_without_mixing_codecs) {
  StageTimings timing(true);
  timing.Observe(TimingStage::kDecodeJpeg, std::chrono::microseconds(100));
  timing.Observe(TimingStage::kDecodeJpeg, std::chrono::microseconds(250));
  timing.Observe(TimingStage::kDecodePng, std::chrono::milliseconds(2));
  Q4T_CHECK(timing.Milliseconds(TimingStage::kDecodeJpeg) == .35);
  Q4T_CHECK(timing.Calls(TimingStage::kDecodeJpeg) == 2);
  Q4T_CHECK(timing.Milliseconds(TimingStage::kDecodePng) == 2);
  Q4T_CHECK(timing.Calls(TimingStage::kDecodePng) == 1);
  Q4T_CHECK(timing.Calls(TimingStage::kForward) == 0);
  return true;
}

Q4T_TEST(vision_timing_stop_is_idempotent_and_unwind_records_partial) {
  StageTimings timing(true);
  {
    TimingScope scope(&timing, TimingStage::kResize);
    scope.Stop();
    scope.Stop();
  }
  try {
    TimingScope scope(&timing, TimingStage::kNormalize);
    throw std::runtime_error("processor exception");
  } catch (const std::runtime_error&) {
  }
  Q4T_CHECK(timing.Calls(TimingStage::kResize) == 1);
  Q4T_CHECK(timing.Calls(TimingStage::kNormalize) == 1);
  Q4T_CHECK(timing.Calls(TimingStage::kPatchify) == 0);
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  timing.Report(file, "partial", false, 1, 3, 64);
  const auto output = Read(file);
  std::fclose(file);
  Q4T_CHECK(output.find("completed=0 items=1 frames=3 patches=64") !=
            std::string::npos);
  Q4T_CHECK(output.find("clock=host_wall") != std::string::npos);
  Q4T_CHECK(output.find("patchify_calls=0") != std::string::npos);
  return true;
}

Q4T_TEST(vision_timing_terminal_fields_and_fresh_request) {
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  StageTimings first(true);
  for (size_t i = 0; i < q4t::vision::kTimingNames.size(); ++i) {
    TimingScope scope(&first, static_cast<TimingStage>(i));
  }
  first.Report(file, "first", true, 2, 2, 128);
  StageTimings next(true);
  next.Report(file, "next", false, 0, 0, 0);
  const auto output = Read(file);
  std::fclose(file);
  const auto boundary = output.find('\n');
  Q4T_CHECK(boundary != std::string::npos);
  const auto a = output.substr(0, boundary);
  const auto b = output.substr(boundary + 1);
  for (const char* name : q4t::vision::kTimingNames) {
    Q4T_CHECK(a.find(std::string(name) + "_calls=1") != std::string::npos);
    Q4T_CHECK(b.find(std::string(name) + "_calls=0") != std::string::npos);
  }
  Q4T_CHECK(a.find("id=first") != std::string::npos);
  Q4T_CHECK(b.find("id=next") != std::string::npos);
  return true;
}
