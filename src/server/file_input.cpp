#include "q4t/server/file_input.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/prompt_cache_policy.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;

ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
ObjectReply At(ObjectReply result, std::string_view param) {
  if (!result.ok()) {
    if (!result.value.IsObject()) result.value = Object();
    Set(&result.value, "param", String(std::string(param)));
  }
  return result;
}
ObjectReply Active(const BinaryAccess& access) {
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (Clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "File input deadline expired");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "File input cancelled");
  return {};
}
bool Metadata(std::string_view value) {
  return !value.empty() && value.size() <= 1024 &&
         detail::NormalizeUtf8(value) == value &&
         std::none_of(value.begin(), value.end(),
                      [](unsigned char c) { return c < 32 || c == 127; });
}
bool HttpUrl(std::string_view url) {
  const size_t offset = url.starts_with("https://")  ? 8
                        : url.starts_with("http://") ? 7
                                                     : 0;
  if (!offset || url.size() > 8192 ||
      std::any_of(url.begin(), url.end(), [](unsigned char c) {
        return c <= 32 || c == 127 || c == '\\';
      }))
    return false;
  const auto authority =
      url.substr(offset, url.find_first_of("/?#", offset) - offset);
  return !authority.empty() && authority.find('@') == authority.npos;
}
bool Mime(std::string_view mime) {
  const auto slash = mime.find('/');
  return slash != mime.npos && slash != 0 && slash + 1 < mime.size() &&
         mime.size() <= 255 &&
         std::all_of(mime.begin(), mime.end(),
                     [](unsigned char c) {
                       return (c >= 'a' && c <= 'z') ||
                              (c >= '0' && c <= '9') || c == '/' || c == '-' ||
                              c == '+' || c == '.';
                     }) &&
         mime.find('/', slash + 1) == mime.npos;
}
int Digit(unsigned char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  return c == '+' ? 62 : c == '/' ? 63 : -1;
}
ObjectReply CheckBudget(const FileInputBudget* budget) {
  if (!budget || budget->max_bytes > kFileInputBytes || !budget->max_files ||
      budget->max_files > 4096 || budget->used_bytes > budget->max_bytes ||
      budget->used_files > budget->max_files)
    return Error(500, "file_input_budget_invalid", "Invalid file input budget");
  if (budget->used_files == budget->max_files)
    return Error(413, "file_input_capacity_exceeded",
                 "File input budget exceeded");
  return {};
}
ObjectReply Reserve(FileInputBudget* budget, uint64_t bytes) {
  auto result = CheckBudget(budget);
  if (!result.ok()) return result;
  if (bytes > budget->max_bytes - budget->used_bytes)
    return Error(413, "file_input_capacity_exceeded",
                 "File input budget exceeded");
  ++budget->used_files;
  budget->used_bytes += bytes;
  return {};
}
ObjectReply Decode(const FileInputSource& source, const BinaryAccess& access,
                   FileInputBudget* budget, std::string* output) {
  const auto input = source.value;
  if (input.size() % 4)
    return Invalid("File data requires padded standard base64", source.param);
  size_t padding = !input.empty() && input.back() == '=' ? 1 : 0;
  if (input.size() > 1 && input[input.size() - 2] == '=') ++padding;
  const uint64_t bytes = input.size() / 4 * 3 - padding;
  auto result = Reserve(budget, bytes);
  if (!result.ok()) return result;
  output->reserve(bytes);
  for (size_t i = 0; i < input.size(); i += 4) {
    if (i % 4096 == 0) {
      result = Active(access);
      if (!result.ok()) return result;
    }
    const int a = Digit(input[i]), b = Digit(input[i + 1]);
    const int c = input[i + 2] == '=' ? 0 : Digit(input[i + 2]);
    const int d = input[i + 3] == '=' ? 0 : Digit(input[i + 3]);
    const bool final = i + 4 == input.size();
    if (a < 0 || b < 0 || c < 0 || d < 0 ||
        ((!final || !padding) &&
         (input[i + 2] == '=' || input[i + 3] == '=')) ||
        (final && ((padding == 2 && (b & 15)) ||
                   (padding == 1 && ((c & 3) || input[i + 2] == '=')))))
      return Invalid("File data is not canonical base64", source.param);
    output->push_back(static_cast<char>((a << 2) | (b >> 4)));
    if (!final || padding < 2)
      output->push_back(static_cast<char>((b << 4) | (c >> 2)));
    if (!final || !padding) output->push_back(static_cast<char>((c << 6) | d));
  }
  return Active(access);
}
}  // namespace

