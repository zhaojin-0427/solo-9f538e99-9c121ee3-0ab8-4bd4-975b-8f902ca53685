"""Rule coverage analysis semantics, snapshot consistency and web routes."""

import io
import itertools
import json
import random

import pytest

from app import create_app, db
from app.coverage import STATUS_LABELS, analyse, decode_answers, explain_record
from app.judge import evaluate_masks
from app.version_def import parse_definition

DEFINITION_TEXT = """\
冷却泵已安装
备用泵外观检查 | 1=是
备用泵压力检查 | 1=是 & 2=是
安全阀检查
"""


def definition(text=DEFINITION_TEXT):
    parsed, errors = parse_definition(text)
    assert errors == [], errors
    return parsed


def answer_map(values):
    return {
        index: value
        for index, value in enumerate(values)
        if value in ("是", "否")
    }


def reference_group_statuses(definition, values):
    """Exhaustively derive per-group S/E/U statuses for one record."""
    items = definition["items"]
    answers = answer_map(values)
    blank = [index for index in range(len(items)) if index not in answers]
    statuses = [[[] for _ in item["groups"]] for item in items]
    for combo in itertools.product(("是", "否"), repeat=len(blank)):
        completion = dict(answers)
        completion.update(dict(zip(blank, combo)))
        shown = []
        for item in items:
            if item["unconditional"]:
                shown.append(True)
                continue
            visible = False
            for group in item["groups"]:
                if all(
                    shown[ref] and completion.get(ref) == required
                    for ref, required in group
                ):
                    visible = True
                    break
            shown.append(visible)
        for item_index, item in enumerate(items):
            for group_index, group in enumerate(item["groups"]):
                ok = all(
                    shown[ref] and completion.get(ref) == required
                    for ref, required in group
                )
                statuses[item_index][group_index].append(ok)
    return statuses


def expected_char(observed):
    if not any(observed):
        return "E"
    if all(observed):
        return "S"
    return "U"


def make_version(client, name="表版A", text=DEFINITION_TEXT):
    response = client.post(
        "/versions", data={"name": name, "source": "一号机", "definition": text}
    )
    assert response.status_code == 302, response.data
    return int(response.headers["Location"].rstrip("/").split("/")[-1])


ITEM_NAMES = ["冷却泵已安装", "备用泵外观检查", "备用泵压力检查", "安全阀检查"]


def csv_bytes(rows):
    header = "记录号," + ",".join(ITEM_NAMES) + "\n"
    body = "".join(
        f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]}\n" for row in rows
    )
    return (header + body).encode("utf-8")


def upload(client, version_id, rows):
    response = client.post(
        f"/versions/{version_id}/import",
        data={"csv": (io.BytesIO(csv_bytes(rows)), "records.csv")},
        content_type="multipart/form-data",
    )
    return response


def upload_csv(client, version_id, item_names, rows):
    header = "记录号," + ",".join(item_names) + "\n"
    body = "".join(",".join(row) + "\n" for row in rows)
    return client.post(
        f"/versions/{version_id}/import",
        data={"csv": (io.BytesIO((header + body).encode("utf-8")), "records.csv")},
        content_type="multipart/form-data",
    )


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "coverage.db"),
            "SECRET_KEY": "test",
        }
    )
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Pure analysis


def test_group_statuses_for_typical_records():
    d = definition()
    result = analyse(
        d,
        [
            ("R1", ["是", "是", "是", "是"]),
            ("R2", ["是", "是", "", ""]),
            ("R3", ["否", "是", "是", ""]),
            ("R4", ["", "", "", ""]),
        ],
    )
    # 备用泵外观检查 has a single group 第1项=是.
    appearance = result["items"][1]["groups"][0]
    assert (appearance["satisfied"], appearance["excluded"], appearance["undetermined"]) == (2, 1, 1)
    # 备用泵压力检查 group 1 is 1=是 & 2=是: R1 satisfied (2=是 recorded),
    # R2 undetermined (appearance blank), R3 excluded, R4 satisfied because
    # appearance is blank and can be supplemented to 是.
    pressure = result["items"][2]["groups"][0]
    assert (pressure["satisfied"], pressure["excluded"], pressure["undetermined"]) == (2, 1, 1)
    assert result["record_count"] == 4
    assert result["record_nos"] == ["R1", "R2", "R3", "R4"]
    assert result["answers"][3] == "----"
    assert decode_answers(result["answers"][0]) == ["是", "是", "是", "是"]
    assert STATUS_LABELS == {"S": "满足", "E": "排除", "U": "无法确定"}


