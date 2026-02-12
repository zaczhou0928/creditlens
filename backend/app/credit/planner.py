"""旧P0指标规则，仅保留用于显式离线演示与口径回归。"""

from __future__ import annotations
import re
from datetime import date, datetime, timedelta
from calendar import monthrange
from zoneinfo import ZoneInfo
from .metrics import METRICS, DIMENSIONS, knowledge_search
from .plan import QueryPlan


class NeedsClarification(Exception):
    def __init__(self, parameter, question, options):
        self.payload = {
            "parameter": parameter,
            "question": question,
            "reason": "不同选项会改变统计对象或计算口径",
            "options": [
                {
                    "id": key,
                    "label": label,
                    "description": METRICS[key].definition if key in METRICS else label,
                    "recommended": False,
                }
                for key, label in options
            ],
        }
        super().__init__(question)


class CreditPlanner:
    def __init__(self, model_client, config, manifest):
        self.model = model_client
        self.config = config
        self.manifest = manifest

    def reference_date(self):
        return (
            date.fromisoformat(self.config.credit_reference_date)
            if self.config.credit_reference_date
            else datetime.now(ZoneInfo("Asia/Shanghai")).date()
        )

    def prepare(self, query, previous=None):
        matches = []
        for metric in METRICS.values():
            for alias in set((*metric.aliases, metric.label)):
                for match in re.finditer(re.escape(alias), query):
                    matches.append((match.start(), match.end(), metric.id))
        selected = []
        for start, end, metric_id in sorted(matches, key=lambda x: x[0] - x[1]):
            if not any(
                start < other_end and end > other_start
                for other_start, other_end, _ in selected
            ):
                selected.append((start, end, metric_id))
        if len({m for _, _, m in selected}) > 1:
            return {
                "action": "direct_response",
                "response": "P0 每次查询一个指标。请分别查询这些指标，再勾选结果生成综合报告。",
            }
        if self.config.credit_demo_mode:
            return self._demo(query, previous)
        raise ValueError("CreditPlanner仅用于显式离线回归；模型模式使用开放Agent工作流")

    def resolve(self, query, decision, previous=None, confirmed=None):
        patch = dict(decision.get("patch") or {})
        allowed = {
            "metric_id",
            "dimensions",
            "filters",
            "time_expression",
            "dpd_threshold",
            "comparison",
        }
        if set(patch) - allowed:
            raise ValueError("查询包含未支持参数")
        confirmed = confirmed or {}
        inherit = bool(decision.get("follow_up") and previous)
        base = dict(previous or {}) if inherit else {}
        old_sources = base.get("parameter_sources", {})
        sources = {key: "inherited" for key in old_sources} if inherit else {}
        aliases = knowledge_search(query)
        ambiguous_overdue = "逾期率" in query and not any(
            m.label in query for m in METRICS.values()
        )
        ambiguous_customer = any(
            w in query for w in ("客户数", "新增客户", "客户增长")
        ) and not any(m.label in query for m in METRICS.values())
        if not confirmed.get("metric_id") and (ambiguous_overdue or ambiguous_customer):
            choices = (
                [
                    ("overdue_balance_ratio", "逾期贷款余额占比"),
                    ("overdue_principal_ratio", "逾期应还本金占比"),
                ]
                if ambiguous_overdue
                else [
                    ("applicant_count", "申请客户数"),
                    ("borrower_count", "放款客户数"),
                    ("new_borrower_count", "首次放款客户数"),
                ]
            )
            raise NeedsClarification("metric_id", "请选择本次统计口径", choices)
        if confirmed.get("metric_id"):
            patch["metric_id"] = confirmed["metric_id"]
        if not patch.get("metric_id"):
            patch.pop("metric_id", None)
            if not base.get("metric_id"):
                if aliases:
                    patch["metric_id"] = aliases[0]["id"]
                else:
                    raise ValueError("未识别支持的信贷指标，请参考指标目录")
        time_expression = str(patch.pop("time_expression", "")).strip()
        for key, value in patch.items():
            if key == "filters" and inherit:
                base[key] = {**base.get(key, {}), **value}
            else:
                base[key] = value
            sources[key] = "user_confirmed" if key in confirmed else "user"
        metric = METRICS.get(base.get("metric_id"))
        if not metric:
            raise ValueError("暂不支持该指标")
        base.setdefault("dimensions", [])
        base.setdefault("filters", {})
        base.setdefault("dpd_threshold", 1)
        base.setdefault("comparison", False)
        sources.setdefault("dpd_threshold", "default")
        explicit_dpd = re.search(r"(超过|大于|至少|达到|逾期)?\s*(\d+)\s*天", query)
        if explicit_dpd:
            base["dpd_threshold"] = int(explicit_dpd[2]) + (
                1 if explicit_dpd[1] in ("超过", "大于") else 0
            )
            sources["dpd_threshold"] = "user"
        if time_expression:
            start, end = self.resolve_time(time_expression, self.reference_date())
            sources["time"] = "user"
        elif inherit and (base.get("start_date") or base.get("as_of")):
            start = date.fromisoformat(str(base.get("start_date") or base["as_of"]))
            end = date.fromisoformat(str(base.get("end_date") or base["as_of"]))
            sources["time"] = "inherited"
        else:
            end = date.fromisoformat(self.manifest["data_as_of"])
            start = end.replace(day=1)
            sources["time"] = "default_data_cutoff"
        base.update(
            start_date=start
            if metric.time_kind == "period" or "month" in base["dimensions"]
            else None,
            end_date=end
            if metric.time_kind == "period" or "month" in base["dimensions"]
            else None,
            as_of=end if metric.time_kind == "snapshot" else None,
            parameter_sources=sources,
            reference_date=self.reference_date(),
        )
        return QueryPlan.model_validate(base)

    @staticmethod
    def resolve_time(text, reference):
        dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", text)
        if dates:
            parsed = [
                date.fromisoformat(
                    "-".join(
                        [
                            p.split("-")[0],
                            p.split("-")[1].zfill(2),
                            p.split("-")[2].zfill(2),
                        ]
                    )
                )
                for p in dates
            ]
            return parsed[0], parsed[-1]
        months = re.findall(r"(?:(\d{4})\s*[年-])?(\d{1,2})\s*月", text)
        if not months:
            months = re.findall(r"(\d{4})-(\d{2})(?!-\d)", text)
        if months:
            vals = [date(int(y or reference.year), int(m), 1) for y, m in months]
            start, end = vals[0], vals[-1]
            days = re.search(r"月\s*(\d{1,2})[日号]", text)
            if days and len(vals) == 1:
                d = start.replace(day=int(days[1]))
                return d, d
            return start, end.replace(day=monthrange(end.year, end.month)[1])
        if "上月" in text or "上个月" in text:
            end = reference.replace(day=1) - timedelta(days=1)
            return end.replace(day=1), end
        if "本月" in text or "这个月" in text:
            return reference.replace(day=1), reference
        if "上年" in text or "去年" in text:
            return date(reference.year - 1, 1, 1), date(reference.year - 1, 12, 31)
        if "今年" in text:
            return reference.replace(month=1, day=1), reference
        if "昨天" in text:
            return reference - timedelta(days=1), reference - timedelta(days=1)
        if "今天" in text:
            return reference, reference
        if "上周" in text:
            end = reference - timedelta(days=reference.weekday() + 1)
            return end - timedelta(days=6), end
        if "本周" in text:
            return reference - timedelta(days=reference.weekday()), reference
        raise ValueError(f"无法确定时间范围：{text}；请使用明确年月或起止日期")

    def _demo(self, query, previous):
        """显式离线演示模式，仅覆盖已声明的有限中文规则，不伪装为大模型。"""
        if any(
            w in query
            for w in ("报告", "分析刚才", "解读", "基于这些", "总结这些", "分析这些")
        ):
            return {"action": "data_qa", "report": "报告" in query}
        patch = {}
        hits = knowledge_search(query)
        if hits:
            patch["metric_id"] = hits[0]["id"]
        follow = bool(
            previous
            and any(
                w in query for w in ("再", "改", "呢", "只看", "换", "其中", "同样")
            )
        )
        if (
            not hits
            and not follow
            and not any(w in query for w in ("客户数", "新增客户", "逾期率"))
        ):
            return {
                "action": "direct_response",
                "response": "我是零售信贷分析 Agent，可查询放款、审批、余额、还款和逾期指标。离线演示模式支持示例问法；开放式问题请启用模型模式。",
            }
        dims = []
        for name, label in DIMENSIONS.items():
            aliases = {
                "product": ["产品", "贷款产品"],
                "channel": ["渠道"],
                "region": ["地区", "区域"],
                "org": ["机构"],
                "segment": ["客户群体"],
                "month": ["月度", "按月", "每月"],
            }.get(name, [label])
            if (
                any(
                    ("按" + w in query or "各" + w in query or "每个" + w in query)
                    for w in aliases
                )
                or name == "month"
                and any(w in query for w in aliases)
            ):
                dims.append(name)
        if dims:
            patch["dimensions"] = dims
        if "不分组" in query or "总计" in query:
            patch["dimensions"] = []
        filters = {}
        for dimension, values in {
            "region": ["华东", "华南", "华北"],
            "channel": ["手机银行", "线下网点", "合作渠道"],
            "product": ["薪享贷", "消费分期贷", "优享贷"],
            "org": ["上海分行", "杭州分行", "广州分行", "北京分行"],
            "segment": ["工薪", "自雇", "其他"],
        }.items():
            selected = [v for v in values if v in query]
            if selected:
                filters[dimension] = selected
        if filters:
            patch["filters"] = filters
        time_match = re.search(
            r"(\d{4}[-年]\d{1,2}[^，。]*|\d{1,2}月[^，。]*|上个月|上月|本月|今年|去年|昨天|今天|上周|本周)",
            query,
        )
        if time_match:
            patch["time_expression"] = time_match[0]
        if "环比" in query or "对比上期" in query:
            patch["comparison"] = True
        return {"action": "database_query", "follow_up": follow, "patch": patch}
