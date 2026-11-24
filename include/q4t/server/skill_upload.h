#pragma once

#include "q4t/server/multipart.h"
#include "q4t/server/skill_worker.h"

namespace q4t::server {
inline constexpr size_t kSkillWireBytes = (128ULL << 20) + (1ULL << 20);
class SkillUpload {
 public:
  SkillUpload(std::string_view content_type, bool version);
  ObjectReply Feed(std::string_view bytes);
  ObjectReply Finish(SkillPackage* package, bool* make_default);
  const ObjectReply& Result() const { return result_; }

 private:
  Status Begin(const MultipartPart& part);
  Status Data(std::string_view bytes);
  Status End();
  Status Fail(std::string field, std::string message, int status = 400);
  bool version_, default_seen_ = false, make_default_ = false,
                 current_default_ = false, zip_ = false;
  bool finished_ = false;
  size_t total_ = 0;
  std::string option_;
  SkillPackage input_;
  ObjectReply result_;
  std::unique_ptr<MultipartDecoder> parser_;
};
ObjectReply DecodeSkillJson(const std::string& json, bool version,
                            const BinaryAccess& access, SkillPackage* package,
                            bool* make_default);
}  // namespace q4t::server