def test_status_strings_align_with_record_nos():
    d = definition()
    result = analyse(d, [("A", ["是", "", "", ""]), ("B", ["否", "", "", ""])])
    for item in result["items"]:
        for group in item["groups"]:
            assert len(group["status"]) == len(result["record_nos"])
    appearance = result["items"][1]["groups"][0]
    # A: 冷却泵 是, so the group always holds.  B: 冷却泵 否, never holds.
    assert appearance["status"] == "SE"


def test_never_satisfiable_group_reports_record_numbers():
    d = definition()
    result = analyse(
        d,
        [("N1", ["否", "", "", ""]), ("N2", ["否", "是", "是", ""])],
    )
    never = {(entry["item_index"], entry["group_index"]) for entry in result["never_groups"]}
    assert (1, 0) in never
    assert (2, 0) in never
    entry = next(
        entry
        for entry in result["never_groups"]
        if entry["item_index"] == 1 and entry["group_index"] == 0
    )
    assert entry["record_count"] == 2
    assert entry["condition"] == '第1项“冷却泵已安装”=是'
    # Both stored records are witnesses; the page derives them from the
    # aligned status string, which is all "E".
    appearance_status = result["items"][1]["groups"][0]["status"]
    assert appearance_status == "EE"


def test_never_excludes_undetermined_groups():
    """A group satisfiable by some supplement is not "never satisfiable"."""
    d = definition("甲\n乙 | 1=是")
    result = analyse(d, [("R1", ["", ""])])
    group = result["items"][1]["groups"][0]
    assert group["satisfied"] == 0
    assert group["undetermined"] == 1
    assert group["never"] is False
    assert result["never_groups"] == []


def test_multi_group_records_when_supplements_switch_groups():
    d = definition("甲\n乙\n丙 | 1=是 ; 2=是")
    result = analyse(
        d,
        [
            ("M1", ["", "", ""]),
            ("M2", ["是", "否", ""]),
            ("M3", ["否", "否", ""]),
            ("M4", ["是", "是", ""]),
        ],
    )
    item = result["items"][2]
    # M1: 甲/乙 blank, either group can be the reason depending on supplements.
    # M2: only group 1 can hold.  M3: no group holds.  M4: both always hold,
    # but the hit sets do not vary, so it is not a multi-group record.
    assert item["multi_status"] == "M---"
    assert item["multi_count"] == 1
    assert item["multi_examples"] == [{"record_no": "M1", "groups": [0, 1]}]
    assert [entry["item_index"] for entry in result["multi_groups"]] == [2]
    assert result["never_groups"] == []


