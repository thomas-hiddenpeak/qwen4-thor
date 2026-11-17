#include "q4t/audio/vad.h"
#include "q4t/test.h"

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>

namespace {
using namespace q4t;
std::string Header() {
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/silero_vad_header.json";
  std::ifstream file(path);
  std::ostringstream text;
  text << file.rdbuf();
  return text.str();
}
std::vector<uint8_t> Fixture() {
  const auto header = Header();
  std::vector<uint8_t> bytes(8 + header.size() + audio::kVadWeightBytes);
  const uint64_t size = header.size();
  std::memcpy(bytes.data(), &size, 8);
  std::memcpy(bytes.data() + 8, header.data(), header.size());
  return bytes;
}
bool Same(const audio::VadState& a, const audio::VadState& b) {
  return a.context == b.context && a.hidden == b.hidden && a.cell == b.cell;
}
}  // namespace

Q4T_TEST(vad_fixed_upstream_header_has_all_fifteen_f32_tensors) {
  const auto text = Header();
  Q4T_CHECK(text.size() == 1208);
  io::Json header;
  Q4T_CHECK(
      io::ParseJson(text, &header, false, {.reject_duplicate_keys = true}));
  audio::VadTensorLayout layout;
  Q4T_CHECK(audio::ParseVadTensorLayout(header, 1238532, &layout));
  Q4T_CHECK(layout.offsets[0] == 0 && layout.offsets[9] == 177408 &&
            layout.offsets[14] == 309632);
  return true;
}

Q4T_TEST(vad_tensor_dtype_shape_overlap_and_unknown_fields_are_rejected) {
  for (const auto& [before, after] :
       std::vector<std::pair<std::string, std::string>>{
           {"F32", "F16"},
           {"[258,1,256]", "[257,1,256]"},
           {"[0,264192]", "[4,264196]"},
           {"stft_conv.weight", "unexpected.weight"}}) {
    std::string text = Header();
    const auto at = text.find(before);
    Q4T_CHECK(at != text.npos);
    text.replace(at, before.size(), after);
    io::Json header;
    Q4T_CHECK(io::ParseJson(text, &header));
    audio::VadTensorLayout layout;
    layout.offsets.fill(42);
    Q4T_CHECK(!audio::ParseVadTensorLayout(header, 1238532, &layout));
    Q4T_CHECK(layout.offsets[0] == 42 && layout.offsets[14] == 42);
  }
  return true;
}

Q4T_TEST(vad_load_is_atomic_for_nonfinite_truncated_and_cancelled_assets) {
  audio::SileroVad model;
  auto bytes = Fixture();
  Q4T_CHECK(model.Load(bytes) && model.Ready());
  const float bad = std::numeric_limits<float>::quiet_NaN();
  std::memcpy(bytes.data() + bytes.size() - 4, &bad, 4);
  Q4T_CHECK(!model.Load(bytes) && model.Ready());
  Q4T_CHECK(!model.Load({bytes.data(), bytes.size() - 1}) && model.Ready());
  Q4T_CHECK(!model.Load(Fixture(), [] { return true; }) && model.Ready());
  audio::VadState state;
  std::array<float, 512> pcm{};
  float probability = -1;
  Q4T_CHECK(model.Step(pcm, &state, &probability));
  Q4T_CHECK(probability == .5f);
  return true;
}

Q4T_TEST(vad_step_rejects_unloaded_shape_and_nonfinite_request_state) {
  audio::SileroVad model;
  audio::VadState state;
  std::array<float, 512> pcm{};
  float probability = -1;
  Q4T_CHECK(!model.Step(pcm, &state, &probability));
  Q4T_CHECK(model.Load(Fixture()));
  Q4T_CHECK(!model.Step({pcm.data(), 511}, &state, &probability));
  pcm[10] = std::numeric_limits<float>::infinity();
  Q4T_CHECK(!model.Step(pcm, &state, &probability));
  pcm[10] = 0;
  state.hidden[10] = std::numeric_limits<float>::infinity();
  Q4T_CHECK(!model.Step(pcm, &state, &probability));
  Q4T_CHECK(probability == -1);
  return true;
}

Q4T_TEST(vad_state_is_request_owned_and_preserves_exact_context_samples) {
  audio::SileroVad model;
  Q4T_CHECK(model.Load(Fixture()));
  audio::VadState first, other;
  std::array<float, 512> pcm{};
  for (size_t i = 0; i < pcm.size(); ++i) pcm[i] = float(i) / 512;
  float probability = -1;
  Q4T_CHECK(model.Step(pcm, &first, &probability));
  Q4T_CHECK(probability == .5f && first.context[0] == 448.f / 512 &&
            first.context[63] == 511.f / 512);
  Q4T_CHECK(other.context[0] == 0 && other.hidden[0] == 0);
  return true;
}

Q4T_TEST(vad_cancel_and_intermediate_overflow_preserve_frame_owner) {
  audio::SileroVad model;
  auto bytes = Fixture();
  Q4T_CHECK(model.Load(bytes));
  audio::VadState state;
  state.context[1] = .1f;
  const auto before = state;
  std::array<float, 512> pcm;
  pcm.fill(1);
  for (int stop_at : {1, 8, 25, 40, 56}) {
    int calls = 0;
    float probability = -1;
    Q4T_CHECK(!model.Step(pcm, &state, &probability,
                          [&] { return ++calls == stop_at; }));
    Q4T_CHECK(Same(state, before) && probability == -1);
  }
  const float huge = std::numeric_limits<float>::max();
  for (size_t i = 0; i < 256; ++i)
    std::memcpy(bytes.data() + 1216 + i * 4, &huge, 4);
  Q4T_CHECK(model.Load(bytes));
  float probability = -1;
  Q4T_CHECK(!model.Step(pcm, &state, &probability));
  Q4T_CHECK(Same(state, before) && probability == -1);
  return true;
}

Q4T_TEST(vad_score_zero_pads_only_tail_and_reports_real_sample_ranges) {
  audio::SileroVad model;
  Q4T_CHECK(model.Load(Fixture()));
  std::vector<float> pcm(1025);
  std::vector<audio::VadFrame> frames;
  Q4T_CHECK(model.Score(pcm, 2000, &frames));
  Q4T_CHECK(frames.size() == 3 && frames[0].begin_sample == 0 &&
            frames[0].end_sample == 512 && frames[1].end_sample == 1024 &&
            frames[2].begin_sample == 1024 && frames[2].end_sample == 1025);
  for (const auto& frame : frames) Q4T_CHECK(frame.speech_probability == .5f);
  return true;
}

Q4T_TEST(vad_score_budget_cancellation_and_new_request_are_isolated) {
  audio::SileroVad model;
  Q4T_CHECK(model.Load(Fixture()));
  std::array<float, 512> pcm{};
  std::vector<audio::VadFrame> frames{{17, 25, .25f}};
  Q4T_CHECK(!model.Score(pcm, 511, &frames));
  Q4T_CHECK(!model.Score(pcm, audio::kVadMaxSamples + 1, &frames));
  Q4T_CHECK(!model.Score(pcm, 512, &frames, [] { return true; }));
  Q4T_CHECK(frames.size() == 1 && frames[0].begin_sample == 17 &&
            frames[0].speech_probability == .25f);
  Q4T_CHECK(model.Score(pcm, audio::kVadMaxSamples, &frames));
  Q4T_CHECK(frames.size() == 1 && frames[0].begin_sample == 0);
  return true;
}
