#include "q4t/server/multipart.h"

#include <algorithm>
#include <map>
#include <vector>

namespace q4t::server {
namespace {
constexpr size_t kFragmentBytes = 8192;
constexpr size_t kHeaderBytes = 8192;
constexpr size_t kIgnoredBytes = 4096;
constexpr size_t kPaddingBytes = 256;

bool Space(char c) { return c == ' ' || c == '\t'; }
std::string_view Trim(std::string_view text) {
  while (!text.empty() && Space(text.front())) text.remove_prefix(1);
  while (!text.empty() && Space(text.back())) text.remove_suffix(1);
  return text;
}
std::string Lower(std::string_view text) {
  std::string result(text);
  for (auto& c : result)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  return result;
}
bool Token(unsigned char c) {
  return c > 32 && c < 127 &&
         std::string_view("()<>@,;:\\\"/[]?={}").find(c) ==
             std::string_view::npos;
}
bool Printable(std::string_view value) {
  return std::all_of(value.begin(), value.end(), [](unsigned char c) {
    return c == '\t' || (c >= 32 && c != 127);
  });
}
bool Boundary(std::string_view value) {
  if (value.empty() || value.size() > 70 || value.back() == ' ') return false;
  for (unsigned char c : value)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') ||
          std::string_view("'()+_,-./:=? ").find(c) != std::string_view::npos))
      return false;
  return true;
}
bool Parameters(std::string_view text, std::string_view base,
                std::map<std::string, std::string>* fields) {
  if (text.size() > kHeaderBytes || !Printable(text)) return false;
  const auto split = text.find(';');
  if (Lower(Trim(text.substr(0, split))) != base) return false;
  if (split == text.npos) return true;
  text.remove_prefix(split);
  while (!text.empty()) {
    if (text.front() != ';' || fields->size() >= 32) return false;
    text = Trim(text.substr(1));
    size_t end = 0;
    while (end < text.size() && Token(text[end])) ++end;
    if (!end) return false;
    const auto key = Lower(text.substr(0, end));
    text = Trim(text.substr(end));
    if (text.empty() || text.front() != '=') return false;
    text = Trim(text.substr(1));
    if (text.empty()) return false;
    std::string value;
    if (text.front() == '"') {
      text.remove_prefix(1);
      bool closed = false;
      while (!text.empty()) {
        char c = text.front();
        text.remove_prefix(1);
        if (c == '"') {
          closed = true;
          break;
        }
        if (c == '\\') {
          if (text.empty()) return false;
          c = text.front();
          text.remove_prefix(1);
        }
        value += c;
      }
      if (!closed) return false;
    } else {
      end = 0;
      while (end < text.size() && Token(text[end])) ++end;
      if (!end) return false;
      value.assign(text.substr(0, end));
      text.remove_prefix(end);
    }
    text = Trim(text);
    if (!fields->emplace(key, std::move(value)).second) return false;
  }
  return true;
}
// A complete delimiter line excludes CRLF. Preamble and closing delimiters
// may have bounded MIME transport padding; padding is not file content.
bool DelimiterLine(std::string_view line, std::string_view boundary,
                   bool* closing) {
  const auto prefix = "--" + std::string(boundary);
  if (!line.starts_with(prefix)) return false;
  line.remove_prefix(prefix.size());
  *closing = line.starts_with("--");
  if (*closing) line.remove_prefix(2);
  return line.size() <= kPaddingBytes &&
         std::all_of(line.begin(), line.end(), Space);
}
}  // namespace

Status MultipartBoundary(std::string_view content_type, std::string* boundary) {
  boundary->clear();
  std::map<std::string, std::string> fields;
  if (!Parameters(content_type, "multipart/form-data", &fields) ||
      !fields.contains("boundary") || !Boundary(fields.at("boundary")))
    return Status::Fail("Invalid multipart/form-data boundary");
  *boundary = fields.at("boundary");
  return {};
}

