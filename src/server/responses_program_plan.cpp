#include "q4t/server/responses_program.h"

#include "q4t/server/input_contract.h"
#include "q4t/server/output_schema.h"
#include "responses_json.h"

namespace q4t::server {
using namespace responses_detail;
io::Json NativeResponsesProgramDefinition(const io::Json& active_tools,
                                          bool parallel) {
  auto catalog = Array();
  const auto add = [&](const Json& tool, const std::string& name_space) {
    if (!ResponsesToolAllows(tool, "programmatic") ||
        (tool.GetString("type") != "function" &&
         tool.GetString("type") != "custom"))
      return;
    auto entry = tool;
    if (!name_space.empty()) Set(&entry, "namespace", String(name_space));
    catalog.array.push_back(std::move(entry));
  };
  for (const auto& tool : active_tools.array) {
    if (tool.GetString("type") == "namespace") {
      if (const auto* children = tool.GetArray("tools"))
        for (const auto& child : children->array)
          add(child, tool.GetString("name"));
    } else {
      add(tool, "");
    }
  }
  const auto parameters =
      Object({{"type", String("object")},
              {"properties",
               Object({{"code", Object({{"type", String("string")},
                                        {"minLength", Number(1)},
                                        {"maxLength", Number(65536)}})}})},
              {"required", Array({String("code")})},
              {"additionalProperties", Boolean(false)}});
  return Object(
      {{"type", String("function")},
       {"function",
        Object(
            {{"name", String(kResponsesProgramModelName)},
             {"strict", Boolean(true)},
             {"parameters", parameters},
             {"description",
              String(
                  "Run JavaScript with top-level await in an isolated runtime. "
                  "Call only the following loaded tools with "
                  "tools[name](arguments) "
                  "or tools[namespace][name](arguments). Function arguments "
                  "are objects; "
                  "custom arguments are strings. Await results, then "
                  "text(value) to "
                  "emit the final result; image(dataURL) emits an image. " +
                  std::string(parallel
                                  ? "Use Promise.all for independent calls. "
                                  : "Tool calls are executed one at a time. ") +
                  "No filesystem, network, "
                  "process, console, Node, or tool search. Never invent "
                  "results. "
                  "Authorized definitions: " +
                  OutputJson(catalog))}})}});
}

Status MapResponsesProgramChoice(const Json& choice, bool enabled, Json* mapped,
                                 ChatPlanError* error) {
  *mapped = choice;
  const auto convert = [&](const Json& original, Json* result,
                           const std::string& path) -> Status {
    *result = original;
    if (original.GetString("type") != "programmatic_tool_calling") return {};
    auto status = InputFields(original, {"type"}, &error->param, path);
    if (!status) return status;
    if (!enabled) {
      *error = {"invalid_request", path, 400};
      return Status::Fail("Program choice requires its enabled tool");
    }
    *result = Object({{"type", String("function")},
                      {"name", String(kResponsesProgramModelName)}});
    return {};
  };
  if (choice.GetString("type") == "allowed_tools") {
    if (const auto* selected = choice.GetArray("tools")) {
      auto tools = Array();
      for (size_t i = 0; i < selected->array.size(); ++i) {
        Json item;
        auto status = convert(selected->array[i], &item,
                              "tool_choice.tools[" + std::to_string(i) + ']');
        if (!status) return status;
        tools.array.push_back(std::move(item));
      }
      Set(mapped, "tools", std::move(tools));
    }
    return {};
  }
  return convert(choice, mapped, "tool_choice");
}
}  // namespace q4t::server
