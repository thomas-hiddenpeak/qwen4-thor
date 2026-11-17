// Opt-in real-asset CPU qualification; separate from the public host registry.
#include "q4t/server/model_assets.h"
#include "q4t/test.h"

#include <fstream>

int ModelAssetFixture(int argc, char** argv);
int main(int argc, char** argv) {
  const int worker = q4t::server::MaybeRunModelAssetWorker(argc, argv);
  if (worker >= 0) return worker;
  const int fixture = ModelAssetFixture(argc, argv);
  if (fixture >= 0) return fixture;
  if (argc == 2 && std::string_view(argv[1]) == "--list") {
    for (const auto& c : q4t::test::Registry())
      std::printf("%s\n", c.name.c_str());
    return q4t::test::Registry().empty() ? 2 : 0;
  }
  // Full checkpoint reads require a separately frozen lease invocation.
  if (argc != 3 || std::string_view(argv[1]) != "--required-list") return 2;
  std::ifstream input(argv[2]);
  if (!input) return 2;
  std::vector<std::string> names;
  std::string name;
  while (std::getline(input, name)) {
    if (!name.empty() && name.back() == '\r') name.pop_back();
    if (!name.empty() && !name.starts_with('#')) names.push_back(name);
  }
  return input.bad() ? 2 : q4t::test::RunTests(names);
}
