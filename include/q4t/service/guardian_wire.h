// Bounded private Guardian protocol. Uses only the standalone JSON parser.
#pragma once

#include <cstdint>
#include <initializer_list>
#include <string>
#include <utility>

#include "q4t/io/json.h"

namespace q4t::service::wire {
constexpr size_t kMaxFrame = 128 * 1024;
io::Json String(std::string text);
io::Json Unsigned(uint64_t number);
io::Json Boolean(bool value);
io::Json Object(
    std::initializer_list<std::pair<std::string, io::Json>> members);
Status Parse(const std::string& text, io::Json* result);
Status Encode(const io::Json& value, std::string* result);
Status GetUnsigned(const io::Json& object, const std::string& key,
                   uint64_t* result);
bool HasKeys(const io::Json& object, std::initializer_list<std::string> keys);
}  // namespace q4t::service::wire
