"""Browser fragment login and stale-tab auth recovery, exercised through real Node vm."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGIN_HTML = ROOT / "dashboard" / "static" / "login.html"
APP_HTML = ROOT / "dashboard" / "static" / "app.html"

NODE_SCRIPT = r'''
const vm = require('vm');
const html = require('fs').readFileSync(0, 'utf8');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { innerHTML: '', textContent: '', value: '', disabled: false, children: [],
    classList: { add() {}, remove() {} }, addEventListener() {}, appendChild(c) { this.children.push(c); }, style: {}, querySelector() { return element('child'); }, focus() {} });
  return elements.get(id);
}
const fetches = [], storage = {}, history = [];
const location = { protocol: 'https:', host: 'example.test', origin: 'https://example.test', hash: TEST_HASH, replace() {} };
history.replaceState = () => {};
const responses = TEST_RESPONSES.slice();
const fetch = async (url, opts) => {
  fetches.push({ url, opts: Object.assign({}, opts) });
  const resp = responses.shift() || { ok: false, status: 500 };
  if (!resp.json) resp.json = async () => ({});
  return resp;
};
function setTimeout() { return 1; } function clearTimeout() {} function setInterval() { return 1; } function clearInterval() {}
const context = vm.createContext({ console, fetch, location, history,
  localStorage: { getItem: k => storage[k] ?? null, setItem: (k, v) => { storage[k] = String(v); }, removeItem: k => { delete storage[k]; } },
  sessionStorage: { getItem: k => storage['s:' + k] ?? null, setItem: (k, v) => { storage['s:' + k] = String(v); }, removeItem: k => { delete storage['s:' + k]; } },
  document: { getElementById: element, createElement: () => element(Math.random()) }, btoa: s => Buffer.from(s).toString('base64'),
  URL, Blob, Math, JSON, Promise, Date, setTimeout, clearTimeout, setInterval, clearInterval });
vm.runInContext([...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(x => x[1]).join('\n'), context);
vm.runInContext(TEST_SETUP, context);
vm.runInContext("(async () => { await new Promise(r => setTimeout(r, 0)); process.stdout.write(JSON.stringify({ fetches, storage, location, history })); })()", context);
'''

def _run(setup, html_path, hash_value="#device=abc.secret", responses=None):
    responses = responses or [{"ok": True, "token": "tok", "key": "k"}]
    script = (NODE_SCRIPT
              .replace("TEST_HASH", json.dumps(hash_value))
              .replace("TEST_RESPONSES", json.dumps(responses))
              .replace("TEST_SETUP", json.dumps(setup)))
    proc = subprocess.run(
        ["node", "-e", script],
        input=html_path.read_text(encoding="utf-8"), text=True, capture_output=True, timeout=20)
    if proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    return json.loads(proc.stdout)

class LoginTests(unittest.TestCase):
    def test_fragment_is_consumed_and_posted_as_split_fields(self):
        result = _run("", LOGIN_HTML)
        self.assertEqual(result["fetches"][0]["url"], "/api/device-login")
        body = json.loads(result["fetches"][0]["opts"]["body"])
        self.assertEqual(body, {"device_id": "abc", "secret": "secret"})
        self.assertEqual(result["location"]["hash"], "")

    def test_success_stores_fresh_session_values_and_durable_credential(self):
        result = _run("", LOGIN_HTML)
        self.assertEqual(result["storage"]["s:jarvis_token"], "tok")
        self.assertEqual(result["storage"]["s:jarvis_key"], "k")
        self.assertEqual(result["storage"]["jarvis_device_credential"], "abc.secret")

    def test_durable_401_removes_only_durable_credential(self):
        result = _run("", LOGIN_HTML, responses=[{"status": 401}])
        self.assertNotIn("jarvis_device_credential", result["storage"])

    def test_manual_pin_remains_functional_and_writes_no_durable_credential(self):
        setup = "document.getElementById('key').value = 'ABCDEF'; doLogin();"
        result = _run(setup, LOGIN_HTML, hash_value="", responses=[{"ok": True, "token": "tok", "key": "pinkey"}])
        self.assertEqual(result["fetches"][0]["url"], "/login")
        self.assertEqual(json.loads(result["fetches"][0]["opts"]["body"]), {"pin": "ABCDEF"})
        self.assertNotIn("jarvis_device_credential", result["storage"])

if __name__ == "__main__":
    unittest.main()