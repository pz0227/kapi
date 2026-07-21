"""
Regression guard: no `yield` inside a `finally` block in the SSE chat routes.

Why: the streaming endpoint's trailing metadata (numeric-groundedness, [DONE])
once lived in a `finally`. If a client disconnects mid-stream, Python throws
GeneratorExit into the generator; yielding while that unwinds raises
"async generator ignored GeneratorExit" and corrupts the response. Persistence
belongs in finally (must survive disconnect); yields must not. This test fails
if anyone reintroduces the pattern.
"""
import ast
from pathlib import Path

CHAT = Path(__file__).resolve().parents[1] / "api" / "routes" / "chat.py"


def _yields_in_finally(path: Path) -> list[int]:
    tree = ast.parse(path.read_text())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.finalbody:
                for n in ast.walk(stmt):
                    if isinstance(n, (ast.Yield, ast.YieldFrom)):
                        bad.append(n.lineno)
    return bad


def test_no_yield_inside_finally_in_chat_routes():
    bad = _yields_in_finally(CHAT)
    assert not bad, f"yield inside finally at lines {bad} — will break on client disconnect"
