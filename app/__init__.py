"""Flask entry point for the inspection-record management site."""

from __future__ import annotations

import os
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from . import coverage, db
from .csv_import import MAX_BYTES, CsvError, parse_csv
from .judge import CATEGORIES, CATEGORY_LABELS
from .version_def import describe_item, parse_definition

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DATABASE=os.environ.get(
            "DATABASE", str(BASE_DIR / "data" / "inspection.db")
        ),
        MAX_CONTENT_LENGTH=MAX_BYTES + 1024 * 1024,
    )
    if config:
        app.config.update(config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE"])

    app.teardown_appcontext(db.close_db)

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        flash("上传文件过大，CSV 最大 20 MB。", "error")
        return redirect(request.path.rsplit("/import", 1)[0] or url_for("index"))

    @app.template_filter("category_label")
    def category_label(value: str) -> str:
        return CATEGORY_LABELS.get(value, value)

    @app.get("/")
    def index():
        return render_template("index.html", versions=db.list_versions())

    def _render_new_version(**context):
        return render_template("new_version.html", **context)

    @app.get("/versions/new")
    def new_version():
        return render_template("new_version.html")

    @app.post("/versions")
    def create_version():
        name = (request.form.get("name") or "").strip()
        source = (request.form.get("source") or "").strip()
        definition_text = request.form.get("definition") or ""

        form = {"name": name, "source": source, "definition_text": definition_text}

        if not name:
            return _render_new_version(
                error="请填写表版名称。", **form
            ), 400
        if len(name) > 100:
            return _render_new_version(
                error="表版名称不能超过 100 个字符。", **form
            ), 400

        definition, errors = parse_definition(definition_text)
        if definition is None:
            return _render_new_version(errors=errors, **form), 400

        try:
            version_id = db.create_version(name, source, definition)
        except Exception:
            app.logger.exception("create_version failed")
            return _render_new_version(
                error="保存表版失败，请稍后重试。", **form
            ), 500
        flash("表版已创建，可以上传巡检记录。", "success")
        return redirect(url_for("version_detail", version_id=version_id))

    @app.get("/versions/<int:version_id>")
    def version_detail(version_id: int):
        version = db.get_version(version_id)
        if version is None:
            abort(404)
        definition = version["definition"]
        counts = db.get_counts(version_id, len(definition["items"]))
        items = []
        for index, item in enumerate(definition["items"]):
            items.append(
                {
                    "index": index,
                    "number": index + 1,
                    "name": item["name"],
                    "condition": describe_item(definition, index),
                    "counts": counts[index],
                    "total": sum(counts[index].values()),
                }
            )
        return render_template(
            "version.html",
            version=version,
            items=items,
            categories=CATEGORIES,
            labels=CATEGORY_LABELS,
        )

    @app.post("/versions/<int:version_id>/import")
    def import_csv(version_id: int):
        version = db.get_version(version_id)
        if version is None:
            abort(404)

        upload = request.files.get("csv")
        if upload is None or not upload.filename:
            flash("请选择 CSV 文件。", "error")
            return redirect(url_for("version_detail", version_id=version_id))

        try:
            rows = parse_csv(upload.read(), version["definition"])
            count = db.import_records(version_id, rows, version["definition"])
        except (CsvError, db.DataError) as exc:
            message = exc.message if isinstance(exc, CsvError) else str(exc)
            errors = exc.errors if isinstance(exc, CsvError) else []
            return render_template(
                "import_result.html",
                version=version,
                error=message,
                errors=errors,
            ), 400
        except Exception:
            app.logger.exception("import_records failed")
            return render_template(
                "import_result.html",
                version=version,
                error="导入失败，未写入任何记录，请稍后重试。",
                errors=[],
            ), 500

        flash(f"已导入 {count} 条记录。", "success")
        return redirect(url_for("version_detail", version_id=version_id))

    @app.get("/versions/<int:version_id>/records")
    def record_lookup(version_id: int):
        version = db.get_version(version_id)
        if version is None:
            abort(404)
        record_no = (request.args.get("record_no") or "").strip()
        if record_no:
            if db.get_record(version_id, record_no) is None:
                flash(f"没有找到记录号“{record_no}”。", "error")
            else:
                return redirect(
                    url_for(
                        "record_detail",
                        version_id=version_id,
                        record_no=record_no,
                    )
                )
        return render_template(
            "lookup.html", version=version, record_no=record_no
        )

    @app.get("/versions/<int:version_id>/records/<path:record_no>")
    def record_detail(version_id: int, record_no: str):
        version = db.get_version(version_id)
        if version is None:
            abort(404)
        record = db.get_record(version_id, record_no)
        if record is None:
            abort(404)

        definition = version["definition"]
        answers = record["answers"]
        if len(answers) < len(definition["items"]):
            answers = answers + [""] * (len(definition["items"]) - len(answers))

        rows = []
        for index, item in enumerate(definition["items"]):
            rows.append(
                {
                    "index": index,
                    "number": index + 1,
                    "name": item["name"],
                    "answer": answers[index],
                    "category": record["categories"][index],
                    "category_label": CATEGORY_LABELS[record["categories"][index]],
                    "condition": describe_item(definition, index),
                }
            )

        counts = db.get_counts(version_id, len(definition["items"]))
        count_rows = [
            {
                "number": index + 1,
                "name": definition["items"][index]["name"],
                "counts": counts[index],
                "total": sum(counts[index].values()),
            }
            for index in range(len(definition["items"]))
        ]

        return render_template(
            "record.html",
            version=version,
            record=record,
            rows=rows,
            count_rows=count_rows,
            categories=CATEGORIES,
            labels=CATEGORY_LABELS,
            corrected=request.args.get("corrected") == "1",
        )

    @app.post("/versions/<int:version_id>/records/<path:record_no>/correct")
    def correct(version_id: int, record_no: str):
        version = db.get_version(version_id)
        if version is None:
            abort(404)
        if db.get_record(version_id, record_no) is None:
            abort(404)

        item_raw = request.form.get("item_index", "")
        value = request.form.get("value", "")
        if not item_raw.isdigit():
            flash("更正失败：检查项无效。", "error")
            return redirect(
                url_for(
                    "record_detail", version_id=version_id, record_no=record_no
                )
            )

        try:
            db.correct_answer(version_id, record_no, int(item_raw), value)
        except db.DataError as exc:
            flash(f"更正失败：{exc}", "error")
            return redirect(
                url_for(
                    "record_detail", version_id=version_id, record_no=record_no
                )
            )
        except Exception:
            app.logger.exception("correct_answer failed")
            flash("更正失败，答案、判定和计数均未改变，请稍后重试。", "error")
            return redirect(
                url_for(
                    "record_detail", version_id=version_id, record_no=record_no
                )
            )

        return redirect(
            url_for(
                "record_detail",
                version_id=version_id,
                record_no=record_no,
                corrected=1,
            )
        )

    @app.get("/coverage")
    def coverage_index():
        all_versions = db.list_versions()
        versions = [
            version for version in all_versions if version["record_count"] > 0
        ]
        analyse_info = {}
        for version in versions:
            analysis = db.get_latest_analysis(version["id"])
            if analysis is None:
                analyse_info[version["id"]] = None
                continue
            analyse_info[version["id"]] = {
                "created_at": analysis["created_at"],
                "revision": analysis["revision"],
                "record_count": analysis["record_count"],
                "stale": analysis["revision"] != db.get_revision(version["id"]),
            }
        return render_template(
            "coverage_index.html",
            versions=versions,
            analyse_info=analyse_info,
            all_versions=all_versions,
        )

    @app.get("/versions/<int:version_id>/coverage")
    def coverage_page(version_id: int):
        version = db.get_version(version_id)
        if version is None:
            abort(404)

        definition = version["definition"]
        analysis = db.get_latest_analysis(version_id)
        current_revision = db.get_revision(version_id)
        stale = analysis is not None and analysis["revision"] != current_revision

        rows = []
        never_rows = []
        if analysis is not None:
            for item in analysis["result"]["items"]:
                definition_item = definition["items"][item["index"]]
                groups = []
                for group in item["groups"]:
                    groups.append(
                        {
                            **group,
                            "label": f"第 {group['group_index'] + 1} 组",
                        }
                    )
                rows.append(
                    {
                        "index": item["index"],
                        "number": item["index"] + 1,
                        "name": definition_item["name"],
                        "condition": describe_item(definition, item["index"]),
                        "groups": groups,
                        "multi_count": item["multi_count"],
                        "multi_examples": item["multi_examples"],
                    }
                )

            for entry in analysis["result"]["never_groups"]:
                record_positions = [
                    position
                    for position, status in enumerate(
                        analysis["result"]["items"][entry["item_index"]][
                            "groups"
                        ][entry["group_index"]]["status"]
                    )
                    if status == "E"
                ]
                numbers = [
                    analysis["result"]["record_nos"][position]
                    for position in record_positions
                ]
                never_rows.append(
                    {
                        **entry,
                        "item_number": entry["item_index"] + 1,
                        "item_name": definition["items"][entry["item_index"]][
                            "name"
                        ],
                        "all_record_nos": numbers,
                    }
                )

        return render_template(
            "coverage.html",
            version=version,
            analysis=analysis,
            stale=stale,
            current_revision=current_revision,
            rows=rows,
            never_rows=never_rows,
            status_labels=coverage.STATUS_LABELS,
        )

    @app.post("/versions/<int:version_id>/coverage")
    def coverage_run(version_id: int):
        version = db.get_version(version_id)
        if version is None:
            abort(404)
        if version["record_count"] == 0:
            flash("该表版还没有导入记录，暂无可分析内容。", "error")
            return redirect(url_for("coverage_page", version_id=version_id))
        try:
            db.run_analysis(version_id)
        except db.DataError as exc:
            flash(f"分析未完成：{exc}", "error")
        except Exception:
            app.logger.exception("run_analysis failed")
            flash("分析失败，未保存任何结果，请稍后重试。", "error")
        else:
            flash("规则覆盖分析完成，计数与明细来自同一份记录快照。", "success")
        return redirect(url_for("coverage_page", version_id=version_id))

    @app.get("/versions/<int:version_id>/coverage/items/<int:item_index>")
    def coverage_item(version_id: int, item_index: int):
        version = db.get_version(version_id)
        if version is None:
            abort(404)
        definition = version["definition"]
        if not 0 <= item_index < len(definition["items"]):
            abort(404)

        analysis_id = request.args.get("analysis_id", type=int)
        if analysis_id is not None:
            analysis = db.get_analysis(version_id, analysis_id)
        else:
            analysis = db.get_latest_analysis(version_id)
        if analysis is None:
            abort(404)

        result = analysis["result"]
        item = result["items"][item_index]
        current_revision = db.get_revision(version_id)
        stale = analysis["revision"] != current_revision

        status = request.args.get("status") or ""
        if status not in ("", "S", "E", "U"):
            status = ""
        group_filter = request.args.get("group", type=int)
        if group_filter is not None and not (
            0 <= group_filter < len(item["groups"])
        ):
            group_filter = None
        record_filter = (request.args.get("record_no") or "").strip()
        page = max(request.args.get("page", type=int) or 1, 1)

        group_rows = []
        for group in item["groups"]:
            group_rows.append(
                {
                    **group,
                    "label": f"第 {group['group_index'] + 1} 组",
                }
            )

        matches = []
        for position, record_no in enumerate(result["record_nos"]):
            if record_filter and record_no != record_filter:
                continue
            statuses = []
            matched = status == ""
            for group in item["groups"]:
                if (
                    group_filter is not None
                    and group["group_index"] != group_filter
                ):
                    continue
                code = group["status"][position]
                if status and code == status:
                    matched = True
                statuses.append(
                    {
                        "group_index": group["group_index"],
                        "label": f"第 {group['group_index'] + 1} 组",
                        "code": code,
                        "status_label": coverage.STATUS_LABELS[code],
                        "selected": group_filter == group["group_index"],
                    }
                )
            if not matched:
                continue
            matches.append((position, record_no, statuses))

        total_matches = len(matches)
        page_count = max(
            (total_matches + coverage.EVIDENCE_PAGE_SIZE - 1)
            // coverage.EVIDENCE_PAGE_SIZE,
            1,
        )
        page = min(page, page_count)
        start = (page - 1) * coverage.EVIDENCE_PAGE_SIZE
        end = start + coverage.EVIDENCE_PAGE_SIZE
        record_rows = []
        for position, record_no, statuses in matches[start:end]:
            explanations = coverage.explain_record(
                definition, result["answers"][position], item_index
            )
            evidence = []
            for status_entry in statuses:
                explanation = explanations[status_entry["group_index"]]
                evidence.append(
                    {
                        **status_entry,
                        "condition": explanation["condition"],
                        "text": explanation["text"],
                    }
                )
            record_rows.append(
                {
                    "record_no": record_no,
                    "answers": coverage.decode_answers(
                        result["answers"][position]
                    ),
                    "evidence": evidence,
                    "is_multi": item["multi_status"][position] == "M",
                }
            )
        page_count = max(
            (total_matches + coverage.EVIDENCE_PAGE_SIZE - 1)
            // coverage.EVIDENCE_PAGE_SIZE,
            1,
        )

        return render_template(
            "coverage_item.html",
            version=version,
            analysis=analysis,
            stale=stale,
            item=item,
            item_number=item_index + 1,
            item_name=definition["items"][item_index]["name"],
            condition=describe_item(definition, item_index),
            group_rows=group_rows,
            current_revision=current_revision,
            record_rows=record_rows,
            total_matches=total_matches,
            page=page,
            page_count=page_count,
            page_size=coverage.EVIDENCE_PAGE_SIZE,
            status=status,
            group_filter=group_filter,
            record_filter=record_filter,
            status_labels=coverage.STATUS_LABELS,
            answer_headers=[
                {
                    "number": index + 1,
                    "name": definition["items"][index]["name"],
                }
                for index in range(len(definition["items"]))
            ],
        )

    return app


app = create_app()
