import pytest

from niftypilot import bench, cli
from niftypilot.models import Experiment


def exp(**kw):
    base = dict(
        experiment_id="e" * 32,
        parameter_set={"data_kind": "actual", "selection_rule": "r"},
        training_period={"range": ["2025-01-01", "2025-06-30"]},
        validation_period={"range": ["2025-07-01", "2025-09-30"]},
        out_of_sample_period={"range": ["2025-10-01", "2025-12-31"]},
        n_trials=25,
        metrics={"per_capital": {}},
        status="dev_done",
        contaminated=False,
        oos_evaluated_at=None,
    )
    return Experiment(**{**base, **kw})


def test_research_report_states_oos_status():
    assert "untouched (sealed)" in cli.research_markdown(exp())
    md = cli.research_markdown(
        exp(contaminated=True),
        [
            {
                "at": "t",
                "purpose": "backtest",
                "range": ["a", "b"],
                "contaminating": True,
                "detail": "cli backtest",
            }
        ],
    )
    assert "CONTAMINATED" in md and "NOT independent evidence" in md
    assert "OOS access log" in md and "cli backtest" in md


def test_cli_refuses_actual_dataset_without_manifest():
    with pytest.raises(SystemExit):
        cli.main(["create-dataset", "--name", "x", "--source", "y", "--kind", "actual"])


def test_benchmark_script_runs_small(capsys):
    r = bench.main(["--symbols", "3", "--days", "30"])
    assert r["dataset"]["candles"] == 4 * 30 * 75  # 3 stocks + index
    assert {s["stage"] for s in r["stages"]} >= {"ingest (validate + insert)", "load market"}
    assert r["peak_rss_mb"] > 0