ObjectReply ParseFileInputSource(const Json& part, FileInputProfile profile,
                                 std::string_view param,
                                 FileInputSource* source) {
  if (!source) return Error(500, "file_input_state_invalid", "Missing source");
  *source = {};
  const bool chat = profile == FileInputProfile::kChat;
  const bool stored = profile == FileInputProfile::kResponsesStored;
  const auto path = std::string(param);
  std::string error;
  auto checked =
      chat ? InputFields(part, {"type", "file", "prompt_cache_breakpoint"},
                         &error, path)
           : InputFields(part,
                         {"type", "file_data", "file_id", "file_url",
                          "filename", "detail", "prompt_cache_breakpoint"},
                         &error, path);
  if (!checked) return Invalid(checked.message(), error);
  if (part.GetString("type") != (chat ? "file" : "input_file"))
    return Invalid("Unexpected file input type", path + ".type");
  const auto* file = chat ? part.Find("file") : &part;
  const auto at = chat ? path + ".file" : path;
  if (!file || !file->IsObject()) return Invalid("Expected file object", at);
  if (chat) {
    checked =
        InputFields(*file, {"file_data", "file_id", "filename"}, &error, at);
    if (!checked) return Invalid(checked.message(), error);
  }
  for (const auto* name : {"file_data", "file_id", "file_url", "filename"}) {
    const auto* value = file->Find(name);
    if (!value) continue;
    const bool nullable =
        stored || (!chat && std::string_view(name) == "file_id");
    if (!(value->IsString() || (nullable && value->IsNull())))
      return Invalid("Expected file input string", at + '.' + name);
  }
  FileInputSource next;
  next.pdf_only = chat;
  next.filename = file->GetString("filename");
  if (Nonnull(*file, "filename") && !Metadata(next.filename))
    return Invalid("Invalid file input filename", at + ".filename");
  if (!chat && part.Has("detail")) {
    next.detail = part.GetString("detail");
    if (next.detail != "auto" && next.detail != "low" && next.detail != "high")
      return Invalid("Invalid PDF detail", path + ".detail");
  }
  const auto* breakpoint = part.Find("prompt_cache_breakpoint");
  if (breakpoint && breakpoint->IsNull() && !stored)
    return Invalid("Expected cache breakpoint object",
                   path + ".prompt_cache_breakpoint");
  checked = ParsePromptCacheBreakpoint(
      breakpoint, path + ".prompt_cache_breakpoint", &next.cache_breakpoint,
      &next.cache_ttl, &error);
  if (!checked) return Invalid(checked.message(), error);
  int selected = 0;
  for (const auto* name : {"file_id", "file_data", "file_url"}) {
    if (!Nonnull(*file, name)) continue;
    ++selected;
    next.value = file->Find(name)->str;
    next.param = at + '.' + name;
    next.kind =
        std::string_view(name) == "file_id"    ? FileInputSource::Kind::kStored
        : std::string_view(name) == "file_url" ? FileInputSource::Kind::kUrl
                                               : FileInputSource::Kind::kInline;
  }
  if (selected != 1)
    return Invalid("File input requires exactly one source", at);
  if (next.kind == FileInputSource::Kind::kStored && !ValidId(next.value))
    return Invalid("Invalid file identity", next.param);
  if (next.kind == FileInputSource::Kind::kUrl && !HttpUrl(next.value))
    return Invalid("File URL requires credential-free HTTP(S)", next.param);
  if (next.kind == FileInputSource::Kind::kInline &&
      next.value.starts_with("data:")) {
    const auto comma = next.value.find(',');
    if (comma == next.value.npos || comma > 320)
      return Invalid("Invalid file data URI", next.param);
    auto header = std::string(next.value.substr(5, comma - 5));
    std::transform(header.begin(), header.end(), header.begin(),
                   [](char c) { return c >= 'A' && c <= 'Z' ? c + 32 : c; });
    if (!header.ends_with(";base64"))
      return Invalid("File data URI requires base64", next.param);
    header.resize(header.size() - 7);
    if (header.ends_with(";charset=utf-8")) header.resize(header.size() - 14);
    if (!Mime(header))
      return Invalid("Invalid file data media type", next.param);
    next.mime_type = std::move(header);
    next.value.remove_prefix(comma + 1);
  }
  *source = std::move(next);
  return {};
}

