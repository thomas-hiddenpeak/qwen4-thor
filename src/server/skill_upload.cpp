#include "q4t/server/skill_upload.h"

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"
#include "skill_bundle.h"

namespace q4t::server {
namespace {
ObjectReply Invalid(std::string field, std::string message, int http = 400) {
  auto reply = object_detail::Error(
      http, http == 413 ? "skill_bundle_capacity" : "invalid_request",
      std::move(message));
  reply.value = object_detail::Object(
      {{"param", object_detail::String(std::move(field))}});
  return reply;
}
}  // namespace

SkillUpload::SkillUpload(std::string_view content_type, bool version)
    : version_(version) {
  std::string boundary;
  auto b = MultipartBoundary(content_type, &boundary);
  if (!b) {
    Fail("Content-Type", b.message());
    return;
  }
  MultipartCallbacks callbacks{[this](const auto& p) { return Begin(p); },
                               [this](auto bytes) { return Data(bytes); },
                               [this] { return End(); }};
  parser_ = std::make_unique<MultipartDecoder>(
      std::move(boundary), kSkillWireBytes, skill_detail::kSkillMaxFiles + 1,
      std::move(callbacks));
}
Status SkillUpload::Fail(std::string field, std::string message, int status) {
  if (result_.ok())
    result_ = Invalid(std::move(field), std::move(message), status);
  return Status::Fail(result_.message);
}
Status SkillUpload::Begin(const MultipartPart& part) {
  current_default_ = part.name == "default";
  if (current_default_) {
    if (!version_ || default_seen_ || part.has_filename)
      return Fail(part.name, "Unexpected default field");
    default_seen_ = true;
    return {};
  }
  if ((part.name != "files[]" && part.name != "files") || !part.has_filename ||
      !file_detail::ValidPackageMemberName(part.filename, false) ||
      detail::NormalizeUtf8(part.filename) != part.filename)
    return Fail("files", "Invalid skill member name");
  if (input_.files.size() >= skill_detail::kSkillMaxFiles)
    return Fail("files", "Too many skill files", 413);
  // A single named archive can also be sent as a one-element files[] array.
  const bool single = part.filename.find('/') == std::string::npos;
  if (zip_ || (single && !input_.files.empty()))
    return Fail("files", "Expected one ZIP or one directory");
  zip_ = single;
  if (!zip_) input_.files.push_back({part.filename, {}});
  return {};
}
Status SkillUpload::Data(std::string_view bytes) {
  if (current_default_) {
    if (bytes.size() > 5 - option_.size())
      return Fail("default", "Expected true or false");
    option_.append(bytes);
    return {};
  }
  auto& target = zip_ ? input_.archive : input_.files.back().bytes;
  const auto cap =
      zip_ ? skill_detail::kSkillArchiveBytes : skill_detail::kSkillFileBytes;
  if (bytes.size() > cap - target.size() ||
      bytes.size() > file_detail::PackageLimits{}.max_bytes - total_)
    return Fail("files", "Skill bundle exceeds capacity", 413);
  target.append(bytes);
  total_ += bytes.size();
  return {};
}
Status SkillUpload::End() {
  if (current_default_) {
    if (option_ != "true" && option_ != "false")
      return Fail("default", "Expected true or false");
    make_default_ = option_ == "true";
  }
  return {};
}
ObjectReply SkillUpload::Feed(std::string_view bytes) {
  if (!result_.ok()) return result_;
  if (finished_) return Invalid("files", "Upload is already complete");
  auto parsed = parser_->Feed(bytes);
  if (!parsed && result_.ok()) Fail("body", parsed.message());
  return result_;
}
ObjectReply SkillUpload::Finish(SkillPackage* package, bool* make_default) {
  if (!result_.ok()) return result_;
  if (finished_ || !package || !make_default)
    return Invalid("files", "Invalid upload state");
  finished_ = true;
  auto parsed = parser_->Finish();
  if (!parsed) {
    if (result_.ok()) Fail("body", parsed.message());
    return result_;
  }
  if (input_.archive.empty() && input_.files.empty())
    return Invalid("files", "Expected skill files");
  *package = std::move(input_);
  *make_default = make_default_;
  return {};
}
ObjectReply DecodeSkillJson(const std::string& json, bool version,
                            const BinaryAccess& access, SkillPackage* package,
                            bool* make_default) {
  io::Json body;
  if (!package || !make_default ||
      !io::ParseJson(json, &body, true, kApplicationJsonLimits) ||
      !body.IsObject())
    return Invalid("body", "Expected a JSON object");
  for (const auto& [field, value] : body.object)
    if (field != "files" && (field != "default" || !version || !value.IsBool()))
      return Invalid(field, "Unexpected skill upload field");
  const auto* file = body.Find("files");
  if (file && file->IsArray() && file->array.size() == 1)
    file = &file->array.front();
  if (!file || !file->IsString())
    return Invalid("files", "Expected a base64 ZIP or multipart file parts");
  SkillPackage input;
  FileInputSource source;
  source.value = file->str;
  source.filename = "skill.zip";
  source.mime_type = "application/zip";
  source.param = "files";
  FileInputBudget budget;
  FileInputContent decoded;
  auto result = ReadLocalFileInput(nullptr, access, source, &budget, &decoded);
  if (!result.ok()) return result;
  if (decoded.bytes.empty())
    return Invalid("files", "Expected a nonempty base64 ZIP");
  input.archive = std::move(decoded.bytes);
  *make_default = body.GetBool("default");
  *package = std::move(input);
  return {};
}
}  // namespace q4t::server
