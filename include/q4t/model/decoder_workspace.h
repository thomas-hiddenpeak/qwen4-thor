// Decoder workspace layout shared by allocation, execution and inspection.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace q4t::model {
struct FullAttentionWeights;

// Actual offsets into the caller-owned device-global workspace. All regions
// are 256-byte aligned. This does not imply any L1/L2 residency guarantee.
struct DecoderWorkspaceLayout {
  size_t attention = 0;
  size_t moe = 0;
  size_t moe_gemm = 0;
  size_t hc_gemm = 0;
  size_t ple = 0;
  size_t mixed = 0;
  size_t block = 0;
  size_t normed = 0;
  size_t hc_down = 0;
  size_t hc_up = 0;
  size_t hc_down_bytes = 0;
  size_t hc_up_bytes = 0;
  size_t combined = 0;
  size_t ple_trunk = 0;
  size_t gate = 0;
  size_t gate_bytes = 0;
  size_t attention_bytes = 0;
  size_t moe_bytes = 0;
  size_t ple_bytes = 0;
  size_t hidden_bytes = 0;
  size_t hyper_bytes = 0;
  size_t total_bytes = 0;
};

DecoderWorkspaceLayout MakeDecoderWorkspaceLayout(
    int T, bool is_full, bool has_ple, int hc, int hs, int E, int moe_is,
    int shared_is, int k, const FullAttentionWeights* full, int lowrank);

// Coarse caller-stream phases. Sublayers must join auxiliary producers before
// returning their outputs to the caller stream. These labels describe the
// existing runner order; they are not a scheduler or a state-commit protocol.
enum class DecoderPhase : uint8_t {
  kPle,
  kAttentionRead,
  kAttention,
  kAttentionWrite,
  kMlpRead,
  kMoe,
  kMlpWrite,
};

struct DecoderResourceView {
  const char* name;
  size_t offset;
  size_t bytes;
  // Phases during which these bytes must remain reserved for this view.
  // A bit covers the whole phase, including work enqueued on its stream.
  uint32_t live_phases;
};

// Uses the exact layout consumed by DecoderLayerForward. Does not allocate,
// launch work, inspect hardware, or describe weights/sequence state outside
// the workspace. Called by inspection tools, not the forward hot path.
std::array<DecoderResourceView, 13> DescribeDecoderWorkspace(
    const DecoderWorkspaceLayout& layout);

}  // namespace q4t::model
