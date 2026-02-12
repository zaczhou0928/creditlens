from __future__ import annotations
from datetime import date
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from .metrics import METRICS, DIMENSIONS, METRIC_VERSION


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: str
    dimensions: list[str] = Field(default_factory=list, max_length=2)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None
    dpd_threshold: int = Field(default=1, ge=1, le=36500)
    comparison: bool = False
    metric_version: str = METRIC_VERSION
    parameter_sources: dict[str, str] = Field(default_factory=dict)
    reference_date: date | None = None

    @field_validator("metric_id")
    @classmethod
    def metric(cls, v):
        if v not in METRICS:
            raise ValueError("暂不支持该指标，请从指标目录选择")
        return v

    @field_validator("metric_version")
    @classmethod
    def version(cls, v):
        if v != METRIC_VERSION:
            raise ValueError("指标版本已变化，请重新查询")
        return v

    @field_validator("dimensions")
    @classmethod
    def dimensions_valid(cls, v):
        if len(set(v)) != len(v) or any(x not in DIMENSIONS for x in v):
            raise ValueError("分组维度无效")
        return v

    @field_validator("filters")
    @classmethod
    def filters_valid(cls, v):
        if any(
            k not in DIMENSIONS
            or k == "month"
            or not vals
            or len(vals) > 20
            or any(not isinstance(x, str) or len(x) > 80 for x in vals)
            for k, vals in v.items()
        ):
            raise ValueError("筛选条件无效")
        return v

    @model_validator(mode="after")
    def dates_valid(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("统计结束日期不能早于开始日期")
        m = METRICS[self.metric_id]
        if m.time_kind == "snapshot":
            if not self.as_of:
                raise ValueError("需要明确余额/逾期指标的观察日期")
            if "month" in self.dimensions and (
                not self.start_date or not self.end_date
            ):
                raise ValueError("月末趋势需要明确起止月份")
        elif (
            not self.start_date or not self.end_date or self.end_date < self.start_date
        ):
            raise ValueError("需要有效的统计起止日期")
        if self.comparison and "month" in self.dimensions:
            raise ValueError("月度趋势与单期环比分开查询，避免比较期间错位")
        return self
