// Shared parsing for endpoints with more than one response representation.
#pragma once

#include <charconv>
#include <string_view>

#include "q4t/server/http_framing.h"

namespace q4t::server::http {
struct MediaPreference {
  int specificity = -1;
  double quality = 0;

  void Offer(int rank, double q) {
    if (rank > specificity || (rank == specificity && q > quality)) {
      specificity = rank;
      quality = q;
    }
  }
  bool PreferredTo(const MediaPreference& other) const {
    return quality > other.quality ||
           (quality == other.quality && specificity > other.specificity);
  }
};

template <class Visitor>
bool VisitAcceptRanges(std::string_view accept, Visitor visit) {
  while (!accept.empty()) {
    const auto comma = accept.find(',');
    auto item = Trim(accept.substr(0, comma));
    const auto semi = item.find(';');
    const auto type = Lower(Trim(item.substr(0, semi)));
    double quality = 1;
    for (auto delimiter = semi; delimiter != item.npos;
         delimiter = item.find(';')) {
      item.remove_prefix(delimiter + 1);
      const auto param = Lower(Trim(item.substr(0, item.find(';'))));
      if (!param.starts_with("q=")) continue;
      const auto value = std::string_view(param).substr(2);
      const auto result =
          std::from_chars(value.data(), value.data() + value.size(), quality,
                          std::chars_format::fixed);
      if (result.ec != std::errc{} ||
          result.ptr != value.data() + value.size() ||
          !(quality >= 0 && quality <= 1))
        return false;
    }
    visit(type, quality);
    if (comma == accept.npos) break;
    accept.remove_prefix(comma + 1);
  }
  return true;
}
}  // namespace q4t::server::http
