"""
Kapi rigorous-eval package.

Components (built spine-first):
  gold.py        — deterministic ground-truth computed from sample CSVs
  testset.py     — loads labeled cases, merges in computed gold
  metrics.py     — deterministic scorers (groundedness, numeric, refusal)   [next]
  failure_tags.py— map metric signals to a failure taxonomy                 [next]
  runner.py      — orchestrate retrieve -> answer -> score -> tag           [next]
  report.py      — JSON + Markdown artifacts                                [next]
  judge.py       — LLM-as-judge against rubric (Phase 2)
  compare.py     — A/B across two configs (Phase 3)
"""
