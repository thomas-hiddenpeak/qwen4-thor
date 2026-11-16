#include "web_page_internal.h"

#include <set>

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server::web_detail {
using namespace object_detail;
Json Page(const WebPage& page) {
  auto links = Array(), images = Array();
  for (const auto& url : page.links) links.array.push_back(String(url));
  for (const auto& image : page.images)
    images.array.push_back(Object(
        {{"url", String(image.url)}, {"caption", String(image.caption)}}));
  return Object({{"url", String(page.url)},
                 {"title", String(page.title)},
                 {"text", String(page.text)},
                 {"source_sha256", String(page.source_sha256)},
                 {"links", std::move(links)},
                 {"images", std::move(images)}});
}
bool Text(const Json& value, const char* key, size_t limit, std::string* out) {
  const auto* text = value.Find(key);
  if (!text || !text->IsString() || text->str.size() > limit ||
      text->str.find('\0') != text->str.npos ||
      detail::NormalizeUtf8(text->str) != text->str)
    return false;
  *out = text->str;
  return true;
}
bool Page(const Json& value, const WebPageLimits& limits, WebPage* page) {
  if (!Text(value, "url", 8192, &page->url) ||
      !Text(value, "title", 4096, &page->title) ||
      !Text(value, "text", limits.max_text_bytes, &page->text) ||
      !Text(value, "source_sha256", 64, &page->source_sha256))
    return false;
  const auto* links = value.GetArray("links");
  const auto* images = value.GetArray("images");
  if (!links || !images || links->array.size() > limits.max_links ||
      images->array.size() > limits.max_images)
    return false;
  std::set<std::string> seen;
  for (const auto& link : links->array) {
    file_detail::Url parsed;
    if (!link.IsString() || !file_detail::ParseUrl(link.str, &parsed) ||
        !seen.insert(link.str).second)
      return false;
    page->links.push_back(link.str);
  }
  seen.clear();
  for (const auto& image : images->array) {
    WebImage next;
    file_detail::Url parsed;
    if (!Text(image, "url", 8192, &next.url) ||
        !file_detail::ParseUrl(next.url, &parsed) ||
        !seen.insert(next.url).second ||
        !Text(image, "caption", 4096, &next.caption))
      return false;
    page->images.push_back(std::move(next));
  }
  return true;
}
}  // namespace q4t::server::web_detail