ObjectReply ParseImageInputSource(const Json& part, FileInputProfile profile,
                                  std::string_view param,
                                  FileInputSource* source) {
  if (!source) return Error(500, "file_input_state_invalid", "Missing source");
  *source = {};
  const bool chat = profile == FileInputProfile::kChat;
  const auto path = std::string(param);
  std::string error;
  auto checked =
      chat ? InputFields(part, {"type", "image_url", "prompt_cache_breakpoint"},
                         &error, path)
           : InputFields(part,
                         {"type", "image_url", "file_id", "detail",
                          "prompt_cache_breakpoint"},
                         &error, path);
  if (!checked) return Invalid(checked.message(), error);
  if (part.GetString("type") != (chat ? "image_url" : "input_image") &&
      !(chat && !part.Has("type") && part.Has("image_url")))
    return Invalid("Unexpected image input type", path + ".type");
  const auto* url = part.Find("image_url");
  const auto* id = part.Find("file_id");
  const auto* detail = part.Find("detail");
  std::string url_path = path + ".image_url";
  std::string detail_path = path + ".detail";
  if (chat && url && url->IsObject()) {
    checked = InputFields(*url, {"url", "detail"}, &error, url_path);
    if (!checked) return Invalid(checked.message(), error);
    detail = url->Find("detail");
    detail_path = url_path + ".detail";
    url = url->Find("url");
    url_path += ".url";
  }
  if ((url && !url->IsNull() && !url->IsString()) ||
      (id && !id->IsNull() && !id->IsString()) ||
      (chat && (!url || !url->IsString())) ||
      (bool(url && !url->IsNull()) + bool(id && !id->IsNull()) != 1))
    return Invalid("Image requires exactly one URL or file ID", path);
  FileInputSource next;
  if (detail) {
    if (!detail->IsString() ||
        (detail->str != "auto" && detail->str != "low" &&
         detail->str != "high" && detail->str != "original"))
      return Invalid("Invalid image detail", detail_path);
    next.detail = detail->str;
  }
  const auto* breakpoint = part.Find("prompt_cache_breakpoint");
  if (breakpoint && breakpoint->IsNull() &&
      profile != FileInputProfile::kResponsesStored)
    return Invalid("Expected cache breakpoint object",
                   path + ".prompt_cache_breakpoint");
  checked = ParsePromptCacheBreakpoint(
      breakpoint, path + ".prompt_cache_breakpoint", &next.cache_breakpoint,
      &next.cache_ttl, &error);
  if (!checked) return Invalid(checked.message(), error);
  if (id && !id->IsNull()) {
    if (!ValidId(id->str))
      return Invalid("Invalid image file ID", path + ".file_id");
    next.kind = FileInputSource::Kind::kStored;
    next.value = id->str;
    next.param = path + ".file_id";
  } else {
    next.value = url->str;
    next.param = url_path;
    if (next.value.empty()) return Invalid("Image source is empty", url_path);
    if (next.value.starts_with("https://") ||
        next.value.starts_with("http://")) {
      if (!HttpUrl(next.value))
        return Invalid("Image URL requires credential-free HTTP(S)", url_path);
      next.kind = FileInputSource::Kind::kUrl;
    } else if (next.value.starts_with("data:")) {
      constexpr std::string_view png = "data:image/png;base64,",
                                 jpeg = "data:image/jpeg;base64,",
                                 gif = "data:image/gif;base64,",
                                 webp = "data:image/webp;base64,";
      if (next.value.starts_with(png)) {
        next.mime_type = "image/png";
        next.value.remove_prefix(png.size());
      } else if (next.value.starts_with(jpeg)) {
        next.mime_type = "image/jpeg";
        next.value.remove_prefix(jpeg.size());
      } else if (next.value.starts_with(gif)) {
        next.mime_type = "image/gif";
        next.value.remove_prefix(gif.size());
      } else if (next.value.starts_with(webp)) {
        next.mime_type = "image/webp";
        next.value.remove_prefix(webp.size());
      } else {
        return Invalid("Image data URI requires a supported media type",
                       url_path);
      }
    }
  }
  *source = std::move(next);
  return {};
}

ObjectReply ReadLocalFileInput(ObjectStore* objects, const BinaryAccess& access,
                               const FileInputSource& source,
                               FileInputBudget* budget,
                               FileInputContent* content) {
  if (!content) return Error(500, "file_input_state_invalid", "Missing output");
  *content = {};
  auto result = Active(access);
  if (!result.ok()) return At(std::move(result), source.param);
  FileInputContent next;
  next.filename = source.filename;
  next.mime_type = source.mime_type;
  if (source.kind == FileInputSource::Kind::kInline) {
    result = Decode(source, access, budget, &next.bytes);
  } else if (source.kind == FileInputSource::Kind::kStored) {
    if (!objects)
      return At(
          Error(503, "object_scope_unavailable", "File storage unavailable"),
          source.param);
    result = CheckBudget(budget);
    if (!result.ok()) return At(std::move(result), source.param);
    BinaryStore binary(*objects);
    BinaryRead read;
    ScopeExit cleanup([&] {
      if (read.id.empty() || read.closed || read.commit_unknown) return;
      try {
        auto bounded = access;
        bounded.deadline = Clock::now() + 200ms;
        bounded.cancelled = {};
        binary.Close(bounded, &read);
      } catch (...) {
      }
    });
    const auto id = std::string(source.value);
    result = Files(*objects).Open(access, id, &read, &next.mime_type,
                                  budget->max_bytes - budget->used_bytes);
    if (!result.ok()) return At(std::move(result), source.param);
    next.filename = result.value.GetString("filename");
    next.file_id = id;
    result = Reserve(budget, read.manifest.bytes);
    if (!result.ok()) return At(std::move(result), source.param);
    next.bytes.reserve(read.manifest.bytes);
    for (size_t i = 0; i < read.manifest.chunks.size(); ++i) {
      std::string chunk;
      result = binary.Read(access, read, i, &chunk);
      if (!result.ok()) return At(std::move(result), source.param);
      next.bytes += chunk;
    }
    result = Active(access);
  } else {
    return At(Error(503, "file_fetch_executor_required",
                    "Remote input requires the isolated fetch executor"),
              source.param);
  }
  if (!result.ok()) return At(std::move(result), source.param);
  next.sha256 = Digest(next.bytes);
  result = Active(access);
  if (!result.ok()) return At(std::move(result), source.param);
  *content = std::move(next);
  return {};
}
}  // namespace q4t::server