MultipartDecoder::MultipartDecoder(std::string boundary,
                                   uint64_t max_wire_bytes, size_t max_parts,
                                   MultipartCallbacks callbacks)
    : boundary_(std::move(boundary)),
      marker_("\r\n--" + boundary_),
      max_wire_bytes_(max_wire_bytes),
      max_parts_(max_parts),
      callbacks_(std::move(callbacks)) {
  if (!Boundary(boundary_) || !max_wire_bytes_ || !max_parts_ ||
      max_parts_ > 1024 || !callbacks_.begin || !callbacks_.data ||
      !callbacks_.end)
    Fail("Invalid multipart decoder configuration");
}

Status MultipartDecoder::Fail(std::string message) {
  if (status_) status_ = Status::Fail(std::move(message));
  return status_;
}

Status MultipartDecoder::Feed(std::string_view bytes) {
  if (!status_) return status_;
  if (finished_) return Fail("Multipart input after Finish");
  if (bytes.size() > max_wire_bytes_ - wire_bytes_)
    return Fail("Multipart wire byte limit exceeded");
  wire_bytes_ += bytes.size();
  while (!bytes.empty()) {
    const auto count = std::min(bytes.size(), kFragmentBytes);
    buffer_.append(bytes.substr(0, count));
    bytes.remove_prefix(count);
    auto status = Process(false);
    if (!status) return status;
  }
  return {};
}

Status MultipartDecoder::Finish() {
  if (!status_) return status_;
  if (finished_) return Fail("Multipart Finish repeated");
  auto status = Process(true);
  if (!status) return status;
  if (state_ != State::kEpilogue) return Fail("Truncated multipart body");
  finished_ = true;
  return {};
}

Status MultipartDecoder::Emit(size_t bytes) {
  if (bytes) {
    try {
      status_ = callbacks_.data(std::string_view(buffer_).substr(0, bytes));
    } catch (...) {
      return Fail("Multipart data consumer failed");
    }
    if (!status_) return status_;
    buffer_.erase(0, bytes);
  }
  return {};
}

Status MultipartDecoder::Headers(std::string_view text) {
  std::vector<std::string> lines;
  while (!text.empty()) {
    const auto end = text.find("\r\n");
    auto line = text.substr(0, end);
    if (line.empty() || !Printable(line)) return Fail("Invalid part header");
    if (Space(line.front())) {
      if (lines.empty()) return Fail("Invalid folded part header");
      lines.back() += ' ';
      lines.back() += Trim(line);
    } else {
      if (lines.size() >= 32) return Fail("Multipart header count exceeded");
      lines.emplace_back(line);
    }
    if (end == text.npos) break;
    text.remove_prefix(end + 2);
  }
  std::map<std::string, std::string> headers;
  for (const auto& line : lines) {
    const auto split = line.find(':');
    if (!split || split == line.npos ||
        !std::all_of(line.begin(), line.begin() + split, Token))
      return Fail("Invalid multipart header name");
    const auto name = Lower(std::string_view(line).substr(0, split));
    if (name != "content-disposition" && name != "content-type" &&
        name != "content-transfer-encoding")
      continue;  // RFC 7578: other part header fields are ignored.
    if (!headers.emplace(name, Trim(std::string_view(line).substr(split + 1)))
             .second)
      return Fail("Duplicate multipart part header");
  }
  std::map<std::string, std::string> fields;
  if (!headers.contains("content-disposition") ||
      !Parameters(headers.at("content-disposition"), "form-data", &fields) ||
      !fields.contains("name") || fields.at("name").empty() ||
      fields.at("name").size() > 256 || fields.contains("filename*") ||
      (fields.contains("filename") && fields.at("filename").size() > 4096))
    return Fail("Invalid multipart form-data disposition");
  if (headers.contains("content-transfer-encoding")) {
    const auto encoding = Lower(headers.at("content-transfer-encoding"));
    if (encoding != "binary" && encoding != "8bit" && encoding != "7bit")
      return Fail("Multipart requires unencoded binary part bytes");
  }
  if (++parts_ > max_parts_) return Fail("Multipart part count exceeded");
  MultipartPart part;
  part.name = fields.at("name");
  part.has_filename = fields.contains("filename");
  if (part.has_filename) part.filename = fields.at("filename");
  part.content_type = headers.contains("content-type")
                          ? headers.at("content-type")
                          : "text/plain";
  try {
    status_ = callbacks_.begin(part);
  } catch (...) {
    return Fail("Multipart part consumer failed");
  }
  return status_;
}

