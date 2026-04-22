from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import hashlib, json
from ..models import QueryResult, Interpretation, Clarification
from ..credit.metrics import DIMENSIONS, METRICS
from ..credit.compiler import previous_dates
from ..credit.plan import QueryPlan
from ..security import AccessScope


class ResultBuilder:
    @staticmethod
    def open_completed(state, execution, trace, explanation):
        scope = AccessScope.from_dict(state["access_scope"])
        title = str(explanation.get("title") or state["query"])
        plan = {
            "mode": "model_sql",
            "query": state["decision"].get("standalone_query") or state["query"],
            "confirmed_parameters": state.get("confirmed", {}),
            "model_interpretation": explanation,
            "knowledge_references": [
                m["id"] for m in state["knowledge"].get("metric_references", [])
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "sql": execution.sql,
                    "rows": execution.rows,
                    "columns": execution.columns,
                    "data_version": execution.data_version,
                    "scope": scope.fingerprint(),
                    "truncated": execution.truncated,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()[:24]
        retrieval = {**state["retrieval"], "knowledge": state["knowledge"]}
        return QueryResult(
            task_id=state["task_id"],
            status="completed",
            route="database_query",
            message="查询完成",
            interpretation=Interpretation(
                metric=title,
                dimension=str(explanation.get("dimensions") or "见SQL"),
                time_range=str(explanation.get("time_range") or "见SQL中的日期条件"),
                table="、".join(t["name"] for t in state["schema_graph"]["tables"]),
                metric_definition=str(
                    explanation.get("definition") or "模型生成SQL，口径需结合SQL复核"
                ),
                metric_version="开放查询 · 模型口径说明",
                assumptions=[str(a) for a in explanation.get("assumptions", [])]
                + [
                    "日期、分母和去重说明由模型生成；执行成功不等于业务口径已获独立校验。",
                    "返回完整性仅针对当前SQL；主动LIMIT、筛选和Top N不代表全量业务。",
                ],
                parameter_sources={
                    k: "user_confirmed" for k in state.get("confirmed", {})
                },
                data_as_of=execution.data_as_of,
                scope_label="授权机构：" + "、".join(sorted(scope.allowed_orgs)),
            ),
            sql=execution.sql,
            columns=execution.columns,
            rows=execution.rows,
            query_plan=plan,
            column_metadata=execution.column_metadata,
            truncated=execution.truncated,
            returned_rows=execution.returned_rows,
            total_rows=execution.total_rows,
            data_version=execution.data_version,
            data_as_of=execution.data_as_of,
            generated_at=datetime.now(timezone.utc).isoformat(),
            scope_fingerprint=scope.fingerprint(),
            result_fingerprint=fingerprint,
            retrieval=retrieval,
            schema_graph=state["schema_graph"],
            tool_calls=trace,
            execution_log=list(trace),
            result_title=title,
            standalone_query=plan["query"],
            workflow_mode="credit_open_query",
            validation={
                "sql_executed": True,
                "read_only": True,
                "scope_enforced": True,
                "complete": not execution.truncated,
                "sql_semantics": "requires_review",
                "sql_source": "model",
                "metric_contract": "reference_only",
            },
            steps=[
                "模型结合服务端上下文理解问题",
                "混合检索Schema与金融口径知识",
                "模型生成SQL并通过MCP调用工具",
                "后端执行只读与权限约束",
                "模型依据可信结果分析",
            ],
        )

    @staticmethod
    def waiting(state):
        return QueryResult(
            task_id=state["task_id"],
            status="waiting_clarification",
            route="database_query",
            message="需要补充统计口径",
            clarification=Clarification.model_validate(state["clarification"]),
            analysis=state["clarification"]["question"],
            workflow_mode="credit_clarification",
        )

    @staticmethod
    def completed(state, execution, trace):
        plan = QueryPlan.model_validate(state["plan"])
        metric = METRICS[plan.metric_id]
        scope = AccessScope.from_dict(state["access_scope"])
        time_range = (
            f"{plan.as_of} 日终"
            if metric.time_kind == "snapshot"
            else f"{plan.start_date} 至 {plan.end_date}"
        )
        if metric.time_kind == "snapshot" and "month" in plan.dimensions:
            time_range = f"{plan.start_date} 至 {plan.end_date} 各月末"
        if plan.comparison:
            other = previous_dates(plan)
            time_range += "；比较期：" + (" / ".join(str(v) for v in other.values()))
        stats = []
        if plan.comparison and not execution.truncated:
            groups = {}
            for row in execution.rows:
                key = tuple(str(row[DIMENSIONS[d]]) for d in plan.dimensions)
                groups.setdefault(key, {})[row["比较期间"]] = row[metric.label]
            for key, values in groups.items():
                current, prior = values.get("本期"), values.get("上期")
                c = Decimal(str(current)) if current is not None else None
                p = Decimal(str(prior)) if prior is not None else None
                stats.append(
                    {
                        "group": " / ".join(key) or "全部",
                        "current": current,
                        "previous": prior,
                        "relative_change": str(
                            ((c - p) / p).quantize(Decimal("0.000001"))
                        )
                        if c is not None and p not in (None, 0)
                        else None,
                        "percentage_point_change": str(
                            ((c - p) * 100).quantize(Decimal("0.01"))
                        )
                        if metric.unit == "比例" and c is not None and p is not None
                        else None,
                    }
                )
        assumptions = [
            metric.definition,
            "人民币；金额两位小数，比例内部保留六位小数。",
        ]
        if metric.id.startswith("overdue"):
            assumptions.append(
                f"逾期天数 >= {plan.dpd_threshold}；到期当日DPD为0，无宽限期。"
            )
        if plan.filters:
            assumptions.append(
                "筛选："
                + "；".join(
                    DIMENSIONS[k] + "=" + ",".join(v) for k, v in plan.filters.items()
                )
            )
        for key, source in plan.parameter_sources.items():
            if source == "default_data_cutoff":
                assumptions.append("未指定日期，默认采用数据截止月/截止日。")
        scope_label = (
            "华东机构（上海、杭州）"
            if scope.allowed_orgs == frozenset({"O1", "O2"})
            else "全部演示机构"
        )
        metadata = execution.column_metadata
        payload = {
            "plan": state["plan"],
            "sql": execution.sql,
            "rows": execution.rows,
            "data_version": execution.data_version,
            "scope": scope.fingerprint(),
            "truncated": execution.truncated,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:24]
        return QueryResult(
            task_id=state["task_id"],
            status="completed",
            route="database_query",
            message="查询完成",
            interpretation=Interpretation(
                metric=metric.label,
                dimension="、".join(DIMENSIONS[d] for d in plan.dimensions) or "无分组",
                time_range=time_range,
                table="、".join(
                    t["label"] for t in state.get("schema_graph", {}).get("tables", [])
                ),
                assumptions=assumptions,
                parameter_sources=plan.parameter_sources,
                metric_definition=metric.definition,
                metric_version=plan.metric_version,
                data_as_of=execution.data_as_of,
                scope_label=scope_label,
            ),
            sql=execution.sql,
            columns=execution.columns,
            rows=execution.rows,
            analysis=f"已按{metric.label}定义计算，返回{len(execution.rows)}行。"
            + (
                "结果为截断预览，不能作为全量汇总。"
                if execution.truncated
                else "结果完整；空值不代表零。"
            ),
            query_plan=state["plan"],
            column_metadata=metadata,
            truncated=execution.truncated,
            returned_rows=execution.returned_rows,
            total_rows=execution.total_rows,
            data_version=execution.data_version,
            data_as_of=execution.data_as_of,
            generated_at=datetime.now(timezone.utc).isoformat(),
            scope_fingerprint=scope.fingerprint(),
            result_fingerprint=fingerprint,
            validation={
                "sql_executed": True,
                "metric_contract": "passed",
                "read_only": True,
                "scope_enforced": True,
                "complete": not execution.truncated,
            },
            comparison_stats=stats,
            retrieval=state.get("retrieval"),
            schema_graph=state.get("schema_graph"),
            tool_calls=trace,
            execution_log=trace,
            result_title=metric.label + " · " + time_range,
            workflow_mode="credit_metric_query",
            steps=[
                "理解问题并绑定指标版本",
                "解析统计时间与继承条件",
                "检索金融Schema并补齐指标依赖",
                "后端编译只读SQL",
                "通过MCP执行权限受控查询",
                "记录口径、数据版本和结果指纹",
            ],
        )
