"""Tests for the dependency-free repo map and symbol finder."""

import os
import shutil
import tempfile
import time
import unittest

from core.models import PermissionLevel, ToolCallRequest
from core.permissions import PermissionGate
from planes.context.index import RepoIndex, extract_symbols
from planes.context.tools import FindSymbolTool, MapRepositoryTool

FILES = {
    "app/service.py": (
        "MAX_RETRIES = 3\n"
        "class BillingService:\n"
        "    def charge(self, amount):\n"
        "        return amount\n"
        "def helper():\n"
        "    return BillingService().charge(1)\n"
    ),
    "web/src/cart.ts": (
        "export interface CartItem { id: string }\n"
        "export class Cart {\n"
        "  add(item: CartItem) {}\n"
        "}\n"
        "export const totalPrice = (items: CartItem[]) => 0;\n"
        "export async function checkout(cart: Cart) {}\n"
    ),
    "web/src/page.jsx": "import { checkout } from './cart';\nfunction Page() { return checkout(); }\n",
    "svc/main.go": (
        "package main\n"
        "type Order struct{}\n"
        "func (o *Order) Total() int { return 0 }\n"
        "func main() {}\n"
    ),
    "core/lib.rs": "pub struct Engine;\npub fn start_engine() {}\nmod tests {}\n",
    "api/User.java": "public class User {\n  public String getName() {\n    return name;\n  }\n}\n",
    "native/math.c": "int add(int a, int b) {\n  return a + b;\n}\nstruct Vec { int x; };\n",
    "node_modules/pkg/index.js": "export function shouldNotAppear() {}\n",
    "dist/bundle.min.js": "function minified(){}\n",
    "generated/skip_me.py": "def skipped(): pass\n",
}


class ContextToolsBase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        for rel, text in FILES.items():
            path = os.path.join(self.ws, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        with open(os.path.join(self.ws, ".harnessignore"), "w") as f:
            f.write("# generated code\ngenerated/\n")
        self.index = RepoIndex(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)


class TestSymbolExtraction(unittest.TestCase):
    def names(self, path, source):
        return {s.name for s in extract_symbols(path, source)}

    def test_python_is_exact(self):
        self.assertEqual(
            self.names("a.py", FILES["app/service.py"]),
            {"MAX_RETRIES", "BillingService", "BillingService.charge", "helper"},
        )

    def test_typescript(self):
        self.assertTrue(
            {"CartItem", "Cart", "totalPrice", "checkout"} <= self.names("a.ts", FILES["web/src/cart.ts"])
        )

    def test_commonjs_and_class_methods(self):
        source = (
            "res.send = function send(body) {\n"
            "exports.compile = (src) => src;\n"
            "module.exports.render = async function () {\n"
            "class Store {\n"
            "  async load(id: string): Promise<void> {\n"
            "  static create() {\n"
            "  if (ready) {\n"
            "  describe('x', () => {\n"
            "    setImmediate(function () {\n"
        )
        names = self.names("a.ts", source)
        self.assertTrue({"res.send", "exports.compile", "module.exports.render", "Store", "load", "create"} <= names)
        self.assertNotIn("if", names)
        self.assertNotIn("describe", names)
        self.assertNotIn("setImmediate", names)

    def test_go_rust_java_c(self):
        self.assertTrue({"Order", "Total", "main"} <= self.names("a.go", FILES["svc/main.go"]))
        self.assertTrue({"Engine", "start_engine", "tests"} <= self.names("a.rs", FILES["core/lib.rs"]))
        self.assertTrue({"User", "getName"} <= self.names("A.java", FILES["api/User.java"]))
        self.assertTrue({"add", "Vec"} <= self.names("a.c", FILES["native/math.c"]))

    def test_c_control_flow_is_not_a_function(self):
        self.assertEqual(self.names("a.c", "  if (x) {\n  while (y) {\n"), set())

    def test_broken_python_yields_nothing_instead_of_crashing(self):
        self.assertEqual(self.names("a.py", "def broken(:\n"), set())


class TestMapRepository(ContextToolsBase):
    def test_maps_all_languages_and_skips_noise(self):
        out = MapRepositoryTool(self.index).execute({}).output
        for expected in ["app/service.py", "web/src/cart.ts", "svc/main.go", "core/lib.rs", "api/User.java"]:
            self.assertIn(expected, out)
        for skipped in ["node_modules", "bundle.min.js", "skip_me"]:
            self.assertNotIn(skipped, out)

    def test_query_and_path_narrow_the_map(self):
        out = MapRepositoryTool(self.index).execute({"query": "checkout"}).output
        self.assertIn("checkout", out)
        self.assertNotIn("BillingService", out)
        out = MapRepositoryTool(self.index).execute({"path": "svc"}).output
        self.assertIn("svc/main.go", out)
        self.assertNotIn("service.py", out)

    def test_over_budget_rolls_up_to_directories(self):
        out = MapRepositoryTool(self.index).execute({"max_chars": 200}).output
        self.assertIn("repository overview", out)
        self.assertIn("web/", out)
        self.assertNotIn("totalPrice:5", out)
        drill = MapRepositoryTool(self.index).execute({"path": "web", "max_chars": 2000}).output
        self.assertIn("totalPrice:5", drill)

    def test_scope_cannot_escape_workspace(self):
        out = MapRepositoryTool(self.index).execute({"path": "../.."}).output
        self.assertIn("0 source files scanned", out)

    def test_changed_file_is_reparsed(self):
        tool = MapRepositoryTool(self.index)
        self.assertNotIn("refund", tool.execute({"path": "app"}).output)
        path = os.path.join(self.ws, "app/service.py")
        with open(path, "a") as f:
            f.write("def refund():\n    pass\n")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertIn("refund", tool.execute({"path": "app"}).output)


class TestFindSymbol(ContextToolsBase):
    def test_definition_and_cross_file_mentions(self):
        out = FindSymbolTool(self.index).execute({"name": "checkout"}).output
        self.assertIn("web/src/cart.ts:6  function checkout", out)
        self.assertIn("web/src/page.jsx:1", out)
        self.assertIn("web/src/page.jsx:2", out)

    def test_method_name_finds_qualified_definition(self):
        out = FindSymbolTool(self.index).execute({"name": "charge"}).output
        self.assertIn("method BillingService.charge", out)
        self.assertIn("app/service.py:6", out)

    def test_whole_word_only(self):
        out = FindSymbolTool(self.index).execute({"name": "Cart"}).output
        self.assertNotIn("CartItem {", out.split("[mentions")[0])

    def test_missing_symbol(self):
        out = FindSymbolTool(self.index).execute({"name": "doesNotExist"}).output
        self.assertIn("(none found)", out)

    def test_name_required(self):
        self.assertFalse(FindSymbolTool(self.index).execute({}).success)


class TestContextToolsAreReadOnly(unittest.TestCase):
    def test_auto_allowed_without_prompt(self):
        gate = PermissionGate(".", input_func=lambda _: self.fail("should not prompt"))
        for tool in ["map_repository", "find_symbol"]:
            call = ToolCallRequest(call_id="1", tool_name=tool, parameters={})
            level = gate.assess(call)
            self.assertEqual(level, PermissionLevel.AUTO_ALLOW)
            self.assertTrue(gate.authorize(call, level))

    def test_unknown_tools_still_restricted(self):
        gate = PermissionGate(".")
        call = ToolCallRequest(call_id="1", tool_name="map_and_delete", parameters={})
        self.assertEqual(gate.assess(call), PermissionLevel.RESTRICTED)


if __name__ == "__main__":
    unittest.main()
