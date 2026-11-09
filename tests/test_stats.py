"""Insights numbers and the log fields they rely on."""
import json
from datetime import datetime, timedelta

from email_agent import stats
from email_agent.agent import outcome_of, rule_id_of

from test_agent import E, make_agent

NOW = datetime(2026, 11, 20, 12, 0, 0)


def at(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def entry(days_ago, source, frm="Northwind <n@x.example>", actions=("archive",), seconds=2, **extra):
    return {"at": at(days_ago), "ref": 1, "email_id": "e", "from": frm, "subject": "s", "category": "promo",
            "source": source, "actions": list(actions), "results": [], "seconds": seconds, **extra}


def write(tmp, name, data):
    (tmp / name).write_text(json.dumps(data))


def test_source_helpers():
    assert rule_id_of("rule r2") == "r2" and rule_id_of("workflow of r12") == "r12"
    assert rule_id_of("your instruction") is None
    assert outcome_of("rule r2") == "auto"
    assert outcome_of("rule r2 (practice)") == "practice"
    assert outcome_of("one-off (read & archive)") == "one_off"
    assert outcome_of("your instruction") == "asked"
    assert outcome_of("your instruction", failed=True) == "failed"


def test_measures(tmp_path):
    write(tmp_path, "activity_log.json", [
        entry(1, "rule r1"), entry(2, "rule r1"), entry(3, "rule r1", actions=("save_note", "archive")),
        entry(4, "rule r2", actions=("delete",)),
        entry(5, "your instruction", frm="Skyline <s@y.example>", actions=("delete",)),
        entry(6, "one-off (read & archive)", frm="Skyline <s@y.example>"),
        entry(7, "rule r1 (practice)"),                      # practice: ignored
        entry(8, "your instruction — failed"),                # failed: ignored
        entry(40, "your instruction", frm="Old <o@z.example>"),   # previous period
        entry(45, "rule r1"),
    ])
    write(tmp_path, "events.json", [
        {"at": at(2), "type": "restore", "ref": 1, "rule_id": "r1", "outcome": "auto"},
        {"at": at(3), "type": "restore", "ref": 2, "rule_id": None, "outcome": "asked"},
        {"at": at(4), "type": "rule_deleted", "rule_id": "r9", "name": "Old rule", "times_applied": 3},
    ])
    write(tmp_path, "rules.json", [{"id": "r1", "name": "Northwind coupons"}])
    write(tmp_path, "todos.json", [{"id": 1, "created": at(2), "done": True}, {"id": 2, "created": at(3), "done": False}])
    r = stats.compute(tmp_path, 30, NOW)
    assert (r["emails"], r["auto"], r["asked"]) == (6, 4, 2)
    assert r["auto_pct"] == 67
    assert r["questions_per_100"] == 33.3 and r["questions_per_100_before"] == 50.0
    assert r["put_back_total"] == 2 and r["put_back_after_auto"] == 1
    assert r["kept_pct"] == 75.0
    assert [(t["rule_id"], t["count"]) for t in r["top_rules"]] == [("r1", 3), ("r2", 1)]
    assert r["top_rules"][0]["name"] == "Northwind coupons"
    assert r["ask_again"] == [{"sender": "Skyline", "count": 2}]
    assert (r["archived"], r["deleted"]) == (4, 2)
    assert (r["todos_made"], r["todos_done"]) == (2, 1)
    assert r["median_read_seconds"] == 2
    assert r["minutes_saved"] == 2 and r["rules_deleted"] == 1


def test_empty_data_folder(tmp_path):
    r = stats.compute(tmp_path, 30, NOW)
    assert r["emails"] == 0 and r["questions_per_100"] is None and r["kept_pct"] is None
    assert r["top_rules"] == [] and r["trust_hearts"] == 4


def test_agent_writes_outcome_rule_and_seconds(tmp_path):
    agent, _ = make_agent(tmp_path, [E["d01"], E["d02"]], {
        "d01": ["Save the coupon code and its conditions to my notes, then delete the email.", "1"]})
    agent.run(days=None)
    log = json.loads((tmp_path / "activity_log.json").read_text())
    assert [e["outcome"] for e in log] == ["asked", "auto"]
    assert log[0]["rule_id"] is None and log[1]["rule_id"] == "r1"
    assert all("seconds" in e for e in log)


def test_deleting_a_rule_and_putting_back_are_recorded(tmp_path):
    from email_agent.webapp import App
    agent, _ = make_agent(tmp_path, [E["d01"], E["d02"]], {
        "d01": ["Save the coupon code and its conditions to my notes, then delete the email.", "1"]})
    agent.run(days=None)
    app = App(tmp_path, "builtin", "")
    app.mode = "gmail"   # use the temp folder, not the shared demo folder
    app.delete_rule("r1")
    events = json.loads((tmp_path / "events.json").read_text())
    assert events[0]["type"] == "rule_deleted" and events[0]["rule_id"] == "r1"
    assert events[0]["times_applied"] >= 1
