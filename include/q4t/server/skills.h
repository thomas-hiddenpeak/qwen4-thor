#pragma once

#include "q4t/server/responses_state.h"
#include "q4t/server/skill_worker.h"

namespace q4t::server {
enum class SkillShape { kResource, kVersion, kQuery, kVersionQuery, kUpdate };
Status ValidateSkill(const io::Json& value, SkillShape shape,
                     ChatPlanError* error);

// For creation, pass the validated archive from ProcessSkillPackage. Version
// upload and the default pointer change share one durable transaction.
ObjectReply HandleSkills(ObjectStore& objects, const StateAccess& access,
                         std::string_view method, std::string_view target,
                         const std::string& body,
                         SkillPackage* package = nullptr,
                         bool make_default = false,
                         BinaryRead* download = nullptr);
// Resolve an authenticated owner's default, latest or explicit immutable
// version and pin its bytes before using it to populate a workspace.
ObjectReply OpenSkillVersion(ObjectStore& objects, const StateAccess& access,
                             std::string_view skill, std::string_view version,
                             BinaryRead* download);
}  // namespace q4t::server
