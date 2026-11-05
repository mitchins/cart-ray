using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [(name = "main", worker = (
    modules = [(name = "worker.py", pythonModule = embed "worker.py")],
    compatibilityDate = "2026-08-31",
    compatibilityFlags = ["python_workers", "disable_python_external_sdk"]
  ))],
  sockets = [(name = "http", address = "127.0.0.1:8789", http = (), service = "main")]
);
