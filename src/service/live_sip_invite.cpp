#include "live_sip_invite.h"

#include <sofia-sip/msg.h>
#include <sofia-sip/sip_header.h>

namespace q4t::service {
namespace {

bool EqualAscii(std::string_view value, std::string_view lower) {
  if (value.size() != lower.size()) return false;
  for (size_t i = 0; i < value.size(); ++i) {
    char c = value[i];
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c != lower[i]) return false;
  }
  return true;
}

bool CopyHeaders(msg_t* message, msg_pub_t* pub) {
  // The transaction layer later adds received/rport to Via. Snapshot the
  // originally parsed field lines at the header/body boundary. A folded line
  // or comma-separated value belongs to its original physical header.
  std::string saved;
  for (auto* field = pub->msg_request->sh_succ; field; field = field->sh_succ) {
    if (!field->sh_len) continue;
    std::string_view wire(static_cast<const char*>(field->sh_data),
                          field->sh_len);
    auto colon = wire.find(':');
    if (colon == wire.npos || wire.find('\0') != wire.npos) return false;
    auto name = wire.substr(0, colon);
    const auto last = name.find_last_not_of(" \t");
    if (last == name.npos) return false;
    name = name.substr(0, last + 1);
    if (EqualAscii(name, "authorization") ||
        EqualAscii(name, "proxy-authorization"))
      continue;
    saved.append(wire);
  }
  if (saved.empty()) return false;
  pub->msg_user = su_strdup(msg_home(message), saved.c_str());
  return pub->msg_user != nullptr;
}

issize_t ExtractBody(msg_t* message, msg_pub_t* pub, char* data, isize_t size,
                     int eos) {
  auto* sip = reinterpret_cast<sip_t*>(pub);
  if (sip->sip_request && sip->sip_request->rq_method == sip_method_invite &&
      !pub->msg_user && !CopyHeaders(message, pub))
    return -1;
  return sip_default_mclass()->mc_extract_body(message, pub, data, size, eos);
}
}  // namespace

msg_mclass_t* CreateSipInviteParser() {
  auto* parser = msg_mclass_clone(sip_default_mclass(), 0, msg_mclass_copy);
  if (parser) parser->mc_extract_body = ExtractBody;
  return parser;
}

std::optional<std::string_view> SipInviteHeaders(const sip_t* sip) {
  if (!sip->sip_user) return std::nullopt;
  return static_cast<const char*>(sip->sip_user);
}

std::optional<std::string> SipInviteOffer(const sip_t* sip) {
  std::string offer;
  for (auto* chunk = sip->sip_payload; chunk; chunk = chunk->pl_next)
    offer.append(chunk->pl_data, chunk->pl_len);
  if (!offer.empty() &&
      (!sip->sip_content_type ||
       !EqualAscii(sip->sip_content_type->c_type, "application/sdp")))
    return std::nullopt;
  return offer;
}

}  // namespace q4t::service
