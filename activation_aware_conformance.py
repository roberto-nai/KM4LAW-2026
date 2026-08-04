#!/usr/bin/env python3
"""Activation-aware DECLARE conformance analysis for judicial event logs.

Declare4Py is the sole conformance engine. The script answers three descriptive
research questions:

RQ1. How frequently is each DECLARE constraint activated?
RQ2. What is the conformance rate among activated cases?
RQ3. How does the treatment of inactive constraints affect the global score?

The CSV-to-event-log transformation, metric extraction, aggregation, and
visualisation are implemented here; all DECLARE semantics are delegated to
Declare4Py's MPDeclareAnalyzer.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import pm4py
from pm4py.objects.conversion.log import converter as log_converter
from pm4py.objects.log.exporter.xes import exporter as xes_exporter
from pm4py.util import constants as pm4py_constants

# Some dependency combinations configure the root logger at DEBUG level during
# import, which would otherwise flood an experiment run with internal messages.
logging.getLogger().setLevel(logging.WARNING)
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)
logging.getLogger("pm4py").setLevel(logging.WARNING)

try:
    from Declare4Py.D4PyEventLog import D4PyEventLog
    from Declare4Py.ProcessMiningTasks.ConformanceChecking.MPDeclareAnalyzer import (
        MPDeclareAnalyzer,
    )
    from Declare4Py.ProcessModels.DeclareModel import DeclareModel
except ImportError as exc:  # pragma: no cover - exercised only in incomplete environments.
    raise SystemExit(
        "Declare4Py is required. Install the experiment dependencies with "
        "'python -m pip install -r requirements.txt'."
    ) from exc


DEFAULT_LOG = Path("event_log/curia_log_en.csv")
DEFAULT_MODEL = Path("declare_model/curia_model.decl")
DEFAULT_OUTPUT = Path("results")

REQUIRED_COLUMNS = ("CaseID", "Activity", "Timestamp")
DECLARE_METRICS = (
    "num_activations",
    "num_fulfillments",
    "num_violations",
    "num_pendings",
    "state",
)


@dataclass(frozen=True)
class RuleMetadata:
    """Readable metadata for one constraint parsed by Declare4Py."""

    rule_id: str
    constraint: str
    template: str
    activities: tuple[str, ...]
    activation_activity: str


@dataclass(frozen=True)
class EngineRun:
    """Matrices returned by one Declare4Py execution."""

    consider_vacuity: bool
    metrics: dict[str, pd.DataFrame]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a Declare4Py-based activation-aware conformance analysis on a "
            "semicolon-separated event log and a DECLARE model."
        )
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"Input event log (default: {DEFAULT_LOG}).",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL,
        help=f"Input DECLARE model (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Directory for generated results (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--separator",
        default=";",
        help="CSV field separator (default: ';').",
    )
    return parser.parse_args()


def load_event_log(log_path: Path, separator: str) -> pd.DataFrame:
    """Load, validate, and deterministically order the event log."""
    if not log_path.is_file():
        raise FileNotFoundError(f"Event log not found: {log_path}")

    frame = pd.read_csv(log_path, sep=separator, encoding="utf-8-sig")
    frame.columns = frame.columns.astype(str).str.strip()

    missing_columns = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing_columns:
        raise ValueError(
            f"The event log is missing required columns: {', '.join(missing_columns)}"
        )

    if frame[list(REQUIRED_COLUMNS)].isna().any().any():
        missing_counts = frame[list(REQUIRED_COLUMNS)].isna().sum()
        details = ", ".join(
            f"{column}={int(count)}" for column, count in missing_counts.items() if count
        )
        raise ValueError(f"Required fields contain missing values: {details}")

    frame = frame.copy()
    frame["CaseID"] = frame["CaseID"].astype(str).str.strip()
    frame["Activity"] = frame["Activity"].astype(str).str.strip()
    frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="raise")

    duplicated_events = int(
        frame.duplicated(subset=["CaseID", "Activity", "Timestamp"]).sum()
    )
    if duplicated_events:
        print(
            f"Warning: {duplicated_events:,} duplicate case/activity/timestamp rows "
            "were retained because they may represent repeated events."
        )

    # Activity is the deterministic tie-breaker used by the original CURIA pipeline.
    return frame.sort_values(
        ["CaseID", "Timestamp", "Activity"], kind="mergesort"
    ).reset_index(drop=True)


def to_declare4py_event_log(frame: pd.DataFrame) -> tuple[D4PyEventLog, list[str], object]:
    """Convert the input table into the event-log object consumed by Declare4Py."""
    pm_frame = frame.rename(
        columns={
            "CaseID": "case:concept:name",
            "Activity": "concept:name",
            "Timestamp": "time:timestamp",
        }
    )
    parameters = {
        log_converter.Variants.TO_EVENT_LOG.value.Parameters.CASE_ID_KEY:
            "case:concept:name"
    }
    pm_event_log = log_converter.apply(
        pm_frame,
        parameters=parameters,
        variant=log_converter.Variants.TO_EVENT_LOG,
    )

    # Recent PM4Py releases do not always propagate these standard keys when a
    # DataFrame is converted in memory. Declare4Py reads them from log metadata.
    log_properties = dict(pm_event_log.properties)
    log_properties[pm4py_constants.PARAMETER_CONSTANT_ACTIVITY_KEY] = "concept:name"
    log_properties[pm4py_constants.PARAMETER_CONSTANT_TIMESTAMP_KEY] = "time:timestamp"
    log_properties[pm4py_constants.PARAMETER_CONSTANT_CASEID_KEY] = "case:concept:name"
    pm_event_log.properties = log_properties

    case_ids: list[str] = []
    for trace in pm_event_log:
        case_id = (getattr(trace, "attributes", {}) or {}).get("concept:name")
        if case_id is None:
            raise ValueError("PM4Py produced a trace without a case identifier.")
        case_ids.append(str(case_id))

    d4py_log = D4PyEventLog(case_name="case:concept:name", log=pm_event_log)
    return d4py_log, case_ids, pm_event_log


def export_xes_log(pm_event_log: object, source_log_path: Path, output_dir: Path) -> Path:
    """Export the in-memory PM4Py event log to XES in the output directory."""
    xes_path = output_dir / f"{source_log_path.stem}.xes"
    xes_exporter.apply(pm_event_log, str(xes_path))
    return xes_path


def ensure_declare_conditions(declare_model: DeclareModel) -> None:
    """Add empty MP-DECLARE condition slots when the model omits them."""
    for constraint in declare_model.constraints:
        conditions = list(constraint.get("condition", []))
        required_slots = 3 if constraint["template"].is_binary else 2
        while len(conditions) < required_slots:
            conditions.append("")
        constraint["condition"] = conditions

    # set_constraints appends to the serialised list, so reset it first.
    declare_model.serialized_constraints = []
    declare_model.set_constraints()


def load_declare_model(model_path: Path) -> DeclareModel:
    """Parse a model with Declare4Py without requiring write access to the file."""
    if not model_path.is_file():
        raise FileNotFoundError(f"DECLARE model not found: {model_path}")
    return load_declare_model_from_text(model_path.read_text(encoding="utf-8-sig"))


def load_declare_model_from_text(content: str) -> DeclareModel:
    """Parse DECLARE source text and normalise empty condition slots."""
    model = DeclareModel().parse_from_string(content)
    if not model.constraints:
        raise ValueError("The DECLARE model contains no constraints recognised by Declare4Py.")
    ensure_declare_conditions(model)
    return model


def clean_constraint_label(serialised_constraint: str) -> str:
    """Remove MP-DECLARE condition placeholders from a display label."""
    return str(serialised_constraint).split("|", 1)[0].strip()


def describe_rules(declare_model: DeclareModel) -> list[RuleMetadata]:
    """Extract ordered, readable rule metadata from the parsed model."""
    rules: list[RuleMetadata] = []
    for index, (constraint, serialised) in enumerate(
        zip(declare_model.constraints, declare_model.serialized_constraints, strict=True),
        start=1,
    ):
        template = constraint["template"]
        activities = tuple(str(activity) for activity in constraint["activities"])
        if not activities:
            raise ValueError(f"Constraint R{index} has no activity parameters.")

        # Declare4Py marks precedence-family templates as reverse activation/target.
        activation_index = 1 if template.reverseActivationTarget else 0
        if activation_index >= len(activities):
            raise ValueError(f"Cannot determine the activation activity for R{index}.")

        rules.append(
            RuleMetadata(
                rule_id=f"R{index}",
                constraint=clean_constraint_label(serialised),
                template=template.templ_str,
                activities=activities,
                activation_activity=activities[activation_index],
            )
        )
    return rules


def run_declare4py(
    event_log: D4PyEventLog,
    declare_model: DeclareModel,
    consider_vacuity: bool,
    rule_ids: Sequence[str],
) -> EngineRun:
    """Run Declare4Py and retrieve every metric required by the experiment."""
    analyser = MPDeclareAnalyzer(event_log, declare_model, consider_vacuity)
    browser = analyser.run()
    matrices: dict[str, pd.DataFrame] = {}

    for metric in DECLARE_METRICS:
        matrix = browser.get_metric(metric=metric)
        if not isinstance(matrix, pd.DataFrame):
            matrix = pd.DataFrame(matrix)
        if matrix.shape[1] != len(rule_ids):
            raise ValueError(
                f"Declare4Py returned {matrix.shape[1]} columns for {len(rule_ids)} rules."
            )
        matrix = matrix.copy()
        matrix.columns = list(rule_ids)
        matrices[metric] = matrix

    return EngineRun(consider_vacuity=consider_vacuity, metrics=matrices)


def integer_metric(value: object) -> int:
    """Normalise an integer-like Declare4Py metric, including None/NaN pendings."""
    if value is None or pd.isna(value):
        return 0
    return int(value)


def validate_engine_runs(
    non_vacuous: EngineRun,
    vacuous: EngineRun,
    expected_traces: int,
    expected_rules: int,
) -> None:
    """Check shape and metric invariance across the two vacuity configurations."""
    expected_shape = (expected_traces, expected_rules)
    for run in (non_vacuous, vacuous):
        for metric, matrix in run.metrics.items():
            if matrix.shape != expected_shape:
                raise ValueError(
                    f"Unexpected {metric} shape {matrix.shape}; expected {expected_shape}."
                )

    for metric in DECLARE_METRICS:
        if metric == "state":
            continue
        # Declare4Py can represent undefined pending counts as None in an
        # object-typed matrix. Convert explicitly before filling missing values
        # to avoid pandas' deprecated implicit downcasting behaviour.
        left = non_vacuous.metrics[metric].astype("Float64").fillna(0)
        right = vacuous.metrics[metric].astype("Float64").fillna(0)
        if not left.equals(right):
            raise ValueError(
                f"Declare4Py metric {metric} changed with consider_vacuity; "
                "only the state metric was expected to change."
            )


def build_case_results(
    case_ids: Sequence[str],
    rules: Sequence[RuleMetadata],
    non_vacuous: EngineRun,
    vacuous: EngineRun,
) -> pd.DataFrame:
    """Create one auditable result row for every case–constraint pair."""
    records: list[dict[str, object]] = []

    for trace_index, case_id in enumerate(case_ids):
        for rule_index, rule in enumerate(rules):
            activations = integer_metric(
                non_vacuous.metrics["num_activations"].iat[trace_index, rule_index]
            )
            fulfilments = integer_metric(
                non_vacuous.metrics["num_fulfillments"].iat[trace_index, rule_index]
            )
            violations = integer_metric(
                non_vacuous.metrics["num_violations"].iat[trace_index, rule_index]
            )
            pendings = integer_metric(
                non_vacuous.metrics["num_pendings"].iat[trace_index, rule_index]
            )
            non_vacuous_state = integer_metric(
                non_vacuous.metrics["state"].iat[trace_index, rule_index]
            )
            vacuous_state = integer_metric(
                vacuous.metrics["state"].iat[trace_index, rule_index]
            )

            if activations == 0:
                outcome = "inactive"
            elif non_vacuous_state == 1:
                outcome = "satisfied"
            else:
                outcome = "violated"

            if activations > 0 and violations == 0 and non_vacuous_state != 1:
                raise ValueError(
                    f"Inconsistent Declare4Py result for case {case_id}, {rule.rule_id}."
                )
            if activations > 0 and violations > 0 and non_vacuous_state != 0:
                raise ValueError(
                    f"Inconsistent Declare4Py violation state for case {case_id}, {rule.rule_id}."
                )

            records.append(
                {
                    "CaseID": case_id,
                    "rule": rule.rule_id,
                    "constraint": rule.constraint,
                    "activation_activity": rule.activation_activity,
                    "activations": activations,
                    "fulfilments": fulfilments,
                    "violations": violations,
                    "pendings": pendings,
                    "state_non_vacuous": non_vacuous_state,
                    "state_vacuous": vacuous_state,
                    "outcome": outcome,
                }
            )

    return pd.DataFrame.from_records(records)


def calculate_rq1(
    case_results: pd.DataFrame, rules: Sequence[RuleMetadata]
) -> pd.DataFrame:
    """RQ1: activation coverage derived from Declare4Py num_activations."""
    total_cases = int(case_results["CaseID"].nunique())
    rows: list[dict[str, object]] = []

    for rule in rules:
        subset = case_results[case_results["rule"] == rule.rule_id]
        activated_cases = int((subset["activations"] > 0).sum())
        rows.append(
            {
                "rule": rule.rule_id,
                "constraint": rule.constraint,
                "activation_activity": rule.activation_activity,
                "cases": total_cases,
                "activated_cases": activated_cases,
                "activation_coverage": activated_cases / total_cases,
                "inactive_cases": total_cases - activated_cases,
                "inactivity_rate": (total_cases - activated_cases) / total_cases,
                "total_activations": int(subset["activations"].sum()),
            }
        )
    return pd.DataFrame(rows)


def calculate_rq2(
    case_results: pd.DataFrame, rules: Sequence[RuleMetadata]
) -> pd.DataFrame:
    """RQ2: conditional conformance using Declare4Py state and violations."""
    rows: list[dict[str, object]] = []

    for rule in rules:
        subset = case_results[
            (case_results["rule"] == rule.rule_id) & (case_results["activations"] > 0)
        ]
        activated_cases = len(subset)
        if activated_cases == 0:
            raise ValueError(f"Rule {rule.rule_id} has no activated cases.")
        satisfied_cases = int((subset["state_non_vacuous"] == 1).sum())
        violated_cases = int((subset["state_non_vacuous"] == 0).sum())
        rows.append(
            {
                "rule": rule.rule_id,
                "constraint": rule.constraint,
                "activated_cases": activated_cases,
                "satisfied_cases": satisfied_cases,
                "conditional_satisfaction_rate": satisfied_cases / activated_cases,
                "violated_cases": violated_cases,
                "conditional_violation_rate": violated_cases / activated_cases,
                "total_fulfilments": int(subset["fulfilments"].sum()),
                "total_violations": int(subset["violations"].sum()),
            }
        )
    return pd.DataFrame(rows)


def calculate_rq3(case_results: pd.DataFrame) -> pd.DataFrame:
    """RQ3: scores from Declare4Py's vacuous and non-vacuous states."""
    total_pairs = len(case_results)
    active = case_results[case_results["activations"] > 0]
    active_pairs = len(active)
    inactive_pairs = total_pairs - active_pairs

    rows = [
        {
            "approach": "Inactive considered satisfied",
            "numerator": int(case_results["state_vacuous"].sum()),
            "denominator": total_pairs,
            "global_score": float(case_results["state_vacuous"].mean()),
            "interpretation": "Declare4Py consider_vacuity=True",
        },
        {
            "approach": "Inactive considered non-satisfied",
            "numerator": int(case_results["state_non_vacuous"].sum()),
            "denominator": total_pairs,
            "global_score": float(case_results["state_non_vacuous"].mean()),
            "interpretation": "Declare4Py consider_vacuity=False",
        },
        {
            "approach": "Inactive excluded",
            "numerator": int(active["state_non_vacuous"].sum()),
            "denominator": active_pairs,
            "global_score": float(active["state_non_vacuous"].mean()),
            "interpretation": "Activation-aware Declare4Py state",
        },
    ]

    result = pd.DataFrame(rows)
    result.attrs["counts"] = {
        "total_pairs": total_pairs,
        "active_pairs": active_pairs,
        "inactive_pairs": inactive_pairs,
        "satisfied_active_pairs": int(active["state_non_vacuous"].sum()),
        "violated_active_pairs": int((active["state_non_vacuous"] == 0).sum()),
    }
    return result


