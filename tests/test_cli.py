import json

from conftest import FIXTURES, ROOT, posting
from job_scoring.cli import main


def test_run_writes_a_report_from_fixtures(tmp_path, capsys):
    out = tmp_path / "r.md"
    assert main(["run", "--boards", str(ROOT / "config" / "boards.example.json"), "--fixtures", str(FIXTURES), "--now", "2026-10-28T12:00:00Z", "--out", str(out)]) == 0
    assert "Job scoring report" in out.read_text() and "shortlist" in capsys.readouterr().out


def test_evaluate_command_prints_tiers(tmp_path, capsys):
    f = tmp_path / "p.json"
    f.write_text(json.dumps([posting()]))
    assert main(["evaluate", str(f)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["tier"] == "strong"
