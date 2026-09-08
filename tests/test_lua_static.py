"""Static Lua validation that runs with the system Lua toolchain."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


class LuaStaticTests(unittest.TestCase):
    def test_all_factorio_lua_sources_parse(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = sorted((root / "mod" / "FactorioCompanion").rglob("*.lua"))
        self.assertTrue(sources)
        luac = shutil.which("luac")
        lua = shutil.which("lua")
        if not luac and not lua:
            self.skipTest("neither luac nor lua is installed")

        for source in sources:
            with self.subTest(source=source.relative_to(root)):
                if luac:
                    command = [luac, "-p", str(source)]
                else:
                    command = [lua, "-e", "assert(loadfile(arg[1]))", str(source)]
                result = subprocess.run(
                    command,
                    cwd=root,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=f"Lua parser failed for {source}: {result.stderr.strip()}",
                )
