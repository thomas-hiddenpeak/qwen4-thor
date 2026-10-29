#include "q4t/server/http_response.h"

#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using q4t::server::detail::NormalizeUtf8;
using q4t::server::detail::Utf8Stream;

struct SocketPair {
  int fd[2]{-1, -1};
  SocketPair() { socketpair(AF_UNIX, SOCK_STREAM, 0, fd); }
  ~SocketPair() {
    for (int f : fd) if (f >= 0) close(f);
  }
};
}  // namespace

Q4T_TEST(server_utf8_stream_boundaries) {
  const std::vector<std::string> cases = {
      "", "plain\n\"text\\", "中文🚀🎉é", std::string("x\0y", 3),
      "\xc2\x80\xdf\xbf\xe0\xa0\x80\xed\x9f\xbf",
      "\xee\x80\x80\xef\xbf\xbf\xf0\x90\x80\x80\xf4\x8f\xbf\xbf"};
  for (const auto& input : cases) {
    Q4T_CHECK(NormalizeUtf8(input) == input);
    for (size_t split = 0; split <= input.size(); ++split) {
      Utf8Stream stream;
      std::string output = stream.Append(input.substr(0, split));
      output += stream.Append(input.substr(split));
      output += stream.Finish();
      Q4T_CHECK(output == input);
      Q4T_CHECK(stream.Finish().empty());
    }
    Utf8Stream stream;
    std::string output;
    for (char c : input) {
      const std::string piece = stream.Append(std::string(1, c));
      const auto event = q4t::server::detail::SseChunk(
          "test", "model", "", piece, "", 0);
      q4t::io::Json parsed;
      Q4T_CHECK(q4t::io::ParseJson(
          event.substr(6, event.size() - 10), &parsed, true).ok());
      // Every emitted piece is independently complete UTF-8.
      Q4T_CHECK(NormalizeUtf8(piece) == piece);
      output += piece;
    }
    output += stream.Finish();
    Q4T_CHECK(output == input);
  }
  return true;
}

Q4T_TEST(server_utf8_malformed_subparts) {
  const std::string r = "\xef\xbf\xbd";
  const std::vector<std::pair<std::string, std::string>> cases = {
      {"\x80", r}, {"\xc0\xaf", r + r}, {"\xff", r},
      {"\xe0\x80\x80", r + r + r},
      {"\xed\xa0\x80", r + r + r},
      {"\xf4\x90\x80\x80", r + r + r + r},
      {"\xf5\x80\x80\x80", r + r + r + r},
      {"\xc2", r}, {"\xe2\x82", r}, {"\xf0\x9f\x9a", r},
      {"\xe2\x82!", r + "!"}, {"\xf0\x9f!", r + "!"},
      {"\xe2\xc2\xa2", r + "\xc2\xa2"}};
  for (const auto& [input, expected] : cases) {
    Q4T_CHECK(NormalizeUtf8(input) == expected);
    for (size_t split = 0; split <= input.size(); ++split) {
      Utf8Stream stream;
      std::string output = stream.Append(input.substr(0, split));
      output += stream.Append(input.substr(split));
      output += stream.Finish();
      Q4T_CHECK(output == expected);
    }
  }
  return true;
}

Q4T_TEST(server_http_response_send_status) {
  SocketPair sockets;
  Q4T_CHECK(sockets.fd[0] >= 0);
  Q4T_CHECK(q4t::server::detail::SendSimple(
      sockets.fd[0], 200, "OK", "hello", "text/plain"));
  std::array<char, 1024> data{};
  const auto n = recv(sockets.fd[1], data.data(), data.size(), 0);
  Q4T_CHECK(n > 0);
  const std::string response(data.data(), static_cast<size_t>(n));
  Q4T_CHECK(response.starts_with("HTTP/1.1 200 OK\r\n"));
  Q4T_CHECK(response.find("Content-Length: 5\r\n") != std::string::npos);
  Q4T_CHECK(response.ends_with("\r\n\r\nhello"));
  shutdown(sockets.fd[1], SHUT_RDWR);
  Q4T_CHECK(!q4t::server::detail::SendSimple(
      sockets.fd[0], 200, "OK", "unsent", "text/plain"));
  return true;
}

Q4T_TEST(server_http_response_partial_writes) {
  SocketPair sockets;
  Q4T_CHECK(sockets.fd[0] >= 0);
  int buffer = 1024;
  setsockopt(sockets.fd[0], SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
  const std::string input(128 * 1024, 'x');
  std::string received;
  std::jthread reader([&] {
    std::array<char, 137> bytes;
    while (received.size() < input.size()) {
      const auto n = recv(sockets.fd[1], bytes.data(), bytes.size(), 0);
      if (n <= 0) break;
      received.append(bytes.data(), static_cast<size_t>(n));
    }
  });
  const bool sent = q4t::server::detail::WriteAll(sockets.fd[0], input);
  shutdown(sockets.fd[0], SHUT_WR);
  reader.join();
  Q4T_CHECK(sent && received == input);
  return true;
}