Status MultipartDecoder::Process(bool final) {
  for (;;) {
    if (state_ == State::kEpilogue) {
      if (buffer_.size() > kIgnoredBytes - ignored_bytes_)
        return Fail("Multipart epilogue byte limit exceeded");
      ignored_bytes_ += buffer_.size();
      buffer_.clear();
      return {};
    }
    if (state_ == State::kPreamble) {
      const auto end = buffer_.find("\r\n");
      if (end == buffer_.npos && !final) {
        if (buffer_.size() > kIgnoredBytes + boundary_.size() + kPaddingBytes)
          return Fail("Multipart preamble byte limit exceeded");
        return {};
      }
      const auto line = std::string_view(buffer_).substr(0, end);
      bool closing = false;
      if (DelimiterLine(line, boundary_, &closing)) {
        if (end == buffer_.npos && !closing)
          return Fail("Truncated multipart opening delimiter");
        state_ = closing ? State::kEpilogue : State::kHeaders;
        ignored_bytes_ = 0;
      } else {
        if (line.size() + 2 > kIgnoredBytes - ignored_bytes_)
          return Fail("Multipart preamble byte limit exceeded");
        ignored_bytes_ += line.size() + 2;
        if (end == buffer_.npos) return Fail("Missing multipart delimiter");
      }
      buffer_.erase(0, end == buffer_.npos ? buffer_.size() : end + 2);
      continue;
    }
    if (state_ == State::kHeaders) {
      const auto end = buffer_.find("\r\n\r\n");
      if (end == buffer_.npos) {
        if (buffer_.size() > kHeaderBytes)
          return Fail("Multipart header byte limit exceeded");
        return final ? Fail("Truncated multipart headers") : Status{};
      }
      if (end > kHeaderBytes)
        return Fail("Multipart header byte limit exceeded");
      if (!Headers(std::string_view(buffer_).substr(0, end))) return status_;
      buffer_.erase(0, end + 4);
      state_ = State::kData;
      continue;
    }
    const auto at = buffer_.find(marker_);
    if (at == buffer_.npos) {
      const auto keep = marker_.size() - 1;
      if (buffer_.size() > keep && !Emit(buffer_.size() - keep)) return status_;
      return final ? Fail("Missing multipart closing delimiter") : Status{};
    }
    if (at && !Emit(at)) return status_;
    // Validate the delimiter suffix before treating a binary prefix match as
    // framing. Preserve invalid lookalikes byte-for-byte as file content.
    size_t end = marker_.size();
    bool closing = false;
    if (buffer_.size() > end && buffer_[end] == '-') {
      if (buffer_.size() == end + 1 && !final) return {};
      closing = buffer_.compare(end, 2, "--") == 0;
      if (closing) end += 2;
    }
    const auto padding = end;
    while (end < buffer_.size() && Space(buffer_[end])) ++end;
    if (end - padding > kPaddingBytes)
      return Fail("Multipart delimiter padding limit exceeded");
    if (end == buffer_.size() && closing && final) {
      try {
        status_ = callbacks_.end();
      } catch (...) {
        return Fail("Multipart completion consumer failed");
      }
      if (!status_) return status_;
      buffer_.clear();
      state_ = State::kEpilogue;
      ignored_bytes_ = 0;
      continue;
    }
    if (end == buffer_.size() ||
        (end + 1 == buffer_.size() && buffer_[end] == '\r'))
      return final ? Fail("Truncated multipart delimiter") : Status{};
    if (buffer_.compare(end, 2, "\r\n") != 0) {
      if (!Emit(1)) return status_;
      continue;
    }
    try {
      status_ = callbacks_.end();
    } catch (...) {
      return Fail("Multipart completion consumer failed");
    }
    if (!status_) return status_;
    buffer_.erase(0, end + 2);
    state_ = closing ? State::kEpilogue : State::kHeaders;
    ignored_bytes_ = 0;
  }
}
}  // namespace q4t::server
