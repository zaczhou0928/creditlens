"""模型分析可信结果；数值通过引用或Decimal计算，显式离线模式保留模板演示。"""

from __future__ import annotations
from dataclasses import dataclass, field
import json
import re
from decimal import Decimal
from ..models import AnalysisReport, VisualizationSpec


@dataclass
class DataQaResult:
    action: str
    answer: str
    report: AnalysisReport | None = None
    tool_calls: list[dict] = field(default_factory=list)


def display(value, meta):
    if value is None:
        return "—（无分母或无数据）"
    if meta.get("kind") == "dimension":
        return str(value)
    n = Decimal(str(value))
    unit = meta.get("unit", "")
    if meta.get("kind") == "ratio":
        return f"{n * 100:.2f}%"
    return f"{n:,.{meta.get("precision", 2)}f}{unit}"


class DataQaAgent:
    def __init__(self, model_client=None, client_factory=None, skill=None):
        self.model = model_client
        self.client_factory = client_factory
        self.skill = skill

    def run_model(self, query, sources, scope, report=None):
        if not sources:
            return DataQaResult(
                "answer", "还没有可用的后端查询结果，请先查询或选择结果。"
            )
        if (
            len({r.data_version for r in sources}) != 1
            or len({r.data_as_of for r in sources}) != 1
        ):
            raise ValueError("所选结果的数据版本或截止日不同，请重新查询")
        facts, catalog = {}, []
        for index, result in enumerate(sources, 1):
            source_id = f"S{index}"
            ref = {
                "id": source_id,
                "task_id": result.task_id,
                "title": result.result_title,
                "sql": result.sql,
                "query_plan": result.query_plan,
                "truncated": result.truncated,
                "data_version": result.data_version,
                "data_as_of": result.data_as_of,
                "result_fingerprint": result.result_fingerprint,
                "columns": result.columns,
                "column_metadata": result.column_metadata,
                "rows": [],
            }
            for row_index, row in enumerate(result.rows):
                cells = {}
                for col_index, col in enumerate(result.columns):
                    fact_id = f"{source_id}.r{row_index}.c{col_index}"
                    facts[fact_id] = {
                        "fact_id": fact_id,
                        "source_task_id": result.task_id,
                        "result_fingerprint": result.result_fingerprint,
                        "row": row_index,
                        "column": col,
                        "value": row[col],
                        "source_id": source_id,
                        "numeric": result.column_metadata.get(col, {}).get("kind")
                        in {"number", "money", "count", "ratio"},
                    }
                    cells[col] = {"value": row[col], "ref": fact_id}
                ref["rows"].append(cells)
            for key, value in (
                ("data_as_of", result.data_as_of),
                ("returned_rows", result.returned_rows),
            ):
                fact_id = f"{source_id}.meta.{key}"
                facts[fact_id] = {
                    "fact_id": fact_id,
                    "source_task_id": result.task_id,
                    "source_id": source_id,
                    "value": value,
                    "numeric": False,
                    "result_fingerprint": result.result_fingerprint,
                }
            catalog.append(ref)
        client = self.client_factory(scope)
        tools = [
            t for t in client.list_tools() if t["name"] in self.skill.allowed_tools
        ]
        traces, feedback = [], []
        for _ in range(6):
            payload = self.model.chat_json(
                self.skill.instructions,
                json.dumps(
                    {
                        "query": query,
                        "report_requested": report,
                        "sources": catalog,
                        "calculated_facts": [
                            f for f in facts.values() if f.get("calculation")
                        ],
                        "chart_tools": tools,
                        "feedback": feedback,
                    },
                    ensure_ascii=False,
                ),
            )
            if payload.get("action") == "calculate":
                if any(r.truncated for r in sources):
                    feedback.append(
                        "来源有截断，拒绝在预览上计算；说明范围或让用户重新聚合查询。"
                    )
                    continue
                try:
                    operands = payload["operands"]
                    if not isinstance(operands, list) or not 1 <= len(operands) <= 1000:
                        raise ValueError("计算需要1至1000个事实引用")
                    selected = [facts[x] for x in operands]
                    if not all(
                        f.get("numeric") and f["value"] is not None for f in selected
                    ):
                        raise ValueError("仅能计算非空数值事实，不允许常量或文本")
                    values = [Decimal(str(f["value"])) for f in selected]
                    op = payload["operation"]
                    if op == "sum":
                        value = sum(values, Decimal(0))
                    elif op == "average":
                        value = sum(values, Decimal(0)) / len(values)
                    elif op == "min":
                        value = min(values)
                    elif op == "max":
                        value = max(values)
                    elif op == "subtract" and len(values) == 2:
                        value = values[0] - values[1]
                    elif op == "divide" and len(values) == 2:
                        value = values[0] / values[1] if values[1] else None
                    else:
                        raise ValueError("不支持的计算操作或操作数数量")
                    fact_id = f"C{len(traces) + 1}"
                    facts[fact_id] = {
                        "fact_id": fact_id,
                        "value": str(value) if value is not None else None,
                        "numeric": True,
                        "calculation": {"operation": op, "operands": operands},
                        "source_task_ids": sorted(
                            {
                                s
                                for f in selected
                                for s in f.get(
                                    "source_task_ids", [f.get("source_task_id")]
                                )
                                if s
                            }
                        ),
                    }
                    traces.append(
                        {
                            "tool": "calculate_verified_facts",
                            "arguments": payload,
                            "result": facts[fact_id],
                            "success": True,
                        }
                    )
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    feedback.append(str(exc))
                continue
            try:
                action = payload.get("action")
                if action not in {"answer", "report"} or (
                    report is False and action != "answer"
                ):
                    raise ValueError("请返回要求的answer或report动作")
                answer = self._render_grounded(
                    str(payload.get("answer") or ""), facts, catalog
                )
                if not answer.strip():
                    raise ValueError("回答不能为空")
                if action == "answer":
                    return DataQaResult("answer", answer, tool_calls=traces)
                title = self._render_grounded(
                    str(payload.get("title") or "信贷分析报告"), facts, catalog
                )
                markdown = self._render_grounded(
                    str(payload.get("markdown") or ""), facts, catalog
                )
                if not markdown.strip():
                    raise ValueError("报告正文不能为空")
                charts = []
                calls = payload.get("tool_calls") or []
                if len(calls) > self.skill.max_tool_calls:
                    raise ValueError("图表数量超过限制")
                for call in calls:
                    name, args = call["name"], call["arguments"]
                    if name not in {t["name"] for t in tools}:
                        raise ValueError("图表工具不可用")
                    source = next(
                        (r for r in sources if r.task_id == args.get("source_task_id")),
                        None,
                    )
                    if source is None or source.truncated:
                        raise ValueError("图表来源不存在或已截断")
                    if (
                        args.get("category_field") not in source.columns
                        or args.get("value_field") not in source.columns
                    ):
                        raise ValueError("图表字段不存在")
                    if source.column_metadata.get(args["value_field"], {}).get(
                        "kind"
                    ) not in {"number", "money", "count", "ratio"}:
                        raise ValueError("图表值必须为数值列")
                    categories = [str(r[args["category_field"]]) for r in source.rows]
                    if len(set(categories)) != len(categories):
                        raise ValueError("图表类别重复，请先按类别聚合或使用表格")
                    chart = VisualizationSpec.model_validate(
                        client.call_tool(name, args)
                    )
                    charts.append(chart)
                    traces.append(
                        {
                            "tool": name,
                            "arguments": args,
                            "result": chart.model_dump(),
                            "success": True,
                        }
                    )
                refs = [{k: v for k, v in c.items() if k != "rows"} for c in catalog]
                markdown += "\n\n### 查询来源\n\n" + "\n".join(
                    f"- [{c['id']}] 查询 `{c['task_id']}`，指纹 `{c['result_fingerprint']}`，数据截止 {c['data_as_of']}。"
                    + (
                        "截断预览，不能推断全量。"
                        if c["truncated"]
                        else "范围以该来源SQL为准。"
                    )
                    for c in catalog
                )
                return DataQaResult(
                    "report",
                    answer,
                    AnalysisReport(
                        title=title,
                        markdown=markdown,
                        visualizations=charts,
                        sources=refs,
                        facts=list(facts.values()),
                        completeness="partial"
                        if any(r.truncated for r in sources)
                        else "complete",
                    ),
                    traces,
                )
            except (KeyError, TypeError, ValueError) as exc:
                feedback.append(str(exc))
        detail = feedback[-1] if feedback else "未返回可用的分析结果"
        raise ValueError(f"模型分析未满足事实引用或工具约束：{detail}")

    @staticmethod
    def _render_grounded(text, facts, catalog):
        pattern = r"\{\{([\w.]+)\}\}"
        refs = re.findall(pattern, text)
        if any(ref not in facts for ref in refs):
            raise ValueError("文本引用不存在的事实")
        remainder = re.sub(pattern, "", text)
        source_ids = {c["id"] for c in catalog}
        if any(s not in source_ids for s in re.findall(r"\[(S\d+)\]", remainder)):
            raise ValueError("文本引用不存在的来源")
        remainder = re.sub(r"\[S\d+\]", "", remainder)
        ungrounded_numbers = re.findall(r"\d+(?:\.\d+)?", remainder)
        if ungrounded_numbers:
            raise ValueError(
                "数值、日期和数字编号必须通过{{事实ID}}引用；不能自行写数或心算。"
                f"检测到未引用数字：{', '.join(dict.fromkeys(ungrounded_numbers))[:160]}。"
                "如果年份、月份没有对应事实ID，改用“所选期间”；列表使用无序符号。"
            )

        def substitute(match):
            f = facts[match.group(1)]
            value = "无数据或无可比分母" if f["value"] is None else str(f["value"])
            # Values are data, not executable Markdown or HTML.
            value = re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", value).replace("\n", " ")
            return value + (
                f" [{f['source_id']}]"
                if f.get("source_id")
                else f" [计算:{f['fact_id']}]"
            )

        return re.sub(pattern, substitute, text)

    def run_verified(self, query, sources, report=False):
        if not sources:
            return DataQaResult(
                "answer",
                "还没有可用的后端查询结果。请先查询指标，再生成报告；前端上传的表格不能作为已验证来源。",
            )
        versions = {r.data_version for r in sources}
        cutoffs = {r.data_as_of for r in sources}
        if len(versions) > 1 or len(cutoffs) > 1:
            raise ValueError("所选结果的数据版本或截止日不同，请重新查询后再生成报告")
        facts = []
        source_refs = []
        blocks = []
        charts = []
        observations = []
        partial = False
        for index, result in enumerate(sources, 1):
            interp = result.interpretation
            if not result.query_plan or not interp:
                continue
            ref = {
                "task_id": result.task_id,
                "title": result.result_title,
                "sql": result.sql,
                "query_plan": result.query_plan,
                "data_version": result.data_version,
                "data_as_of": result.data_as_of,
                "metric_version": interp.metric_version,
                "result_fingerprint": result.result_fingerprint,
                "generated_at": result.generated_at,
                "truncated": result.truncated,
                "column_metadata": result.column_metadata,
            }
            source_refs.append(ref)
            metric_column = interp.metric
            dim_cols = [
                c
                for c, m in result.column_metadata.items()
                if m.get("kind") == "dimension"
            ]
            rows = result.rows
            label = f"S{index}"
            lines = [
                f"### {label} · {result.result_title}",
                f"统计范围：{interp.time_range}。口径：{interp.metric_definition}。",
                f"数据范围：{interp.scope_label}。数据截止：{result.data_as_of}。指标版本：{interp.metric_version}。",
            ]
            if result.truncated:
                partial = True
                lines.append(
                    "**该结果为截断预览，不能用于全量汇总、整体排名或占比结论。**"
                )
            if not rows:
                lines.append("当前范围没有记录；未将缺失的分组自动补为零。")
            else:
                lines.extend(
                    [
                        "",
                        "| " + " | ".join(result.columns) + " |",
                        "| " + " | ".join(["---"] * len(result.columns)) + " |",
                    ]
                )
                for row_idx, row in enumerate(rows):
                    values = []
                    for col in result.columns:
                        values.append(
                            display(
                                row[col],
                                result.column_metadata.get(col, {"kind": "dimension"}),
                            ).replace("|", "\\|")
                        )
                        if (
                            result.column_metadata.get(col, {}).get("kind")
                            != "dimension"
                        ):
                            facts.append(
                                {
                                    "fact_id": f"{label}.r{row_idx}.{col}",
                                    "source_task_id": result.task_id,
                                    "row": row_idx,
                                    "column": col,
                                    "value": row[col],
                                    "metric_id": result.query_plan["metric_id"],
                                    "metric_version": interp.metric_version,
                                    "result_fingerprint": result.result_fingerprint,
                                }
                            )
                    if row_idx < 20:
                        lines.append("| " + " | ".join(values) + " |")
                if len(rows) > 20:
                    lines.append(
                        f"报告表格展示前20行，已返回结果共{len(rows)}行；完整性见结果状态。"
                    )
                if not result.truncated and not result.query_plan.get("comparison"):
                    valid = [r for r in rows if r.get(metric_column) is not None]
                    if valid:
                        top = max(valid, key=lambda r: Decimal(str(r[metric_column])))
                        group = " / ".join(str(top[c]) for c in dim_cols) or "当前范围"
                        observations.append(
                            f"{label}：{group}的{metric_column}为{display(top[metric_column], result.column_metadata[metric_column])}"
                            + ("，在当前查询分组中最高。" if dim_cols else "。")
                        )
                for stat in result.comparison_stats:
                    change = (
                        "无可比基数"
                        if stat["relative_change"] is None
                        else f"{Decimal(str(stat['relative_change'])) * 100:.2f}%"
                    )
                    extra = (
                        f"，相差{stat['percentage_point_change']}个百分点"
                        if stat.get("percentage_point_change") is not None
                        else ""
                    )
                    lines.append(f"环比：{stat['group']}，相对变化{change}{extra}。")
                    facts.append(
                        {
                            "fact_id": f"{label}.comparison.{len(facts)}",
                            "source_task_id": result.task_id,
                            "calculation": stat,
                            "result_fingerprint": result.result_fingerprint,
                        }
                    )
                if (
                    len(dim_cols) == 1
                    and dim_cols != ["比较期间"]
                    and not result.truncated
                    and not result.query_plan.get("comparison")
                    and 1 < len(rows) <= 30
                ):
                    charts.append(
                        VisualizationSpec(
                            type="line" if dim_cols == ["月份"] else "bar",
                            title=result.result_title or interp.metric,
                            source_task_id=result.task_id,
                            category_field=dim_cols[0],
                            value_field=metric_column,
                            max_items=min(30, max(3, len(rows))),
                        )
                    )
            lines.extend(
                [
                    "",
                    f"来源：查询 `{result.task_id}`，结果指纹 `{result.result_fingerprint}`。",
                ]
            )
            blocks.append("\n".join(lines))
        if not source_refs:
            return DataQaResult(
                "answer", "选中的结果不包含可验证的指标定义，请重新查询。"
            )
        answer = (
            "\n".join(observations)
            or "已核对可用查询结果。部分结果为空或不完整，请查看来源状态。"
        )
        if "为什么" in query or "原因" in query:
            answer += "\n现有结果仅支持描述差异，尚不能据此确定因果；需要对应的审批策略、客群或业务事件证据。"
        if not report:
            return DataQaResult("answer", answer)
        title = "零售信贷经营与贷后指标分析报告"
        markdown = (
            f"# {title}\n\n合成演示数据；数据截止：{sources[0].data_as_of}。\n\n## 关键发现\n\n{answer}\n\n"
            + "\n\n".join(blocks)
            + "\n\n## 数据说明\n\n报告仅引用所列查询结果，各节独立遵循其统计期间和指标口径。不同口径、期间或单位没有直接合并。无分母显示为空；数据缺失不补零。结论描述已观测差异，不推断因果。"
        )
        return DataQaResult(
            "report",
            answer,
            AnalysisReport(
                title=title,
                markdown=markdown,
                visualizations=charts,
                sources=source_refs,
                facts=facts,
                completeness="partial" if partial else "complete",
            ),
        )
