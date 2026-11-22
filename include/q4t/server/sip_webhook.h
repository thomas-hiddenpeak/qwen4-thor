#pragma once

namespace q4t::server {
// Isolated private recipient for an authenticated trunk. No model is loaded.
int MaybeRunSipWebhookWorker(int argc, char** argv);
}  // namespace q4t::server