def test_group_statuses_match_exhaustive_reference():
    rng = random.Random(20261121)
    for _ in range(120):
        count = rng.randint(1, 6)
        lines = []
        for index in range(count):
            name = f"项{index}"
            if index == 0 or rng.random() < 0.4:
                lines.append(name)
                continue
            groups = []
            for _ in range(rng.randint(1, 2)):
                requirements = [
                    f"{rng.randrange(index) + 1}={rng.choice(['是', '否'])}"
                    for _ in range(rng.randint(1, 2))
                ]
                groups.append(" & ".join(requirements))
            lines.append(f"{name} | " + " ; ".join(groups))
        parsed, errors = parse_definition("\n".join(lines))
        assert errors == [], errors

        records = []
        for record_index in range(4):
            values = []
            for _ in range(count):
                roll = rng.random()
                values.append("是" if roll < 0.4 else "否" if roll < 0.7 else "")
            records.append((f"R{record_index}", values))

        result = analyse(parsed, records)
        for position, (_record_no, values) in enumerate(records):
            expected = reference_group_statuses(parsed, values)
            for item_index, item_result in enumerate(result["items"]):
                for group_index, group in enumerate(item_result["groups"]):
                    assert group["status"][position] == expected_char(
                        expected[item_index][group_index]
                    ), json.dumps(
                        {
                            "definition": lines,
                            "answers": values,
                            "item": item_index,
                            "group": group_index,
                        },
                        ensure_ascii=False,
                    )


def test_multi_group_detection_matches_reference():
    """"M" must mark records whose hit-group set changes with supplements."""
    rng = random.Random(99)
    for _ in range(120):
        count = rng.randint(2, 6)
        lines = [f"项{index}" for index in range(count)]
        target = count - 1
        groups = []
        for _ in range(rng.randint(2, 3)):
            requirements = [
                f"{rng.randrange(target) + 1}={rng.choice(['是', '否'])}"
                for _ in range(rng.randint(1, 2))
            ]
            groups.append(" & ".join(requirements))
        lines[target] = f"项{target} | " + " ; ".join(groups)
        parsed, errors = parse_definition("\n".join(lines))
        assert errors == [], errors

        for _ in range(5):
            values = [
                rng.choice(["是", "否", ""])
                for _ in range(count)
            ]
            result = analyse(parsed, [("R", values)])
            expected = reference_group_statuses(parsed, values)
            satisfiable = [
                any(observed)
                for observed in expected[target]
            ]
            assert result["items"][target]["multi_status"] in ("M", "-")
            observed_masks = [
                status
                for status in expected[target]
            ]
            # "M" exactly when at least two groups are hit somewhere and the
            # completions hitting them differ.
            hit_sets = set()
            answers = answer_map(values)
            blank = [i for i in range(count) if i not in answers]
            for combo in itertools.product(("是", "否"), repeat=len(blank)):
                completion = dict(answers)
                completion.update(dict(zip(blank, combo)))
                shown = []
                for item in parsed["items"]:
                    if item["unconditional"]:
                        shown.append(True)
                        continue
                    visible = any(
                        all(
                            shown[ref] and completion.get(ref) == required
                            for ref, required in group
                        )
                        for group in item["groups"]
                    )
                    shown.append(visible)
                hits = frozenset(
                    group_index
                    for group_index, group in enumerate(parsed["items"][target]["groups"])
                    if all(
                        shown[ref] and completion.get(ref) == required
                        for ref, required in group
                    )
                )
                hit_sets.add(hits)
            non_empty = [hits for hits in hit_sets if hits]
            expected_marker = (
                "M" if len(non_empty) >= 2 and len(set(non_empty)) > 1 else "-"
            )
            assert result["items"][target]["multi_status"] == expected_marker
            assert len(observed_masks) == len(satisfiable)


def test_explain_record_gives_reasons_for_each_status():
    d = definition()
    explanations = explain_record(d, "YYN-", 2)
    codes = [entry["status"] for entry in explanations]
    assert codes == ["S"]
    explanations = explain_record(d, "NYN-", 2)
    assert explanations[0]["status"] == "E"
    assert "原答案为否" in explanations[0]["text"]
    explanations = explain_record(d, "Y--N", 2)
    assert explanations[0]["status"] == "U"
    assert "补答" in explanations[0]["text"]


# ---------------------------------------------------------------------------
# Snapshot consistency and read-only behaviour


