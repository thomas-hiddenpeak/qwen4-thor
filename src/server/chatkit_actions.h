#pragma once

#include "chatkit_credentials.h"

namespace q4t::server::chatkit_detail {
// Bind project-owned action code when the session is issued. The browser
// submits action names and data; the executable source comes from the grant.
ObjectReply BindActionFile(ObjectStore& objects, const StateAccess& access,
                           io::Json* workflow);
ObjectReply OpenWorkflow(ObjectStore& objects, const StateAccess& access,
                         const Grant& grant, io::Json* definition);
// Stream handlers may emit provisional events before the atomic thread update.
// The returned terminal events follow its successful commit.
ObjectReply DispatchAction(
    ObjectStore& objects, const StateAccess& access, const Grant& grant,
    const io::Json& params, bool streaming,
    const std::function<bool(const io::Json&)>& emit = {});
}  // namespace q4t::server::chatkit_detail
