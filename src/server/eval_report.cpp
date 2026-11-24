#include "eval_runs.h"

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_request.h"

namespace q4t::server {
std::string EvalReportBase(const HttpRequestHeaders& headers, bool gateway,
                           std::string_view fallback,
                           std::string_view configured) {
  std::string_view scheme = "http";
  std::string_view host = headers.host.empty() ? fallback : headers.host;
  std::string_view prefix;
  if (!configured.empty()) {
    const auto start = configured.find("://");
    if (start == configured.npos) return {};
    scheme = configured.substr(0, start);
    configured.remove_prefix(start + 3);
    const auto slash = configured.find('/');
    host = configured.substr(0, slash);
    if (slash != configured.npos) prefix = configured.substr(slash);
  } else if (gateway && (!headers.forwarded_host.empty() ||
                         !headers.forwarded_proto.empty() ||
                         !headers.forwarded_prefix.empty())) {
    scheme = headers.forwarded_proto;
    host = headers.forwarded_host;
    prefix = headers.forwarded_prefix;
  }
  if ((scheme != "http" && scheme != "https") ||
      host.find_first_of("/?#") != host.npos || !http::ValidPathPrefix(prefix))
    return {};
  file_detail::Url url;
  if (!file_detail::ParseUrl(std::string(scheme) + "://" + std::string(host),
                             &url))
    return {};
  return url.scheme + "://" + url.authority + std::string(prefix);
}

namespace eval_detail {
namespace {
using namespace object_detail;
std::string HtmlText(std::string_view value) {
  std::string text;
  constexpr size_t kTextBytes = 1024;
  const bool shortened = value.size() > kTextBytes;
  if (shortened) {
    size_t cut = kTextBytes;
    while ((static_cast<unsigned char>(value[cut]) & 0xc0) == 0x80) --cut;
    value = value.substr(0, cut);
  }
  for (char c : value) {
    switch (c) {
      case '&': text += "&amp;"; break;
      case '<': text += "&lt;"; break;
      case '>': text += "&gt;"; break;
      case '"': text += "&quot;"; break;
      case '\'': text += "&#39;"; break;
      default: text += c;
    }
  }
  if (shortened) text += "…";
  return text;
}
}  // namespace

void ProjectRun(Json* run, std::string_view base) {
  if (run->GetString("object") != "eval.run") return;
  const auto url = std::string(base) + "/v1/evals/" + run->GetString("eval_id") +
                   "/runs/" + run->GetString("id") + "/report";
  Set(run, "report_url", String(url));
}

// Only the scoped, validated public run enters this renderer. Large sample
// payloads remain in the paginated output-items resource.
std::string RenderRunReport(const io::Json& run) {
  std::string html =
      "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
      "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
      "<meta name=\"referrer\" content=\"no-referrer\">"
      "<meta http-equiv=\"Content-Security-Policy\" "
      "content=\"default-src 'none'; base-uri 'none'; form-action 'none'\">"
      "<title>Evaluation run report</title><main><h1>Evaluation run report</h1>";
  const auto name = run.GetString("name");
  if (!name.empty()) html += "<h2>" + HtmlText(name) + "</h2>";
  html += "<dl><dt>Evaluation</dt><dd>" + HtmlText(run.GetString("eval_id")) +
          "</dd><dt>Run</dt><dd>" + HtmlText(run.GetString("id")) +
          "</dd><dt>Status</dt><dd>" + HtmlText(run.GetString("status")) +
          "</dd>";
  if (const auto* counts = run.Find("result_counts")) {
    for (auto key : {"total", "passed", "failed", "errored"})
      html += "<dt>" + std::string(key) + "</dt><dd>" +
              std::to_string(counts->GetInt(key)) + "</dd>";
  }
  html += "</dl>";
  const auto* error = run.Find("error");
  if (error && error->IsObject())
    html += "<h2>Run error</h2><p>" + HtmlText(error->GetString("message")) +
            "</p>";
  const auto* grades = run.GetArray("per_testing_criteria_results");
  if (grades && !grades->array.empty()) {
    html += "<table><caption>Results by criterion</caption><thead><tr>"
            "<th>Criterion</th><th>Passed</th><th>Failed</th></tr></thead><tbody>";
    constexpr size_t kMaxCriteria = 100;
    size_t shown = 0;
    for (const auto& grade : grades->array) {
      if (shown == kMaxCriteria) break;
      ++shown;
      html += "<tr><th scope=\"row\">" +
              HtmlText(grade.GetString("testing_criteria")) + "</th><td>" +
              std::to_string(grade.GetInt("passed")) + "</td><td>" +
              std::to_string(grade.GetInt("failed")) + "</td></tr>";
    }
    html += "</tbody></table>";
    if (shown < grades->array.size())
      html += "<p>Showing the first 100 criteria. The full run contains all results.</p>";

  }
  html += "<p><a href=\"../" + HtmlText(run.GetString("id")) +
          "\">Full run (JSON)</a></p>";
  return html + "<p><a href=\"./output_items\">Output items (JSON)</a></p>"
                "</main></html>";
}
}  // namespace eval_detail
}  // namespace q4t::server
