#pragma once

namespace q4t::server {

// A speculative step consumes the pending bonus and k draft tokens. Use
// subtraction so malformed/near-limit positions cannot overflow the check.
inline bool MtpStepFits(int position, int max_len, int max_prefill, int k) {
  return position >= 0 && position < max_len && k > 0 &&
         k < max_prefill && k < max_len - position;
}

// Ordinary decode emits its last allowed token without consuming it. A full
// sequential step needs room for its pending correction after k+1 inputs.
inline bool MtpSequentialStepFits(int position, int max_len, int max_prefill,
                                  int k, int output_remaining) {
  return MtpStepFits(position, max_len, max_prefill, k) &&
         output_remaining > 0 && k < output_remaining - 1 &&
         k < max_len - position - 1;
}

}  // namespace q4t::server
