#pragma once

#include <string>
#include <string_view>

#include "q4t/server/file_input.h"

namespace q4t::server::file_detail {
ObjectReply DecodeRtfBytes(int codepage, std::string_view bytes,
                           uint64_t max_bytes, std::string* output);
void RtfUtf8(uint32_t scalar, std::string* output);
int RtfCharsetCodepage(int charset);
}  // namespace q4t::server::file_detail