def create_outcome_figure(
    rq1: pd.DataFrame, rq2: pd.DataFrame, output_path: Path
) -> None:
    """Create a publication-friendly stacked percentage bar chart."""
    plot_data = rq1[["rule", "cases", "inactive_cases"]].merge(
        rq2[["rule", "satisfied_cases", "violated_cases"]], on="rule", how="left"
    )
    denominator = plot_data["cases"]
    inactive = plot_data["inactive_cases"] / denominator
    satisfied = plot_data["satisfied_cases"] / denominator
    violated = plot_data["violated_cases"] / denominator

    figure, axis = plt.subplots(figsize=(8.0, 4.8))
    inactive_bars = axis.bar(
        plot_data["rule"], inactive, label="Inactive", color="#B8C2CC"
    )
    satisfied_bars = axis.bar(
        plot_data["rule"], satisfied, bottom=inactive, label="Satisfied", color="#2A9D8F"
    )
    violated_bars = axis.bar(
        plot_data["rule"],
        violated,
        bottom=inactive + satisfied,
        label="Violated",
        color="#E76F51",
    )
    axis.set_title(
        "Activation-aware outcomes by DECLARE constraint",
        fontweight="bold",
        pad=36,
    )
    axis.set_xlabel("Constraint (rule)", fontweight="bold")
    axis.set_ylabel("Share of cases", fontweight="bold")
    axis.set_ylim(0, 1)
    axis.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    axis.grid(axis="y", alpha=0.2, linewidth=0.7)
    axis.set_axisbelow(True)

    def annotate_segments(
        bars: matplotlib.container.BarContainer,
        values: pd.Series,
        bottoms: pd.Series,
    ) -> None:
        for patch, value, bottom in zip(bars.patches, values, bottoms, strict=True):
            if value <= 0:
                continue
            x_center = patch.get_x() + patch.get_width() / 2
            y_center = bottom + (value / 2)
            axis.text(
                x_center,
                y_center,
                f"{value:.0%}",
                ha="center",
                va="center",
                fontsize=7,
                fontweight="normal",
                color="#1F2933",
            )

    zero_bottom = pd.Series(0.0, index=plot_data.index)
    annotate_segments(inactive_bars, inactive, zero_bottom)
    annotate_segments(satisfied_bars, satisfied, inactive)
    annotate_segments(violated_bars, violated, inactive + satisfied)

    # Matplotlib's public API uses the American spelling for this location.
    axis.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.10))
    figure.tight_layout()
    figure.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(figure)


