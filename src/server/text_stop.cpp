#include "q4t/server/text_stop.h"

#include <algorithm>
#include <utility>

#include "q4t/server/http_response.h"

namespace q4t::server {

Status ParseTextStop(const io::Json& request, bool constraints_active,
                     std::vector<std::string>* stops,
                     std::string* error_param) {
  stops->clear();
  const auto* value = request.Find("stop");
  if (!value || value->IsNull()) return Status();
  const auto invalid = [&](std::string param, const char* message) {
    stops->clear();
    if (error_param) *error_param = std::move(param);
    return Status::Fail(message);
  };
  if (!value->IsString() && !value->IsArray())
    return invalid("stop", "stop must be a string, string array or null");
  const size_t count = value->IsString() ? 1 : value->array.size();
  if (count > kMaxStopStrings)
    return invalid("stop", "stop allows at most 4 strings");
  for (size_t i = 0; i < count; ++i) {
    const auto& item = value->IsString() ? *value : value->array[i];
    if (!item.IsString() || item.str.empty() ||
        item.str.size() > kMaxStopBytes ||
        detail::NormalizeUtf8(item.str) != item.str)
      return invalid(
          value->IsString() ? "stop" : "stop[" + std::to_string(i) + "]",
          "stop entries must be valid UTF-8 strings of 1..256 bytes");
    stops->push_back(item.str);
  }
  if (stops->empty()) return Status();
  if (constraints_active)
    return invalid("stop", "stop is not supported with constrained output");
  if (request.Has("session_id") || request.Has("prefix_cache_scope") ||
      request.GetInt("prefix_cache_write_tokens", 0) > 0)
    return invalid("stop", "stop is not supported with state caches");
  return Status();
}

std::string TextStopStream::Append(std::string_view text) {
  if (finished_ || Matched()) return {};
  if (!Enabled()) return std::string(text);
  pending_.append(text);
  size_t first = std::string::npos;
  size_t first_end = std::string::npos;
  for (size_t i = 0; i < stops_.size(); ++i) {
    const size_t found = pending_.find(stops_[i]);
    if (found != std::string::npos && found + stops_[i].size() < first_end) {
      first = found;
      first_end = found + stops_[i].size();
      matched_index_ = static_cast<int>(i);
    }
  }
  if (Matched()) {
    std::string output = pending_.substr(0, first);
    pending_.clear();
    return output;
  }
  size_t keep = 0;
  for (const auto& stop : stops_) {
    for (size_t n = std::min(pending_.size(), stop.size() - 1); n > keep; --n) {
      if (pending_.ends_with(std::string_view(stop).substr(0, n))) {
        keep = n;
        break;
      }
    }
  }
  std::string output = pending_.substr(0, pending_.size() - keep);
  pending_.erase(0, pending_.size() - keep);
  return output;
}

std::string TextStopStream::Finish() {
  finished_ = true;
  return std::exchange(pending_, {});
}

}  // namespace q4t::server
