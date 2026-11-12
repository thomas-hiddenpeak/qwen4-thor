#include "file_fetch_internal.h"

#include <algorithm>
#include <charconv>
#include <cstring>
#include <set>

#include "object_protocol.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace file_detail {
namespace {
using namespace object_detail;
using service::ingress::Address;
using service::ingress::Network;
bool Host(std::string_view host) {
  if (host.empty() || host.size() > 253) return false;
  if (Address::Parse(std::string(host)).family != AF_UNSPEC) return true;
  if (host.back() == '.') host.remove_suffix(1);
  while (!host.empty()) {
    const auto dot = host.find('.');
    const auto label = host.substr(0, dot);
    if (label.empty() || label.size() > 63 || label.front() == '-' ||
        label.back() == '-')
      return false;
    for (unsigned char c : label)
      if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-'))
        return false;
    if (dot == host.npos) return true;
    host.remove_prefix(dot + 1);
  }
  return false;
}
bool In(const Address& address, const char* cidr) {
  Network network;
  return network.Parse(cidr) && network.Contains(address);
}
std::string RemoveDots(std::string path) {
  // RFC 3986 path normalization, only for resolving a redirect reference.
  std::string output;
  while (!path.empty()) {
    if (path.starts_with("../"))
      path.erase(0, 3);
    else if (path.starts_with("./"))
      path.erase(0, 2);
    else if (path.starts_with("/./"))
      path.erase(0, 2);
    else if (path == "/.")
      path = "/";
    else if (path.starts_with("/../") || path == "/..") {
      path.replace(0, 3, "");
      if (path.empty()) path = "/";
      const auto slash = output.rfind('/');
      output.erase(slash == output.npos ? 0 : slash);
    } else if (path == "." || path == "..")
      path.clear();
    else {
      const auto slash = path.find('/', path.front() == '/' ? 1 : 0);
      output += path.substr(0, slash);
      path.erase(0, slash == path.npos ? path.size() : slash);
    }
  }
  return output;
}
}  // namespace

bool ParseUrl(std::string_view value, Url* out) {
  if (!out) return false;
  *out = {};
  if (value.empty() || value.size() > 8192) return false;
  for (unsigned char c : value)
    if (c <= 32 || c >= 127 || c == '\\') return false;
  const auto scheme = value.find("://");
  if (scheme == value.npos) return false;
  Url next;
  next.scheme = http::Lower(value.substr(0, scheme));
  if (next.scheme != "http" && next.scheme != "https") return false;
  next.tls = next.scheme == "https";
  next.port = next.tls ? 443 : 80;
  value.remove_prefix(scheme + 3);
  const auto end = value.find_first_of("/?#");
  auto authority = value.substr(0, end);
  if (authority.empty() || authority.find('@') != authority.npos ||
      authority.find('%') != authority.npos)
    return false;
  std::string_view host, port;
  if (authority.front() == '[') {
    const auto close = authority.find(']');
    if (close == authority.npos) return false;
    host = authority.substr(1, close - 1);
    if (Address::Parse(std::string(host)).family != AF_INET6) return false;
    if (close + 1 != authority.size()) {
      if (authority[close + 1] != ':') return false;
      port = authority.substr(close + 2);
      if (port.empty()) return false;
    }
  } else {
    const auto colon = authority.find(':');
    host = authority.substr(0, colon);
    if (colon != authority.npos) {
      port = authority.substr(colon + 1);
      if (port.empty()) return false;
    }
  }
  next.host = http::Lower(host);
  if (!Host(next.host)) return false;
  if (!port.empty()) {
    auto parsed =
        std::from_chars(port.data(), port.data() + port.size(), next.port);
    if (parsed.ec != std::errc{} || parsed.ptr != port.data() + port.size() ||
        next.port < 1 || next.port > 65535)
      return false;
  }
  next.authority = http::Lower(authority);
  value = end == value.npos ? std::string_view{} : value.substr(end);
  value = value.substr(0, value.find('#'));
  next.target = value.empty() || value.front() == '?' ? "/" : "";
  next.target += value;
  *out = std::move(next);
  return true;
}
bool Redirect(const Url& from, std::string_view location, Url* out) {
  if (location.empty() || location.size() > 8192) return false;
  for (unsigned char c : location)
    if (c <= 32 || c >= 127 || c == '\\') return false;
  const auto colon = location.find(':');
  if (colon != location.npos && colon < location.find_first_of("/?#") &&
      location.find("://") == location.npos)
    return false;
  location = location.substr(0, location.find('#'));
  std::string target;
  if (location.find("://") != location.npos)
    target = location;
  else if (location.starts_with("//"))
    target = from.scheme + ':' + std::string(location);
  else {
    const auto path = from.target.substr(0, from.target.find('?'));
    if (location.empty())
      target = from.target;
    else if (location.front() == '?')
      target = path + std::string(location);
    else if (location.front() == '/')
      target = location;
    else
      target = path.substr(0, path.rfind('/') + 1) + std::string(location);
    const auto query = target.find('?');
    target = RemoveDots(target.substr(0, query)) +
             (query == target.npos ? "" : target.substr(query));
    target = from.scheme + "://" + from.authority + target;
  }
  if (!ParseUrl(target, out)) return false;
  return !from.tls || out->tls;
}
bool AllowedAddress(const Address& raw, const FileFetchOptions& options) {
  auto address = raw;
  const unsigned char mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255};
  if (address.family == AF_INET6 &&
      std::memcmp(address.bytes.data(), mapped, 12) == 0) {
    std::memmove(address.bytes.data(), address.bytes.data() + 12, 4);
    address.family = AF_INET;
  }
  if (address.family != AF_INET && address.family != AF_INET6) return false;
  for (const auto& cidr : options.allowed_networks) {
    Network network;
    if (network.Parse(cidr) && network.Contains(address)) return true;
  }
  if (!options.public_networks) return false;
  if (address.family == AF_INET) {
    for (const auto* cidr :
         {"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
          "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
          "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
          "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"})
      if (In(address, cidr)) return false;
    return true;
  }
  // Conservative global unicast, excluding special/transition/documentation
  // ranges. Administrators can explicitly grant a required special network.
  return In(address, "2000::/3") && !In(address, "2001::/23") &&
         !In(address, "2001:db8::/32") && !In(address, "2002::/16") &&
         !In(address, "3fff::/20");
}
bool AllowedUrl(const Url& url, const FileFetchOptions& options) {
  if ((!url.tls && !options.allow_http) ||
      std::find(options.ports.begin(), options.ports.end(), url.port) ==
          options.ports.end())
    return false;
  auto host = url.host;
  if (host.ends_with('.')) host.pop_back();
  return options.allowed_hosts.empty() ||
         std::find(options.allowed_hosts.begin(), options.allowed_hosts.end(),
                   host) != options.allowed_hosts.end();
}
bool ValidOptions(const FileFetchOptions& options) {
  if (options.timeout_ms < 1 || options.timeout_ms > 30000 ||
      options.max_redirects < 0 || options.max_redirects > 10 ||
      options.allowed_networks.size() > 128 ||
      options.allowed_hosts.size() > 128 || options.ports.empty() ||
      options.ports.size() > 128 ||
      (!options.ca_file.empty() &&
       (options.ca_file.front() != '/' || options.ca_file.size() > 4096 ||
        options.ca_file.find('\0') != options.ca_file.npos)))
    return false;
  std::set<int> ports;
  for (int port : options.ports)
    if (port < 1 || port > 65535 || !ports.insert(port).second) return false;
  for (const auto& cidr : options.allowed_networks) {
    Network network;
    if (!network.Parse(cidr)) return false;
  }
  for (const auto& host : options.allowed_hosts)
    if (!Host(host) || host.ends_with('.')) return false;
  return true;
}
Json OptionsJson(const FileFetchOptions& options) {
  auto hosts = Array(), networks = Array(), ports = Array();
  for (const auto& value : options.allowed_hosts)
    hosts.array.push_back(String(value));
  for (const auto& value : options.allowed_networks)
    networks.array.push_back(String(value));
  for (int value : options.ports) ports.array.push_back(Number(value));
  return Object({{"public_networks", Boolean(options.public_networks)},
                 {"allow_http", Boolean(options.allow_http)},
                 {"allowed_networks", std::move(networks)},
                 {"allowed_hosts", std::move(hosts)},
                 {"ports", std::move(ports)},
                 {"ca_file", String(options.ca_file)},
                 {"timeout_ms", Number(options.timeout_ms)},
                 {"max_redirects", Number(options.max_redirects)}});
}
}  // namespace file_detail

