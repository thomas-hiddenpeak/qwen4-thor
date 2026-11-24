#include "skill_manifest.h"

#include <dlfcn.h>
#include <locale.h>
#include <wchar.h>
#include <wctype.h>

#include <set>

#include "../../third_party/libyaml/yaml.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server::skill_detail {
namespace {
using object_detail::Error;
struct Runtime {
  decltype(&yaml_parser_initialize) init = nullptr;
  decltype(&yaml_parser_delete) close = nullptr;
  decltype(&yaml_parser_set_input_string) input = nullptr;
  decltype(&yaml_parser_load) load = nullptr;
  decltype(&yaml_document_get_node) node = nullptr;
  decltype(&yaml_document_get_root_node) root = nullptr;
  decltype(&yaml_document_delete) release = nullptr;
  locale_t locale = nullptr;
};
Runtime& Api() {
  static Runtime api;
  return api;
}
ObjectReply Invalid() {
  return Error(400, "invalid_skill_manifest", "Invalid SKILL.md front matter");
}
ObjectReply Unavailable() {
  return Error(503, "skill_manifest_codec_unavailable",
               "Skill manifest parser is unavailable");
}
std::string_view Scalar(const yaml_node_t* node) {
  if (!node || node->type != YAML_SCALAR_NODE ||
      std::string_view(reinterpret_cast<const char*>(node->tag)) !=
          YAML_STR_TAG)
    return {};
  return {reinterpret_cast<const char*>(node->data.scalar.value),
          node->data.scalar.length};
}
bool Text(std::string_view value, size_t limit, bool skill_name = false) {
  if (value.empty() ||
      (skill_name && (value.front() == '-' || value.back() == '-' ||
                      value.find("--") != value.npos)))
    return false;
  std::mbstate_t state{};
  size_t chars = 0;
  bool nonspace = false;
  while (!value.empty()) {
    wchar_t point;
    const auto used = std::mbrtowc(&point, value.data(), value.size(), &state);
    if (!used || used > value.size() || ++chars > limit) return false;
    if (skill_name && point != '-' &&
        (!iswalnum_l(point, Api().locale) ||
         towlower_l(point, Api().locale) != static_cast<wint_t>(point)))
      return false;
    nonspace |= !iswspace_l(point, Api().locale);
    value.remove_prefix(used);
  }
  return nonspace;
}

bool FrontMatter(std::string_view markdown, std::string_view* output) {
  // Accept LF/CRLF delimiters at column zero. Indented dashes inside a YAML
  // block scalar belong to the description.
  auto line = [&](size_t start, size_t* next) {
    const auto end = markdown.find('\n', start);
    *next = end == markdown.npos ? markdown.size() : end + 1;
    auto content =
        markdown.substr(start, end == markdown.npos ? end : end - start);
    if (content.ends_with('\r')) content.remove_suffix(1);
    return content;
  };
  if (markdown.starts_with("\xef\xbb\xbf")) markdown.remove_prefix(3);
  size_t start;
  if (line(0, &start) != "---") return false;
  for (size_t pos = start; pos < markdown.size();) {
    size_t next;
    if (line(pos, &next) == "---") {
      *output = markdown.substr(start, pos - start);
      return true;
    }
    pos = next;
  }
  return false;
}
bool Metadata(yaml_document_t* doc, const yaml_node_t* mapping) {
  if (!mapping || mapping->type != YAML_MAPPING_NODE) return false;
  std::set<std::string_view> seen;
  for (auto p = mapping->data.mapping.pairs.start;
       p != mapping->data.mapping.pairs.top; ++p) {
    const auto key = Scalar(Api().node(doc, p->key));
    const auto* node = Api().node(doc, p->value);
    if (key.empty() || !seen.insert(key).second || !node ||
        node->type != YAML_SCALAR_NODE ||
        std::string_view(reinterpret_cast<const char*>(node->tag)) !=
            YAML_STR_TAG)
      return false;
  }
  return true;
}
}  // namespace

ObjectReply LoadManifestRuntime() {
  auto& api = Api();
  if (api.locale) return {};
  void* handle = dlopen(Q4T_SYSTEM_SKILL_YAML_LIBRARY,
                        RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!handle) return Unavailable();
  ScopeExit close([&] { dlclose(handle); });
#define Q4T_LOAD(member, symbol)                                               \
  api.member = reinterpret_cast<decltype(api.member)>(dlsym(handle, #symbol)); \
  if (!api.member) return Unavailable()
  Q4T_LOAD(init, yaml_parser_initialize);
  Q4T_LOAD(close, yaml_parser_delete);
  Q4T_LOAD(input, yaml_parser_set_input_string);
  Q4T_LOAD(load, yaml_parser_load);
  Q4T_LOAD(node, yaml_document_get_node);
  Q4T_LOAD(root, yaml_document_get_root_node);
  Q4T_LOAD(release, yaml_document_delete);
#undef Q4T_LOAD
  api.locale = newlocale(LC_CTYPE_MASK, "C.UTF-8", nullptr);
  // Both LibYAML metadata checks and libarchive filename conversion use
  // this dedicated worker thread. Select UTF-8 before sandboxing and keep
  // it active through the last archive header.
  return api.locale && uselocale(api.locale) ? ObjectReply{} : Unavailable();
}

ObjectReply ReadManifest(std::string_view folder, std::string_view markdown,
                         Manifest* output) {
  auto& api = Api();
  if (!output || !api.locale) return Unavailable();
  if (!Text(markdown, markdown.size())) return Invalid();
  std::string_view header;
  if (!FrontMatter(markdown, &header)) return Invalid();
  yaml_parser_t parser;
  if (!api.init(&parser)) return Unavailable();
  ScopeExit close([&] { api.close(&parser); });
  api.input(&parser, reinterpret_cast<const unsigned char*>(header.data()),
            header.size());
  yaml_document_t document;
  if (!api.load(&parser, &document)) return Invalid();
  ScopeExit cleanup([&] { api.release(&document); });
  auto* mapping = api.root(&document);
  if (!mapping || mapping->type != YAML_MAPPING_NODE) return Invalid();
  Manifest result;
  std::set<std::string_view> seen;
  for (auto p = mapping->data.mapping.pairs.start;
       p != mapping->data.mapping.pairs.top; ++p) {
    const auto key = Scalar(api.node(&document, p->key));
    if (!seen.insert(key).second) return Invalid();
    const auto* value = api.node(&document, p->value);
    const auto text = Scalar(value);
    if (key == "name") {
      if (text != folder || !Text(text, 64, true)) return Invalid();
      result.name = text;
    } else if (key == "description") {
      if (!Text(text, 1024)) return Invalid();
      result.description = text;
    } else if (key == "metadata") {
      if (!Metadata(&document, value)) return Invalid();
    } else if (key == "compatibility") {
      if (!Text(text, 500)) return Invalid();
    } else if ((key != "license" && key != "allowed-tools") || text.empty()) {
      return Invalid();
    }
  }
  if (result.name.empty() || result.description.empty()) return Invalid();
  yaml_document_t trailing;
  if (!api.load(&parser, &trailing)) return Invalid();
  ScopeExit cleanup_trailing([&] { api.release(&trailing); });
  if (api.root(&trailing)) return Invalid();
  *output = std::move(result);
  return {};
}
}  // namespace q4t::server::skill_detail
