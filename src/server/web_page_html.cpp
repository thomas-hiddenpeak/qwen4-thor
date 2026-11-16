#include "web_page_internal.h"

#include <dlfcn.h>
#include <libxml/HTMLparser.h>

#include <algorithm>
#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"

namespace q4t::server::web_detail {
namespace {
using namespace object_detail;
ObjectReply Invalid() {
  return Error(422, "web_page_invalid", "Web page text cannot be decoded");
}
ObjectReply Capacity() {
  return Error(413, "web_page_capacity", "Web page extraction limit exceeded");
}
bool Space(unsigned char c) {
  return c == ' ' || c == '\n' || c == '\r' || c == '\t' || c == '\f';
}
bool Append(std::string_view value, size_t limit, std::string* output) {
  for (unsigned char c : value) {
    if (Space(c)) {
      if (output->empty() || output->back() == ' ') continue;
      c = ' ';
    }
    if (output->size() == limit) return false;
    output->push_back(c);
  }
  return true;
}
void Trim(std::string* value) {
  if (!value->empty() && value->back() == ' ') value->pop_back();
}
std::string_view Name(const xmlNode* node) {
  return node->name ? reinterpret_cast<const char*>(node->name) : "";
}
std::string Attribute(const xmlNode* node, std::string_view name) {
  for (auto* attr = node->properties; attr; attr = attr->next) {
    if (!attr->name || name != reinterpret_cast<const char*>(attr->name))
      continue;
    std::string value;
    for (auto* text = attr->children; text; text = text->next) {
      if (text->type != XML_TEXT_NODE || !text->content) continue;
      const std::string_view part(reinterpret_cast<const char*>(text->content));
      if (part.size() > 8192 - value.size()) return {};
      value += part;
    }
    return value;
  }
  return {};
}
bool Has(const xmlNode* node, std::string_view name) {
  for (auto* attr = node->properties; attr; attr = attr->next)
    if (attr->name && name == reinterpret_cast<const char*>(attr->name))
      return true;
  return false;
}
bool Hidden(const xmlNode* node) {
  const auto name = Name(node);
  if (name == "script" || name == "style" || name == "template" ||
      name == "noscript" || name == "iframe" || name == "object" ||
      name == "svg" || name == "canvas" || Has(node, "hidden") ||
      http::Lower(Attribute(node, "aria-hidden")) == "true")
    return true;
  auto style = http::Lower(Attribute(node, "style"));
  std::erase_if(style, [](unsigned char c) { return Space(c); });
  return style.find("display:none") != style.npos ||
         style.find("visibility:hidden") != style.npos;
}
bool Block(std::string_view name) {
  for (const auto block :
       {"p",          "div",   "br",      "hr",      "li",     "ul",     "ol",
        "tr",         "td",    "th",      "h1",      "h2",     "h3",     "h4",
        "h5",         "h6",    "article", "section", "header", "footer", "pre",
        "blockquote", "table", "dl",      "dt",      "dd"})
    if (name == block) return true;
  return false;
}
std::string Resolve(const file_detail::Url& base, std::string_view value) {
  if (value.empty() || value.front() == '#') return {};
  file_detail::Url next;
  return file_detail::Redirect(base, http::Trim(value), &next) ? next.Text()
                                                               : "";
}
std::string Charset(std::string_view mime) {
  const auto lower = http::Lower(mime);
  const auto at = lower.find("charset=");
  if (at == lower.npos) return {};
  auto value = http::Trim(mime.substr(at + 8));
  value = value.substr(0, value.find(';'));
  if (value.size() >= 2 && (value.front() == '\'' || value.front() == '"') &&
      value.back() == value.front())
    value = value.substr(1, value.size() - 2);
  if (value.empty() || value.size() > 64 ||
      std::any_of(value.begin(), value.end(), [](unsigned char c) {
        return !((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                 (c >= '0' && c <= '9') || c == '-' || c == '_');
      }))
    return "invalid-encoding";
  return std::string(value);
}
xmlParserInputPtr NoExternal(const char*, const char*, xmlParserCtxtPtr) {
  return nullptr;
}
struct HtmlRuntime {
  bool ready = false;
  decltype(&htmlReadMemory) read = nullptr;
  decltype(&xmlFreeDoc) free = nullptr;
};
HtmlRuntime& Runtime() {
  static HtmlRuntime value;
  return value;
}
}  // namespace

ObjectReply LoadPageRuntime(std::string_view mime) {
  const auto type = http::Lower(http::Trim(mime.substr(0, mime.find(';'))));
  if (type != "text/html" && type != "application/xhtml+xml") return {};
  auto& api = Runtime();
  if (api.ready) return {};
  // Exec-child initialization before sandbox entry and source-byte parsing.
  void* handle =
      dlopen(Q4T_SYSTEM_XML_LIBRARY, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!handle)
    return Error(503, "web_page_codec_unavailable", "HTML codec unavailable");
#define Q4T_HTML(symbol)                                           \
  auto symbol##_fn =                                               \
      reinterpret_cast<decltype(&symbol)>(dlsym(handle, #symbol)); \
  if (!symbol##_fn)                                                \
  return Error(503, "web_page_codec_unavailable", "HTML codec unavailable")
  Q4T_HTML(htmlReadMemory);
  Q4T_HTML(xmlFreeDoc);
  Q4T_HTML(xmlInitParser);
  Q4T_HTML(xmlSetExternalEntityLoader);
#undef Q4T_HTML
  xmlInitParser_fn();
  xmlSetExternalEntityLoader_fn(NoExternal);
  const auto charset = Charset(mime);
  auto* warmup = htmlReadMemory_fn(
      "<p></p>", 7, nullptr, charset.empty() ? nullptr : charset.c_str(),
      HTML_PARSE_NOERROR | HTML_PARSE_NOWARNING | HTML_PARSE_NONET);
  if (!warmup) return Invalid();
  xmlFreeDoc_fn(warmup);
  api.read = htmlReadMemory_fn;
  api.free = xmlFreeDoc_fn;
  api.ready = true;
  return {};
}

ObjectReply DecodePage(const FileInputContent& input, std::string_view raw_url,
                       const WebPageLimits& limits, WebPage* output) {
  *output = {};
  file_detail::Url base;
  if (!ValidWebPageLimits(limits) || !file_detail::ParseUrl(raw_url, &base))
    return Error(500, "web_page_state_invalid", "Invalid web page owner");
  if (input.bytes.size() > limits.max_source_bytes) return Capacity();
  if (input.bytes.find('\0') != input.bytes.npos) return Invalid();
  WebPage next;
  next.url = base.Text();
  next.source_sha256 = Digest(input.bytes);
  const auto mime = http::Lower(http::Trim(
      std::string_view(input.mime_type).substr(0, input.mime_type.find(';'))));
  if (mime == "text/plain" || mime == "text/markdown") {
    if (detail::NormalizeUtf8(input.bytes) != input.bytes) return Invalid();
    if (input.bytes.size() > limits.max_text_bytes) return Capacity();
    next.text = input.bytes;
    *output = std::move(next);
    return {};
  }
  if (mime != "text/html" && mime != "application/xhtml+xml")
    return Error(415, "web_page_media_type", "Expected HTML or text web page");
  const auto& api = Runtime();
  if (!api.ready)
    return Error(503, "web_page_codec_unavailable", "HTML codec unavailable");
  const auto charset = Charset(input.mime_type);
  auto* document =
      api.read(input.bytes.data(), input.bytes.size(), next.url.c_str(),
               charset.empty() ? nullptr : charset.c_str(),
               HTML_PARSE_RECOVER | HTML_PARSE_NOERROR | HTML_PARSE_NOWARNING |
                   HTML_PARSE_NONET | HTML_PARSE_COMPACT);
  if (!document) return Invalid();
  ScopeExit free([&] { api.free(document); });
  struct Cursor {
    xmlNode* node;
    size_t depth;
    bool hidden, head, title, closing;
  };
  std::vector<Cursor> stack;
  if (document->children)
    stack.push_back({document->children, 1, false, false, false, false});
  std::set<std::string> links, images;
  size_t nodes = 0;
  bool have_base = false;
  while (!stack.empty()) {
    const auto cursor = stack.back();
    stack.pop_back();
    auto* node = cursor.node;
    const auto name = Name(node);
    if (cursor.closing) {
      if (!cursor.hidden && !cursor.head && Block(name) &&
          !Append(" ", limits.max_text_bytes, &next.text))
        return Capacity();
      continue;
    }
    if (++nodes > limits.max_nodes || cursor.depth > limits.max_depth)
      return Capacity();
    if (node->next)
      stack.push_back({node->next, cursor.depth, cursor.hidden, cursor.head,
                       cursor.title, false});
    if (node->type == XML_TEXT_NODE || node->type == XML_CDATA_SECTION_NODE) {
      if (!cursor.hidden && node->content) {
        const auto text = reinterpret_cast<const char*>(node->content);
        if (cursor.title) {
          if (!Append(text, 4096, &next.title)) return Capacity();
        } else if (!cursor.head &&
                   !Append(text, limits.max_text_bytes, &next.text))
          return Capacity();
      }
      continue;
    }
    if (node->type != XML_ELEMENT_NODE) continue;
    const bool hidden = cursor.hidden || Hidden(node);
    const bool head = cursor.head || name == "head";
    const bool title = cursor.title || name == "title";
    if (!hidden) {
      if (name == "base" && !have_base) {
        const auto resolved = Resolve(base, Attribute(node, "href"));
        if (!resolved.empty()) {
          (void)file_detail::ParseUrl(resolved, &base);
          have_base = true;
        }
      }
      if (name == "a") {
        auto link = Resolve(base, Attribute(node, "href"));
        if (!link.empty() && links.insert(link).second) {
          if (next.links.size() == limits.max_links) return Capacity();
          next.links.push_back(std::move(link));
        }
      }
      if (name == "img") {
        auto link = Resolve(base, Attribute(node, "src"));
        if (!link.empty() && images.insert(link).second) {
          if (next.images.size() == limits.max_images) return Capacity();
          auto caption = Attribute(node, "alt");
          if (caption.empty()) caption = Attribute(node, "title");
          if (caption.size() > 4096) return Capacity();
          next.images.push_back({std::move(link), std::move(caption)});
        }
      }
      if (!head && Block(name) &&
          !Append(" ", limits.max_text_bytes, &next.text))
        return Capacity();
    }
    stack.push_back({node, cursor.depth, hidden, head, title, true});
    if (node->children)
      stack.push_back(
          {node->children, cursor.depth + 1, hidden, head, title, false});
  }
  Trim(&next.title);
  Trim(&next.text);
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server::web_detail
