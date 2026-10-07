#pragma once

namespace q4t::server {

// A speculative step consumes the pending bonus and k draft tokens. Use
// subtraction so malformed/near-limit positions cannot overflow the check.
inline bool MtpStepFits(int position, int max_len, int max_prefill, int k) {
  return position >= 0 && position < max_len && k > 0 &&
         k < max_prefill && k < max_len - position;
}

}  // namespace q4t::server
