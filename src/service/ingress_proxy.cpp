#include "q4t/service/ingress_proxy.h"

#include <algorithm>
#include <charconv>
#include <set>

#include "q4t/server/http_framing.h"
#include "q4t/service/guardian_wire.h"

namespace q4t::service::ingress {
namespace {
namespace h = server::http;

bool Prefix(std::string_view value) {
  if (value.empty()) return true;
  if (value.size() > 512 || value.front() != '/' || value.back() == '/')
    return false;
  value.remove_prefix(1);
  while (!value.empty()) {
    const auto end = value.find('/');
    const auto part = value.substr(0, end);
    if (part.empty() || part == "." || part == "..") return false;
    for (unsigned char c : part)
      if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
            c == '~'))
        return false;
    if (end == value.npos) break;
    value.remove_prefix(end + 1);
  }
  return true;
}

bool Node(std::string_view value, Address* address) {
  *address = Address::Parse(std::string(value));
  if (address->family != AF_UNSPEC) return true;
  size_t end = value.rfind(':');
  if (value.starts_with('[')) {
    end = value.find(']');
    if (end == value.npos) return false;
    *address = Address::Parse(std::string(value.substr(1, end - 1)));
    if (address->family != AF_INET6) return false;
    if (end + 1 == value.size()) return true;
    if (value[end + 1] != ':') return false;
    ++end;
  } else {
    if (end == value.npos) return false;
    *address = Address::Parse(std::string(value.substr(0, end)));
    if (address->family != AF_INET) return false;
  }
  const auto port = value.substr(end + 1);
  unsigned number = 0;
  const auto parsed =
      std::from_chars(port.data(), port.data() + port.size(), number);
  return !port.empty() && parsed.ec == std::errc{} &&
         parsed.ptr == port.data() + port.size() && number > 0 &&
         number <= 65535;
}

struct Hop {
  std::string from, host, proto;
};

bool Parameter(std::string_view* raw, std::string* value) {
  *raw = h::Trim(*raw);
  if (raw->empty()) return false;
  if (raw->front() != '"') {
    size_t count = 0;
    while (count < raw->size() && h::IsToken((*raw)[count])) ++count;
    if (!count) return false;
    *value = raw->substr(0, count);
    raw->remove_prefix(count);
    return true;
  }
  raw->remove_prefix(1);
  while (!raw->empty()) {
    unsigned char c = raw->front();
    raw->remove_prefix(1);
    if (c == '"') return true;
    if (c == '\\') {
      if (raw->empty()) return false;
      c = raw->front();
      raw->remove_prefix(1);
    }
    if ((c < 32 && c != '\t') || c == 127) return false;
    *value += c;
  }
  return false;
}

bool Forwarded(std::string_view raw, std::vector<Hop>* hops) {
  if (raw.empty()) return false;
  for (;;) {
    if (hops->size() == 32) return false;
    Hop hop;
    std::set<std::string> seen;
    for (;;) {
      raw = h::Trim(raw);
      const auto equal = raw.find('=');
      if (equal == raw.npos || equal == 0) return false;
      const auto name = h::Lower(raw.substr(0, equal));
      for (unsigned char c : name)
        if (!h::IsToken(c)) return false;
      if (!seen.insert(name).second) return false;
      raw.remove_prefix(equal + 1);
      std::string value;
      if (!Parameter(&raw, &value)) return false;
      if (name == "for") hop.from = value;
      if (name == "host") hop.host = value;
      if (name == "proto") hop.proto = value;
      raw = h::Trim(raw);
      if (raw.empty() || raw.front() == ',') break;
      if (raw.front() != ';') return false;
      raw.remove_prefix(1);
    }
    hops->push_back(std::move(hop));
    if (raw.empty()) return true;
    raw.remove_prefix(1);
  }
}

bool List(std::string_view raw, std::vector<std::string>* parts) {
  if (raw.empty()) return true;
  for (;;) {
    const auto comma = raw.find(',');
    const auto part = h::Trim(raw.substr(0, comma));
    if (part.empty() || parts->size() == 32) return false;
    parts->emplace_back(part);
    if (comma == raw.npos) return true;
    raw.remove_prefix(comma + 1);
  }
}