ObjectReply ParseFileFetchOptions(const io::Json& value,
                                  FileFetchOptions* options) {
  using namespace object_detail;
  if (!options)
    return Error(500, "file_fetch_policy_invalid", "Missing policy");
  *options = {};
  std::string param;
  auto checked = InputFields(
      value,
      {"public_networks", "allow_http", "allowed_networks", "allowed_hosts",
       "ports", "ca_file", "timeout_ms", "max_redirects"},
      &param);
  auto invalid = [&] {
    auto result =
        Error(400, "file_fetch_policy_invalid", "Invalid file fetch policy");
    result.value = Object({{"param", String(param)}});
    return result;
  };
  if (!checked) return invalid();
  FileFetchOptions next;
  for (auto [name, target] :
       {std::pair{"public_networks", &next.public_networks},
        std::pair{"allow_http", &next.allow_http}}) {
    if (const auto* item = value.Find(name)) {
      param = name;
      if (!item->IsBool()) return invalid();
      *target = item->boolean;
    }
  }
  for (auto [name, target] :
       {std::pair{"allowed_networks", &next.allowed_networks},
        std::pair{"allowed_hosts", &next.allowed_hosts}}) {
    if (const auto* item = value.Find(name)) {
      param = name;
      if (!item->IsArray() || item->array.size() > 128) return invalid();
      for (const auto& entry : item->array) {
        if (!entry.IsString()) return invalid();
        target->push_back(entry.str);
      }
    }
  }
  if (const auto* item = value.Find("ports")) {
    param = "ports";
    if (!item->IsArray() || item->array.size() > 128) return invalid();
    next.ports.clear();
    for (const auto& entry : item->array) {
      const auto port = entry.AsInt(-1);
      if (port < 1 || port > 65535) return invalid();
      next.ports.push_back(port);
    }
  }
  if (const auto* item = value.Find("ca_file")) {
    param = "ca_file";
    if (!item->IsString()) return invalid();
    next.ca_file = item->str;
  }
  for (auto [name, target] :
       {std::pair{"timeout_ms", &next.timeout_ms},
        std::pair{"max_redirects", &next.max_redirects}}) {
    if (const auto* item = value.Find(name)) {
      param = name;
      const auto number = item->AsInt(-1);
      if (number < 0 || number > 30000) return invalid();
      *target = number;
    }
  }
  param = "file_fetch";
  if (!file_detail::ValidOptions(next)) return invalid();
  *options = std::move(next);
  return {};
}
}  // namespace q4t::server
