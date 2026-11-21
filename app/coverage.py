"""Rule coverage analysis for stored inspection records.

The analysis is deliberately read-only with respect to versions, records and
judgments.  It evaluates every record against every completion of its blank
answers and records, for each condition group, whether the group is

* ``S`` satisfied in *every* completion,
* ``E`` excluded (satisfied in no completion), or
* ``U`` undetermined (satisfied in some completions but not others).

Every record's status string, along with its original answers, is kept inside
the stored result so the summary counts and the record-level evidence always
come from the same snapshot.
"""

from __future__ import annotations

from .judge import evaluate_masks
from .version_def import describe_group

STATUS_LABELS = {
    "S": "满足",
    "E": "排除",
    "U": "无法确定",
}

ANSWER_CODES = {"是": "Y", "否": "N", "": "-"}
CODE_ANSWERS = {code: answer for answer, code in ANSWER_CODES.items()}

# How many multi-group records are listed with examples on the summary page.
MULTI_EXAMPLE_LIMIT = 10
# Page size for the per-item evidence page.
EVIDENCE_PAGE_SIZE = 100


def encode_answers(answers: list[str]) -> str:
    return "".join(ANSWER_CODES.get(answer, "-") for answer in answers)


def decode_answers(code: str) -> list[str]:
    return [CODE_ANSWERS.get(char, "") for char in code]


def _normalise_answers(definition: dict, answers: list[str]) -> list[str]:
    target = len(definition["items"])
    padded = list(answers[:target])
    if len(padded) < target:
        padded.extend([""] * (target - len(padded)))
    return padded


def _status_char(mask: int, all_ones: int) -> str:
    if mask == 0:
        return "E"
    if mask == all_ones:
        return "S"
    return "U"


