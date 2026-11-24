#include "q4t/server/responses_python.h"

#include "object_protocol.h"
#include "q4t/server/containers.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
using namespace object_detail;
Status ValidatePythonTool(const io::Json& tool, ChatPlanError* error,
                          const std::string& path) {
  auto valid = InputFields(tool, {"type", "container", "allowed_callers"},
                           &error->param, path);
  if (!valid) return valid;
  const auto* container = tool.Find("container");
  if (container && container->IsString() &&
      container->str.starts_with("cntr_") && ValidId(container->str))
    return {};
  if (!container || !container->IsObject() ||
      container->GetString("type") != "auto") {
    *error = {"invalid_request", path + ".container", 400};
    return Status::Fail("Expected a container ID or auto configuration");
  }
  valid = InputFields(*container,
                      {"type", "file_ids", "memory_limit", "network_policy"},
                      &error->param, path + ".container");
  if (!valid) return valid;
  auto create = *container;
  std::erase_if(create.object, [](const auto& p) { return p.first == "type"; });
  Set(&create, "name", String("code-interpreter"));
  return ValidateContainer(create, ContainerShape::kCreate, error);
}
io::Json PythonToolFunction() {
  return Object(
      {{"type", String("function")},
       {"function",
        Object({{"name", String(kPythonToolName)},
                {"description",
                 String("Execute Python 3 in the selected container. Read and "
                        "write project files under "
                        "/mnt/data (the working directory) and read mounted "
                        "skills under /mnt/skills. "
                        "Use print() to return the calculated result.")},
                {"strict", Boolean(false)},
                {"parameters",
                 Object({{"type", String("object")},
                         {"properties",
                          Object({{"code",
                                   Object({{"type", String("string")}})}})},
                         {"required", Array({String("code")})},
                         {"additionalProperties", Boolean(false)}})}})}});
}
}  // namespace q4t::server
