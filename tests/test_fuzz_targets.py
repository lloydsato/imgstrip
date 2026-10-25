"""The fuzz targets (fuzz/targets.py) on their seeds and on random mutations of them, without Atheris, so that the
targets keep working and what the fuzzer would report as a bug is caught here first."""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fuzz"))

import targets  # noqa: E402
from test_fuzz import mutate  # noqa: E402

CHEAP = [t for t in targets.TARGETS if t != "strip"]  # the engine itself is mutation-fuzzed in test_fuzz.py


@pytest.mark.parametrize("name", list(targets.TARGETS))
def test_every_target_has_seeds_and_accepts_them(name):
    seeds = targets.seeds(name)
    assert seeds
    for data in seeds.values():
        targets.TARGETS[name](data)


@pytest.mark.parametrize("name", CHEAP)
def test_mutated_seeds_never_break_a_target(name):
    rng = random.Random(f"fuzz-{name}")
    seeds = [s for s in targets.seeds(name).values() if s]
    for _ in range(150):
        data = rng.choice(seeds)
        for _ in range(rng.randint(1, 3)):
            data = mutate(data, rng) if data else bytes(rng.randrange(256) for _ in range(16))
        targets.TARGETS[name](data)


def test_the_sandbox_answer_target_reads_a_real_answer():
    answers = targets.seeds("sandbox_answer")
    assert {"png_everything.png", "jpeg_everything.jpg", "array"} <= set(answers)
    for data in answers.values():
        targets.sandbox_answer(data)


@pytest.mark.parametrize("depth", [100, 5000])
def test_a_deeply_nested_answer_is_refused_not_a_crash(depth):
    import json

    from imgstrip import sandbox

    nested = ("[" * depth + "]" * depth).encode()
    w_inner = io.BytesIO()
    sandbox._Writer(w_inner).frame(b"H", nested)
    outer = io.BytesIO()
    sandbox._Writer(outer).frame(b"D", w_inner.getvalue())
    sandbox._Writer(outer).frame(b"S", json.dumps({"exit": 0}).encode())
    targets.sandbox_answer(outer.getvalue())


REGRESSIONS = sorted((Path(__file__).resolve().parent.parent / "fuzz" / "regressions").iterdir())


@pytest.mark.parametrize("path", REGRESSIONS, ids=lambda p: p.name)
def test_inputs_the_fuzzer_found_stay_fixed(path):
    """Each file is named <target>-<what it found>; it once made that target fail."""
    targets.TARGETS[path.name.split("-")[0]](path.read_bytes())