def _lowest_bit(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _assignment(bit: int, blank: list[int]) -> dict[int, str]:
    return {
        item_index: ("是" if (bit >> position) & 1 else "否")
        for position, item_index in enumerate(blank)
    }


def analyse(definition: dict, records: list[tuple[str, list[str]]]) -> dict:
    """Build a JSON-serialisable coverage snapshot.

    ``records`` is ordered and contains ``(record_no, answers)`` pairs.  The
    result aligns ``record_nos``, ``answers`` and every group's ``status``
    string by record position.
    """
    items = definition["items"]
    item_count = len(items)
    record_count = len(records)

    record_nos: list[str] = []
    answer_codes: list[str] = []
    group_status: list[list[list[str]]] = [
        [[] for _ in item["groups"]] for item in items
    ]
    multi_status: list[list[str]] = [[] for _ in items]

    for record_no, raw_answers in records:
        answers = _normalise_answers(definition, raw_answers)
        answer_map = {
            index: value
            for index, value in enumerate(answers)
            if value in ("是", "否")
        }
        all_ones, _displayed, group_masks, _categories = evaluate_masks(
            definition, answer_map
        )

        record_nos.append(record_no)
        answer_codes.append(encode_answers(answers))

        for item_index in range(item_count):
            masks = group_masks[item_index]
            for group_index, mask in enumerate(masks):
                group_status[item_index][group_index].append(
                    _status_char(mask, all_ones)
                )

            # Under different supplements the set of satisfied groups can
            # differ only when at least two groups are satisfiable in some
            # completion and their completion masks are not identical.  Then
            # one record may hit one group under some supplements and another
            # group under others.
            satisfiable_masks = [mask for mask in masks if mask]
            is_multi = (
                len(satisfiable_masks) >= 2
                and len(set(satisfiable_masks)) > 1
            )
            multi_status[item_index].append("M" if is_multi else "-")

    result_items = []
    never_groups = []
    multi_groups = []

    for item_index, item in enumerate(items):
        groups = []
        for group_index, statuses in enumerate(group_status[item_index]):
            status_text = "".join(statuses)
            satisfied = status_text.count("S")
            excluded = status_text.count("E")
            undetermined = status_text.count("U")
            groups.append(
                {
                    "group_index": group_index,
                    "label": f"第 {group_index + 1} 组",
                    "condition": describe_group(
                        definition, item_index, group_index
                    ),
                    "satisfied": satisfied,
                    "excluded": excluded,
                    "undetermined": undetermined,
                    "total": record_count,
                    "status": status_text,
                    # A group is "never satisfiable" when no stored record can
                    # satisfy it under any supplement.  "U" means some
                    # supplement does satisfy it, so it does not count.
                    "never": satisfied == 0 and undetermined == 0,
                }
            )
            if satisfied == 0 and undetermined == 0:
                # Every record's status is "E", so all record numbers are
                # witnesses.  The page lists them from the aligned status
                # strings; only the count is needed here.
                never_groups.append(
                    {
                        "item_index": item_index,
                        "group_index": group_index,
                        "condition": groups[-1]["condition"],
                        "record_count": record_count,
                    }
                )

        item_multi_status = "".join(multi_status[item_index])
        multi_indices = [
            position
            for position, marker in enumerate(item_multi_status)
            if marker == "M"
        ]
        examples = []
        for position in multi_indices[:MULTI_EXAMPLE_LIMIT]:
            examples.append(
                {
                    "record_no": record_nos[position],
                    "groups": [
                        group["group_index"]
                        for group in groups
                        if group["status"][position] != "E"
                    ],
                }
            )
        if multi_indices:
            multi_groups.append(
                {
                    "item_index": item_index,
                    "multi_count": len(multi_indices),
                    "examples": examples,
                }
            )

        result_items.append(
            {
                "index": item_index,
                "group_count": len(groups),
                "groups": groups,
                "multi_count": len(multi_indices),
                "multi_status": item_multi_status,
                "multi_examples": examples,
            }
        )

    return {
        "record_count": record_count,
        "record_nos": record_nos,
        "answers": answer_codes,
        "items": result_items,
        "never_groups": never_groups,
        "multi_groups": multi_groups,
        "status_labels": STATUS_LABELS,
    }


def _excluded_reason(definition: dict, answer_map: dict[int, str],
                     displayed: list[int], item_index: int,
                     group_index: int) -> str:
    group = definition["items"][item_index]["groups"][group_index]
    reasons = []
    for reference, required in group:
        if displayed[reference] == 0:
            reasons.append(f"第 {reference + 1} 项在记录中始终不显示")
        elif reference in answer_map and answer_map[reference] != required:
            reasons.append(
                f"第 {reference + 1} 项原答案为{answer_map[reference]}，"
                f"不满足“={required}”"
            )
    if reasons:
        return "；".join(reasons) + "。"
    undecided = [
        reference for reference, _ in group if reference not in answer_map
    ]
    if undecided:
        names = "、".join(f"第 {index + 1} 项" for index in undecided)
        return f"取决于留空项（{names}）的补答，但补答无法使该组成立。"
    return "该组全部要求无法同时满足。"


def _undetermined_reason(
    definition: dict,
    answer_map: dict[int, str],
    all_ones: int,
    mask: int,
) -> str:
    item_count = len(definition["items"])
    blank = [index for index in range(item_count) if index not in answer_map]
    satisfying = _lowest_bit(mask)
    failing = _lowest_bit(all_ones & ~mask)
    sat_values = _assignment(satisfying, blank)
    fail_values = _assignment(failing, blank)
    pivots = [
        index
        for index in blank
        if sat_values[index] != fail_values[index]
    ]
    parts = []
    for index in pivots:
        parts.append(
            f"第 {index + 1} 项留空，补答{sat_values[index]}时满足、"
            f"补答{fail_values[index]}时不满足"
        )
    if not parts:
        return "该组是否满足取决于留空项的补答。"
    return "；".join(parts) + "。"


def explain_record(
    definition: dict, answer_code: str, item_index: int
) -> list[dict]:
    """Explain one item's group statuses for one stored record."""
    answers = _normalise_answers(definition, decode_answers(answer_code))
    answer_map = {
        index: value
        for index, value in enumerate(answers)
        if value in ("是", "否")
    }
    all_ones, displayed, group_masks, _categories = evaluate_masks(
        definition, answer_map
    )
    explanations = []
    for group_index, mask in enumerate(group_masks[item_index]):
        status = _status_char(mask, all_ones)
        if status == "S":
            text = "所有补答下均满足该组条件。"
        elif status == "E":
            text = "所有补答下均不满足该组条件。" + _excluded_reason(
                definition, answer_map, displayed, item_index, group_index
            )
        else:
            text = "该组是否满足取决于留空项。" + _undetermined_reason(
                definition,
                answer_map,
                all_ones,
                mask,
            )
        explanations.append(
            {
                "group_index": group_index,
                "condition": describe_group(definition, item_index, group_index),
                "status": status,
                "label": STATUS_LABELS[status],
                "text": text,
            }
        )
    return explanations


__all__ = [
    "STATUS_LABELS",
    "analyse",
    "decode_answers",
    "encode_answers",
    "explain_record",
]
