import json
import math
from pathlib import Path

import pytest

from app.transitions import plan_transition

CASES = json.loads((Path(__file__).resolve().parents[2] / "shared" / "transition_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_case(case):
    a = {"id": "A", "speed": 1, **case["a"]}
    b = {"id": "B", "speed": 1, **case["b"]}
    src = lambda v: math.inf if v is None else v  # noqa: E731
    plan = plan_transition({"id": "t", "type": "fade", "duration": case["duration"]}, a, b, src(case["srcA"]), src(case["srcB"]))
    if case["expect"] is None:
        assert plan is None
        return
    e = case["expect"]
    assert plan.status == e["status"], plan.message
    assert plan.half == pytest.approx(e["half"], abs=1e-6)
    assert (plan.freeze_a, plan.freeze_b) == (e["freezeA"], e["freezeB"])
    assert (plan.message == "") == (e["status"] == "ok")