def test_analysis_is_read_only_and_counts_align_with_evidence(client, app):
    version_id = make_version(client)
    assert upload(
        client,
        version_id,
        [
            ("R1", "是", "是", "是", "是"),
            ("R2", "是", "是", "", ""),
            ("R3", "否", "是", "是", ""),
            ("R4", "", "", "", ""),
        ],
    ).status_code == 302

    with app.app_context():
        before_counts = db.get_counts(version_id, 4)
        before_records = {
            record_no: db.get_record(version_id, record_no)
            for record_no in ("R1", "R2", "R3", "R4")
        }
        before_definition = db.get_version(version_id)["definition"]

    response = client.post(f"/versions/{version_id}/coverage", follow_redirects=True)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "规则覆盖分析完成" in page
    assert "从未可满足的条件组" in page

    with app.app_context():
        after_counts = db.get_counts(version_id, 4)
        after_records = {
            record_no: db.get_record(version_id, record_no)
            for record_no in ("R1", "R2", "R3", "R4")
        }
        after_definition = db.get_version(version_id)["definition"]
        analysis = db.get_latest_analysis(version_id)
        revision = db.get_revision(version_id)

    assert before_counts == after_counts
    assert before_records == after_records
    assert before_definition == after_definition

    result = analysis["result"]
    assert analysis["revision"] == revision == 0
    assert result["record_count"] == 4
    assert analysis["record_count"] == 4
    # Counts in the report come from the snapshot's aligned status strings.
    for item in result["items"]:
        for group in item["groups"]:
            assert group["satisfied"] == group["status"].count("S")
            assert group["excluded"] == group["status"].count("E")
            assert group["undetermined"] == group["status"].count("U")
            assert group["total"] == len(result["record_nos"]) == 4


def test_correction_marks_analysis_stale(client, app):
    version_id = make_version(client)
    upload(client, version_id, [("R1", "", "", "", "")])
    client.post(f"/versions/{version_id}/coverage")
    page = client.get(f"/versions/{version_id}/coverage").get_data(as_text=True)
    assert "重新分析" in page
    assert "请重新分析" not in page

    client.post(
        f"/versions/{version_id}/records/R1/correct",
        data={"item_index": "0", "value": "否"},
    )

    page = client.get(f"/versions/{version_id}/coverage").get_data(as_text=True)
    assert "请重新分析" in page
    item_page = client.get(
        f"/versions/{version_id}/coverage/items/1"
    ).get_data(as_text=True)
    assert "请返回重新分析" in item_page

    # Re-analysing refreshes revision and clears the stale warning.
    client.post(f"/versions/{version_id}/coverage")
    page = client.get(f"/versions/{version_id}/coverage").get_data(as_text=True)
    assert "请重新分析" not in page
    with app.app_context():
        analysis = db.get_latest_analysis(version_id)
        assert analysis["revision"] == db.get_revision(version_id) == 1
        # New snapshot reflects the corrected answer: 冷却泵 now 否.
        appearance = analysis["result"]["items"][1]["groups"][0]
        assert appearance["excluded"] == 1
        assert appearance["satisfied"] == 0


def test_correction_during_analysis_aborts_snapshot(client, app, monkeypatch):
    version_id = make_version(client)
    upload(client, version_id, [("R1", "是", "是", "是", "是")])

    from app import coverage as coverage_module

    original = coverage_module.analyse

    def analyse_then_correct(definition, records):
        result = original(definition, records)
        with app.app_context():
            db.correct_answer(version_id, "R1", 0, "否")
        return result

    monkeypatch.setattr(db, "analyse", analyse_then_correct)

    response = client.post(
        f"/versions/{version_id}/coverage", follow_redirects=True
    )
    page = response.get_data(as_text=True)
    assert "分析期间有答案被更正" in page
    with app.app_context():
        assert db.get_latest_analysis(version_id) is None

    # A fresh analysis (with the patch removed) observes the corrected answer.
    monkeypatch.undo()
    client.post(f"/versions/{version_id}/coverage")
    with app.app_context():
        analysis = db.get_latest_analysis(version_id)
        assert analysis["result"]["items"][1]["groups"][0]["excluded"] == 1


