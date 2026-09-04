"""Standalone stdlib-only DSL worker; stdin contains public training facts only."""
import hashlib
import importlib.util
import json
import signal
import sys
import types

signal.alarm(25)
# factor_ast shares the application's canonical hashing contract without copying
# the production application package (which could import configuration/DB code).
package = types.ModuleType("application")
results = types.ModuleType("application.results")
results.payload_hash = lambda value: hashlib.sha256(json.dumps(
    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
sys.modules["application"] = package
sys.modules["application.results"] = results
spec = importlib.util.spec_from_file_location("factor_ast", "/worker/factor_ast.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
request = json.loads(sys.stdin.read(2_000_001))
print(json.dumps({"values": module.evaluate(request["ast"], request["fields"])}, allow_nan=False))
