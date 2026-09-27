// Bounded, connection-close HTTP request reader for the runner.
#pragma once

#include <string>

namespace q4t::server {

// Supports HTTP/1.0 and HTTP/1.1 origin-form requests with Content-Length.
// Rejects transfer encoding, Expect, duplicate framing/Host, malformed fields,
// headers over 64 KiB and bodies over 16 MiB. The caller owns socket timeouts
// and closes the connection after one response; pipelining is not supported.
bool ReadHttpRequest(int fd, std::string* method, std::string* path,
                     std::string* body);

}  // namespace q4t::server