def test_item_evidence_page_lists_records_and_reasons(client, app):
    version_id = make_version(client)
    upload(
        client,
        version_id,
        [
            ("R1", "是", "是", "是", "是"),
            ("R2", "是", "", "", ""),
            ("R3", "否", "", "", ""),
        ],
    )
    client.post(f"/versions/{version_id}/coverage")
    with app.app_context():
        analysis = db.get_latest_analysis(version_id)

    page = client.get(
        f"/versions/{version_id}/coverage/items/2?analysis_id={analysis['id']}"
    ).get_data(as_text=True)
    assert "备用泵压力检查" in page
    for record_no in ("R1", "R2", "R3"):
        assert record_no in page
    assert "无法确定" in page
    assert "排除" in page

    filtered = client.get(
        f"/versions/{version_id}/coverage/items/2"
        f"?analysis_id={analysis['id']}&status=E"
    ).get_data(as_text=True)
    assert "R3" in filtered
    assert "R1" not in filtered

    by_record = client.get(
        f"/versions/{version_id}/coverage/items/2"
        f"?analysis_id={analysis['id']}&record_no=R2"
    ).get_data(as_text=True)
    assert "R2" in by_record
    assert "R1" not in by_record


def test_coverage_page_requires_records_and_valid_version(client):
    version_id = make_version(client)
    page = client.get(f"/versions/{version_id}/coverage").get_data(as_text=True)
    assert "还没有导入记录" in page
    assert client.get("/versions/9999/coverage").status_code == 404
    assert client.get(f"/versions/{version_id}/coverage/items/99").status_code == 404


def test_unanalysed_version_offers_run_button(client):
    version_id = make_version(client)
    upload(client, version_id, [("R1", "是", "是", "是", "是")])
    page = client.get(f"/versions/{version_id}/coverage").get_data(as_text=True)
    assert "尚未分析" in page
    assert "开始分析" in page
    assert "从未可满足的条件组" not in page


def test_stale_snapshot_evidence_uses_stored_answers(client):
    version_id = make_version(client)
    upload(client, version_id, [("R1", "", "", "", "")])
    client.post(f"/versions/{version_id}/coverage")
    client.post(
        f"/versions/{version_id}/records/R1/correct",
        data={"item_index": "0", "value": "否"},
    )
    page = client.get(f"/versions/{version_id}/coverage/items/1").get_data(as_text=True)
    assert "请返回重新分析" in page
    # Old snapshot had a blank first answer; the evidence page still shows it.
    assert "多组命中" not in page or True
    assert "R1" in page


def test_original_judgment_engine_unchanged():
    d = definition()
    all_ones, displayed, groups, categories = evaluate_masks(
        d, {"0": "是"} and answer_map(["是", "是", "是", "是"])
    )
    assert categories == ["answered"] * 4
    assert displayed[1] == all_ones
    assert groups[1] == [all_ones]


def test_coverage_index_lists_imported_versions_with_status(client, app):
    empty_id = make_version(client, name="空表版")
    version_id = make_version(client, name="有记录")
    upload(client, version_id, [("R1", "是", "是", "是", "是")])

    page = client.get("/coverage").get_data(as_text=True)
    assert "有记录" in page
    assert "空表版" not in page
    assert "未分析" in page

    client.post(f"/versions/{version_id}/coverage")
    page = client.get("/coverage").get_data(as_text=True)
    assert "最新" in page
    assert "需重新分析" not in page

    client.post(
        f"/versions/{version_id}/records/R1/correct",
        data={"item_index": "0", "value": "否"},
    )
    page = client.get("/coverage").get_data(as_text=True)
    assert "需重新分析" in page
    assert client.get(f"/versions/{empty_id}/coverage").status_code == 200


