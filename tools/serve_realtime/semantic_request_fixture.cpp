// Development-only export of the production semantic classifier request.
#include <cstdio>

#include "q4t/server/output_schema.h"
#include "q4t/server/realtime_semantic_score.h"

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  q4t::io::Json request;
  if (!q4t::server::BuildRealtimeSemanticRequest(
          "qwen3.8-flash-next", argv[1], &request))
    return 1;
  const auto encoded = q4t::server::OutputJson(request);
  return std::printf("%s\n", encoded.c_str()) < 0;
}
