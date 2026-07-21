"""Smoke test: the public trust_demo runs end to end and exercises all three
mechanisms. Guards the repo's headline 'run this in 5 seconds' artifact against
silent breakage when an underlying interface changes."""
import sys
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO = Path(__file__).resolve().parents[2] / "examples" / "trust_demo.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("trust_demo", DEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_file_exists():
    assert DEMO.exists(), "examples/trust_demo.py is referenced in the README"


def test_demo_runs_clean_and_prints_all_three_sections(capsys):
    mod = _load_demo()
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "COMPUTE-FIRST" in out
    assert "NUMERIC GROUNDING" in out
    assert "HONESTY GUARD" in out
    assert "FLAGGED" in out          # the fabricated number was caught
    assert "COMPUTED FROM FULL DATASET" in out