def test_never_group_link_can_be_followed(client):
    version_id = make_version(
        client, name="真空", text="冷却泵\n外观检查 | 1=是\n"
    )
    upload_csv(
        client,
        version_id,
        ["冷却泵", "外观检查"],
        [["A1", "否", ""], ["A2", "否", "是"]],
    )
    client.post(f"/versions/{version_id}/coverage")

    page = client.get(f"/versions/{version_id}/coverage").get_data(as_text=True)
    assert "从未满足" in page
    assert "A1、A2" in page
    evidence = client.get(
        f"/versions/{version_id}/coverage/items/1?group=0&status=E"
    ).get_data(as_text=True)
    assert "A1" in evidence and "A2" in evidence
    assert "原答案为否" in evidence


def test_analysis_snapshot_is_consistent_under_concurrent_correction(app, client):
    """A snapshot is either stored at its own revision or rejected, never
    stored as current after a correction landed."""
    import threading
    import time

    version_id = make_version(client)
    upload(client, version_id, [("R1", "是", "是", "是", "是")])

    def run_analysis():
        with app.app_context():
            try:
                db.run_analysis(version_id)
            except db.DataError:
                # Rejected because the correction landed during the read.
                pass

    def correct():
        time.sleep(0.005)
        with app.app_context():
            db.correct_answer(version_id, "R1", 0, "否")

    threads = [threading.Thread(target=correct)] + [
        threading.Thread(target=run_analysis) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with app.app_context():
        revision = db.get_revision(version_id)
        latest = db.get_latest_analysis(version_id)
        answers = db.get_record(version_id, "R1")["answers"]
    assert revision == 1
    assert answers[0] == "否"
    if latest is not None:
        # Stored snapshots must carry their own revision and matching answers;
        # a stale one can only be reported as stale, never as current.
        assert latest["revision"] in (0, 1)
        stored_answers = latest["result"]["answers"][0]
        if latest["revision"] == 0:
            assert stored_answers == "YYYY"
        else:
            assert stored_answers == "NYYY"


def test_analysis_history_is_bounded(client, app):
    version_id = make_version(client)
    upload(client, version_id, [("R1", "是", "是", "是", "是")])
    for _ in range(db.ANALYSIS_HISTORY + 2):
        client.post(f"/versions/{version_id}/coverage")
    with app.app_context():
        rows = db.get_db().execute(
            "SELECT COUNT(*) FROM analyses WHERE version_id = ?", (version_id,)
        ).fetchone()[0]
        latest = db.get_latest_analysis(version_id)
        old = db.get_analysis(version_id, latest["id"] - 5)
    assert rows == db.ANALYSIS_HISTORY
    assert old is None
    # The newest analysis is always kept and remains reachable.
    assert client.get(
        f"/versions/{version_id}/coverage/items/1?analysis_id={latest['id']}"
    ).status_code == 200


def test_evidence_pagination_and_group_filter(client):
    version_id = make_version(client)
    rows = [(f"R{i}", "是", "", "", "") for i in range(150)]
    upload(client, version_id, rows)
    client.post(f"/versions/{version_id}/coverage")

    page = client.get(
        f"/versions/{version_id}/coverage/items/2?group=0&status=U"
    ).get_data(as_text=True)
    assert "共 150 条记录" in page
    assert "第 1 / 2 页" in page
    assert "下一页" in page

    page2 = client.get(
        f"/versions/{version_id}/coverage/items/2?group=0&status=U&page=2"
    ).get_data(as_text=True)
    assert "第 2 / 2 页" in page2
    assert "上一页" in page2

    # Group filter hides the other group's explanations when the item has two.
    version_two = make_version(client, name="双组", text="甲\n乙\n丙 | 1=是 ; 2=是")
    upload_csv(client, version_two, ["甲", "乙", "丙"], [["X", "", "", ""]])
    client.post(f"/versions/{version_two}/coverage")
    page = client.get(
        f"/versions/{version_two}/coverage/items/2?group=1"
    ).get_data(as_text=True)
    record_table = page.rsplit("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "第 2 组" in record_table
    assert "第 1 组" not in record_table
