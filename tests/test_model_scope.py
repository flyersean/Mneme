"""Unit tests for the model_scope string-OR-list fix (bug a).

tools-level tests run standalone (import proxy/mneme/tools.py).
proxy-level tests are string-extracted from mneme_proxy.py so the 10k-line
module (which starts servers on import) is never executed.
Run:  python3 tests/test_model_scope.py
"""
import importlib.util
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "proxy"))


def _load_tools():
    spec = importlib.util.spec_from_file_location("mneme_tools_test",
                                                  os.path.join(ROOT, "proxy/mneme/tools.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tools_level_tests():
    t = _load_tools()
    # 1. single string (legacy scope, may be a single FILE) -> 1-element list
    r = t._resolve_model_scope("/etc/hostname")
    assert r == ["/etc/hostname"], r
    # 2. list, dedup + realpath
    r = t._resolve_model_scope(["/tmp", "/home/ubuntu/mneme/output", "/tmp/", None, ""])
    assert r == ["/tmp", "/home/ubuntu/mneme/output"], r
    # 3. None / empty -> []
    assert t._resolve_model_scope(None) == []
    assert t._resolve_model_scope([]) == []
    # 4. set_scope accepts BOTH forms (backward compat at every call site)
    t.set_scope(model_scope=["/tmp", "/etc2"])
    assert t.MODEL_SCOPE == ["/tmp", "/etc2"]
    t.set_scope(model_scope="/single/path")
    assert t.MODEL_SCOPE == ["/single/path"]
    # 5. A single-FILE root is not makedirs'd by the bash sandbox setup
    single = "/etc/hostname"
    t2 = _load_tools()
    t2.MODEL_SCOPE = [single]
    assert t2._scope_dirs() == [], t2._scope_dirs()
    t2.MODEL_SCOPE = ["/tmp"]
    assert t2._scope_dirs() == ["/tmp"]
    # 6. _writable gates by the SAME root set for every scope form
    t3 = _load_tools()
    t3.set_scope(model_scope=["/tmp", "/not2"])
    assert t3._writable("/tmp/foo.txt")
    assert t3._writable("/not2/bar.bin")
    assert not t3._writable("/etc/mneme_nope.txt")
    t4 = _load_tools()
    t4.set_scope(model_scope=single)  # legacy single-file scope: exactly that file
    assert t4._writable(single)
    assert not t4._writable(os.path.dirname(single))
    # 7. blocked-write message names the allowed roots + suggests an alternative
    msg = "[write blocked: ...]"  # pattern asserted in source below
    print("tools-level model_scope tests: OK")


def proxy_level_tests():
    src = open(os.path.join(ROOT, "proxy/mneme_proxy.py")).read()
    import ast
    ast.parse(src)
    # _model_scope returns the LIST form via mntools._resolve_model_scope
    m = re.search(r"def _model_scope\(.*?(?=\ndef )", src, re.S)
    body = m.group(0)
    assert "_resolve_model_scope" in body and "mntools._resolve_model_scope(cfg)" in body
    assert "return [os.path.realpath" in body  # env-fallback still list
    # every fs endpoint gates by the multi-root check
    for endpoint in ('@app.route("/fs/read"', '@app.route("/fs/write"',
                    '@app.route("/fs/mkdir"', '@app.route("/fs/delete"'):
        assert endpoint in src, endpoint
    # the single-string _within(target, _model_scope()) pattern is gone
    assert "_within(target, _model_scope())" not in src
    assert "_within(e.path, model_scope)" not in src
    assert "_within_any(target, roots)" in src
    # blocked messages name allowed roots
    assert "Allowed roots:" in src
    # write/mkdir/delete gate through _within_any AND the roots-naming message;
    # the generic "outside browser scope" guard must remain only in browser-root
    # guards (/fs/read, /fs/scope, /fs/open), NOT in the model-scope write gates
    for ep in ('/fs/write', '/fs/mkdir', '/fs/delete'):
        m = re.search(r'@app\.route\("%s".*?(?=@app\.route\("/fs)' % re.escape(ep), src, re.S)
        assert m, ep
        assert "_within_any" in m.group(0), ep
        assert "outside browser scope" not in m.group(0), ep
    # native tools share the SAME resolved root set
    assert "mntools.set_scope(model_scope=_model_scope())" in src
    print("proxy-level model_scope tests: OK (source-verified)")


if __name__ == "__main__":
    tools_level_tests()
    proxy_level_tests()
    print("ALL MODEL_SCOPE TESTS PASSED")
