import pytest

from fakes import FakeRemover, write_photo
from test_pipeline_build import fake_llm
from vinted_listing import cli
from vinted_listing.pipeline import Deps
from vinted_listing.usage import BudgetGuard, UsageLedger


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_ROOT", str(tmp_path / "drive"))
    monkeypatch.setenv("VINTED_LISTING_HOME", str(tmp_path / "home"))
    for name in ("MODEL_PROFILE", "MODEL_DESCRIBE", "MODEL_RESEARCH", "MODEL_PARSE", "MODEL_LISTING",
                 "MONTHLY_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)

    def fake_build_deps(settings):
        ledger = UsageLedger(settings.db_path, settings)
        return Deps(llm=fake_llm(), remover=FakeRemover(), ledger=ledger, budget=BudgetGuard(ledger, settings))

    monkeypatch.setattr(cli, "build_deps", fake_build_deps)
    return tmp_path


def test_run_builds_package_without_touching_source(env, capsys):
    folder = env / "inbox" / "levis-501"
    write_photo(folder / "IMG_1.jpg")
    (folder / "hint.txt").write_text("brand: Levi's\nprice: 20", encoding="utf-8")
    before = sorted(p.name for p in folder.iterdir())

    assert cli.main(["run", str(folder), "--out", str(env / "out")]) == 0

    assert (env / "out" / "listing.txt").exists() and (env / "out" / "01.jpg").exists()
    assert sorted(p.name for p in folder.iterdir()) == before
    assert "listing.txt" in capsys.readouterr().out


def test_run_default_out_is_runs_dir(env):
    folder = env / "levis-501"
    write_photo(folder / "IMG_1.jpg")
    assert cli.main(["run", str(folder)]) == 0
    assert (env / "home" / "runs" / "levis-501" / "listing.txt").exists()


def test_run_folder_without_photos_fails(env, capsys):
    (env / "empty").mkdir()
    assert cli.main(["run", str(env / "empty")]) == 1
    assert "no photos" in capsys.readouterr().err


def test_run_reports_failed_step(env, monkeypatch, capsys):
    folder = env / "toy"
    write_photo(folder / "IMG_1.jpg")
    original = cli.build_deps
    monkeypatch.setattr(cli, "build_deps", lambda s: Deps(**{**original(s).__dict__, "llm": fake_llm(raise_on="describe")}))
    assert cli.main(["run", str(folder)]) == 1
    assert "failed at step 'describe'" in capsys.readouterr().err


def test_run_deferred_by_budget_exits_3(env, monkeypatch, capsys):
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "0")
    folder = env / "toy"
    write_photo(folder / "IMG_1.jpg")
    assert cli.main(["run", str(folder)]) == 3
    assert "monthly budget" in capsys.readouterr().err


def test_eval_profile_and_compare(env, capsys):
    golden = env / "golden"
    write_photo(golden / "levis" / "IMG_1.jpg")
    assert cli.main(["eval", str(golden), "--profile", "opus"]) == 0
    assert cli.main(["eval", str(golden), "--profile", "mixed"]) == 0
    reports = sorted((env / "home" / "runs").glob("eval-*/report.csv"))
    assert {r.parent.name.split("-")[1] for r in reports} == {"opus", "mixed"}
    opus = next(r for r in reports if "-opus-" in r.parent.name)
    mixed = next(r for r in reports if "-mixed-" in r.parent.name)
    assert "claude-opus-5-5" in opus.read_text(encoding="utf-8")
    assert cli.main(["eval-compare", str(opus), str(mixed)]) == 0
    assert (mixed.parent / "compare.txt").exists() and "average cost per item" in capsys.readouterr().out


def test_costs_prints_summary(env, capsys):
    assert cli.main(["costs"]) == 0
    out = capsys.readouterr().out
    assert "Month to date: $0.00 of $20.00" in out and "remaining $20.00" in out


def test_missing_drive_root_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DRIVE_ROOT", raising=False)
    monkeypatch.setenv("VINTED_LISTING_HOME", str(tmp_path / "home"))
    assert cli.main(["costs"]) == 2
    assert "DRIVE_ROOT" in capsys.readouterr().err