bool XForwarded(const server::HttpRequestHeaders& fields,
                std::vector<Hop>* hops) {
  std::vector<std::string> from, host, proto;
  if (!List(fields.forwarded_for, &from) || from.empty() ||
      !List(fields.forwarded_host, &host) ||
      !List(fields.forwarded_proto, &proto))
    return false;
  for (const auto* values : {&host, &proto})
    if (values->size() > 1 && values->size() != from.size()) return false;
  for (size_t i = 0; i < from.size(); ++i)
    hops->push_back({from[i],
                     host.empty() ? "" : host[host.size() == 1 ? 0 : i],
                     proto.empty() ? "" : proto[proto.size() == 1 ? 0 : i]});
  return true;
}
}  // namespace

bool HttpPolicy::Load(const io::Json& value) {
  if (!wire::HasKeys(value,
                     {"keep_alive_requests", "keep_alive_idle_seconds",
                      "request_timeout_seconds", "path_prefix", "proxy"}))
    return false;
  HttpPolicy result;
  uint64_t requests = 0, idle = 0, timeout = 0;
  if (!wire::GetUnsigned(value, "keep_alive_requests", &requests) ||
      requests < 1 || requests > 1000 ||
      !wire::GetUnsigned(value, "keep_alive_idle_seconds", &idle) || idle < 1 ||
      idle > 60 ||
      !wire::GetUnsigned(value, "request_timeout_seconds", &timeout) ||
      timeout < 1 || timeout > 120)
    return false;
  const auto* prefix = value.Find("path_prefix");
  if (!prefix || !prefix->IsString() || !Prefix(prefix->str)) return false;
  result.path_prefix = prefix->str;
  result.keep_alive_requests = requests;
  result.keep_alive_idle_ms = idle * 1000;
  result.request_timeout_ms = timeout * 1000;
  const auto& proxy = *value.Find("proxy");
  if (!proxy.IsNull()) {
    if (!wire::HasKeys(proxy,
                       {"trusted_cidrs", "header", "allow_stripped_prefix"}))
      return false;
    const auto* networks = proxy.GetArray("trusted_cidrs");
    const auto* strip = proxy.Find("allow_stripped_prefix");
    result.forwarding_ = proxy.GetString("header");
    if ((result.forwarding_ != "forwarded" &&
         result.forwarding_ != "x-forwarded") ||
        !strip || !strip->IsBool() ||
        (strip->boolean && result.path_prefix.empty()) || !networks ||
        networks->array.empty() || networks->array.size() > 32)
      return false;
    result.allow_stripped_prefix_ = strip->boolean;
    for (const auto& item : networks->array) {
      Network network;
      if (!item.IsString() || !network.Parse(item.str)) return false;
      for (const auto& prior : result.trusted_)
        if (prior.Contains(network.address) || network.Contains(prior.address))
          return false;
      result.trusted_.push_back(network);
    }
  }
  *this = std::move(result);
  return true;
}

bool HttpPolicy::Trusted(const Address& peer) const {
  return std::any_of(
      trusted_.begin(), trusted_.end(),
      [&](const auto& network) { return network.Contains(peer); });
}

bool HttpPolicy::Resolve(const server::HttpRequestHeaders& headers,
                         const Address& peer, const std::string& target,
                         bool tls, ResolvedRequest* output) const {
  ResolvedRequest result{peer, headers.host, tls ? "https" : "http", target,
                         false};
  if (Trusted(peer)) {
    const auto& field =
        forwarding_ == "forwarded" ? headers.forwarded : headers.forwarded_for;
    if (!field.empty()) {
      std::vector<Hop> hops;
      if (!(forwarding_ == "forwarded" ? Forwarded(field, &hops)
                                       : XForwarded(headers, &hops)))
        return false;
      for (auto hop = hops.rbegin();
           hop != hops.rend() && Trusted(result.source); ++hop) {
        if (!Node(hop->from, &result.source)) return false;
        if (!hop->host.empty()) result.host = hop->host;
        if (!hop->proto.empty()) {
          result.scheme = h::Lower(hop->proto);
          if (result.scheme != "http" && result.scheme != "https") return false;
        }
      }
      result.proxied = true;
    } else if (!headers.forwarded_host.empty() ||
               !headers.forwarded_proto.empty()) {
      return false;  // Enabled forwarding metadata without a provable for
                     // chain.
    }
    if (!headers.forwarded_prefix.empty() &&
        (!result.proxied || headers.forwarded_prefix != path_prefix))
      return false;
  }
  if (!path_prefix.empty()) {
    if (target.starts_with(path_prefix + "/")) {
      result.target.erase(0, path_prefix.size());
    } else if (!(result.proxied && allow_stripped_prefix_ &&
                 headers.forwarded_prefix == path_prefix)) {
      return false;
    }
  }
  *output = std::move(result);
  return true;
}
}  // namespace q4t::service::ingress