def package_version(distribution: str) -> str:
    """Return an installed distribution version for the reproducibility record."""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def ensure_output_directory(output_dir: Path) -> None:
    """Create a missing output directory and initialise it with .gitkeep."""
    if output_dir.exists():
        if not output_dir.is_dir():
            raise NotADirectoryError(
                f"The output path exists but is not a directory: {output_dir}"
            )
        return

    output_dir.mkdir(parents=True)
    (output_dir / ".gitkeep").touch()

def save_results(
    output_dir: Path,
    event_log: pd.DataFrame,
    rules: Sequence[RuleMetadata],
    case_results: pd.DataFrame,
    rq1: pd.DataFrame,
    rq2: pd.DataFrame,
    rq3: pd.DataFrame,
) -> None:
    """Save all reproducibility artefacts."""
    ensure_output_directory(output_dir)
    case_results.to_csv(output_dir / "case_rule_results.csv", index=False)
    rq1.to_csv(output_dir / "rq1_activation_coverage.csv", index=False)
    rq2.to_csv(output_dir / "rq2_conditional_conformance.csv", index=False)
    rq3.to_csv(output_dir / "rq3_global_scores.csv", index=False)
    create_outcome_figure(rq1, rq2, output_dir / "activation_outcomes.png")

    summary = {
        "engine": {
            "name": "Declare4Py MPDeclareAnalyzer",
            "declare4py_version": package_version("declare4py"),
            "pm4py_version": pm4py.__version__,
            "pandas_version": pd.__version__,
        },
        "input": {
            "events": int(len(event_log)),
            "cases": int(event_log["CaseID"].nunique()),
            "rules": len(rules),
        },
        "rules": [asdict(rule) for rule in rules],
        "pair_counts": rq3.attrs["counts"],
        "scores": {
            row["interpretation"]: row["global_score"]
            for row in rq3.to_dict(orient="records")
        },
    }
    (output_dir / "experiment_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def format_percentage_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a display copy with rate columns formatted as percentages."""
    display = frame.copy()
    rate_columns: Iterable[str] = (
        column
        for column in display.columns
        if column.endswith("_rate") or column.endswith("_coverage") or column == "global_score"
    )
    for column in rate_columns:
        display[column] = display[column].map(lambda value: f"{value:.2%}")
    return display


def main() -> None:
    arguments = parse_arguments()
    ensure_output_directory(arguments.output_dir)
    event_table = load_event_log(arguments.log, arguments.separator)
    d4py_log, case_ids, pm_event_log = to_declare4py_event_log(event_table)
    xes_path = export_xes_log(pm_event_log, arguments.log, arguments.output_dir)
    declare_model = load_declare_model(arguments.model)
    rules = describe_rules(declare_model)
    rule_ids = [rule.rule_id for rule in rules]

    print("Running Declare4Py with consider_vacuity=False...")
    non_vacuous = run_declare4py(d4py_log, declare_model, False, rule_ids)
    print("Running Declare4Py with consider_vacuity=True...")
    vacuous = run_declare4py(d4py_log, declare_model, True, rule_ids)
    validate_engine_runs(non_vacuous, vacuous, len(case_ids), len(rules))

    case_results = build_case_results(case_ids, rules, non_vacuous, vacuous)
    rq1 = calculate_rq1(case_results, rules)
    rq2 = calculate_rq2(case_results, rules)
    rq3 = calculate_rq3(case_results)
    save_results(
        arguments.output_dir,
        event_table,
        rules,
        case_results,
        rq1,
        rq2,
        rq3,
    )

    # print(f"\nLoaded {len(event_table):,} events from {len(case_ids):,} cases.")
    print(f"\nLoaded file: {arguments.log}")
    print(f"Exported XES log: {xes_path}")
    print(f"Loaded {len(case_ids):,} cases and {len(event_table):,} events.")
    print()
    print(f"Parsed {len(rules)} DECLARE constraints.\n")
    print("RQ1 — Activation coverage")
    print(
        format_percentage_columns(rq1)[
            ["rule", "activated_cases", "activation_coverage", "inactive_cases"]
        ].to_string(index=False)
    )
    print("\nRQ2 — Conditional conformance among activated cases")
    print(
        format_percentage_columns(rq2)[
            [
                "rule",
                "activated_cases",
                "satisfied_cases",
                "conditional_satisfaction_rate",
                "violated_cases",
                "conditional_violation_rate",
            ]
        ].to_string(index=False)
    )
    print("\nRQ3 — Global score sensitivity")
    print(
        format_percentage_columns(rq3)[
            ["approach", "numerator", "denominator", "global_score"]
        ].to_string(index=False)
    )
    print(f"\nResults written to: {arguments.output_dir.resolve()}")


if __name__ == "__main__":
    main()
