from pathlib import Path
import ast, re

p = Path(__file__).resolve().parents[1] / "crawler.py"
src = p.read_text(encoding="utf-8")
tree = ast.parse(src)

defs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
required = {"discover_xml","html_discovery","title_fetch","cancellation_requested"}
missing = required - defs
assert not missing, f"missing functions: {missing}"

m = re.search(r"(?ms)^async def discover_xml\(.*?(?=^async def |^def |\Z)", src)
assert m, "discover_xml body not found"
body = m.group(0)
sig = body.splitlines()[0]
param_text = sig[sig.find("(")+1:sig.rfind(")")]
params = [x.strip().split("=")[0].strip() for x in param_text.split(",")]
url_param = params[1]
if url_param != "target":
    executable_target_lines = [
        ln for ln in body.splitlines()
        if re.search(r"\btarget\b", ln)
        and not ln.strip().startswith("#")
        and '"""' not in ln
    ]
    assert not executable_target_lines, f"hardcoded target reference remains: {executable_target_lines}"

print("smoke_check: OK")
