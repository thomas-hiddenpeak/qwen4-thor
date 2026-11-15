// Shared bounded MCP SSE event reader; no scheduling or network ownership.
#pragma once

#include "object_protocol.h"
#include "remote_http_internal.h"

namespace q4t::server::mcp_detail {
// Reads complete events but does not wait for EOF after an RPC response.
// LF, CRLF and CR are accepted, with a single optional initial UTF-8 BOM.
class Events {
 public:
  explicit Events(file_detail::RemoteHttp* http) : http_(http) {}
  ObjectReply Next(std::string* kind, std::string* data) {
    kind->clear();
    data->clear();
    for (;;) {
      std::string line;
      auto reply = Line(&line);
      if (!reply.ok()) return reply;
      if (first_) {
        first_ = false;
        if (line.starts_with("\xef\xbb\xbf")) line.erase(0, 3);
      }
      if (line.empty()) {
        if (!data->empty()) {
          data->pop_back();
          if (kind->empty()) *kind = "message";
          return {};
        }
        kind->clear();
        continue;
      }
      if (line.front() == ':') continue;
      const auto colon = line.find(':');
      const auto field = line.substr(0, colon);
      auto value = colon == line.npos
                       ? std::string_view{}
                       : std::string_view(line).substr(colon + 1);
      if (value.starts_with(' ')) value.remove_prefix(1);
      if (field == "event") {
        if (value.size() > 128)
          return object_detail::Error(413, "mcp_response_capacity",
                                      "MCP response budget exceeded");
        *kind = value;
      } else if (field == "data") {
        if (data->size() + value.size() + 1 > (1U << 20))
          return object_detail::Error(413, "mcp_response_capacity",
                                      "MCP response budget exceeded");
        data->append(value);
        data->push_back('\n');
      }
    }
  }

 private:
  ObjectReply Line(std::string* line) {
    line->clear();
    for (;;) {
      if (index_ == bytes_.size()) {
        bool eof = false;
        auto reply = http_->Read(&bytes_, &eof);
        if (!reply.ok()) return reply;
        index_ = 0;
        if (eof)
          return object_detail::Error(502, "mcp_stream_truncated",
                                      "MCP stream ended");
      }
      const char c = bytes_[index_++];
      if (skip_lf_) {
        skip_lf_ = false;
        if (c == '\n') continue;
      }
      if (c == '\r' || c == '\n') {
        skip_lf_ = c == '\r';
        return {};
      }
      if (line->size() == (1U << 20))
        return object_detail::Error(413, "mcp_response_capacity",
                                    "MCP response budget exceeded");
      line->push_back(c);
    }
  }
  file_detail::RemoteHttp* http_;
  std::string bytes_;
  size_t index_ = 0;
  bool skip_lf_ = false, first_ = true;
};

}  // namespace q4t::server::mcp_detail
