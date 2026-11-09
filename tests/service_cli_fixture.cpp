#include <cstring>

#include "q4t/service/service_cli.h"

int main(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "service") != 0) return 2;
  return q4t::service::RunService(argc, argv);
}
