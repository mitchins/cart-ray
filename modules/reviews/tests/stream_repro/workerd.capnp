using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [(name = "main", worker = (
    modules = [(name = "worker.mjs", esModule = embed "worker.mjs")],
    compatibilityDate = "2026-08-28"
  ))],
  sockets = [(name = "http", address = "127.0.0.1:8789", http = (), service = "main")]
);
