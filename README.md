# Activation-aware DECLARE conformance analysis of CURIA event logs

This repository contains a compact and reproducible experiment on the effect of
constraint activation and vacuous satisfaction in declarative conformance
checking. The case study uses an event log reconstructed from public InfoCuria
metadata and five DECLARE constraints describing judicial procedural behaviour.

The experiment is deliberately descriptive. It does not require manual
annotation, machine learning, or calls to external services.

## InfoCuria
[https://infocuria.curia.europa.eu/tabs/tout?lang=EN](https://infocuria.curia.europa.eu/tabs/tout?lang=EN)


## Repository structure

```text
.
├── activation_aware_conformance.py   # Script to be executed
├── declare_model/
│   └── curia_model.decl              # DECLARE models
├── event_log/
│   └── curia_log_en.csv              # CURIA event log 
├── results/                          # Results created by the script (include the XES version of the event log)
└── requirements.txt                  # Libraries needed
```

`curia_log_en.csv` in the `event_log` directory, or provide another path with the `--log` option.

## Requirements

- Python 3.10 or later
- Declare4Py 2.2.0
- PM4Py 2.7
- pandas
- matplotlib

Declare4Py's `MPDeclareAnalyzer` is the sole conformance engine. The script does
not reimplement any DECLARE constraint. pandas prepares the event table and
aggregates the metrics returned by Declare4Py; PM4Py converts the table into an
event log; matplotlib creates the result figure.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Running the experiment

Using the default repository layout:

```bash
python activation_aware_conformance.py
```

Using explicit paths:

```bash
python activation_aware_conformance.py \
  --log event_log/curia_log_en.csv \
  --model declare_model/curia_model.decl \
  --output-dir results
```

The input CSV is semicolon-separated by default. A different separator can be
specified with `--separator`.

## Input data schema

The script requires three columns:

| Column | Description |
|---|---|
| `CaseID` | Identifier shared by all events belonging to one proceeding |
| `Activity` | Controlled event label used by the DECLARE constraints |
| `Timestamp` | Date or date-time used to order events within each case |

Additional columns are retained in the source file but are not required by this
experiment. Events are ordered by `CaseID`, `Timestamp`, and `Activity`, using a
stable sort. Activity provides a deterministic tie-breaker when two events have
the same timestamp.

## DECLARE semantics

The supplied model uses the following standard finite-trace templates:

- `Precedence[A, B]`: every occurrence of `B` must have an occurrence of `A`
  earlier in the trace. `B` is the activation.
- `Response[A, B]`: every occurrence of `A` must have an occurrence of `B`
  later in the trace. `A` is the activation.
- `NotResponse[A, B]`: no occurrence of `B` may follow an occurrence of `A`.
  `A` is the activation.

A trace–constraint pair is classified as:

- **inactive** when it has no activation;
- **satisfied** when it is activated and has no violation;
- **violated** when at least one activation violates the constraint.

These outcomes are derived exclusively from Declare4Py metrics:

- `num_activations` identifies inactive pairs;
- `num_fulfillments` and `num_violations` provide diagnostics;
- `state` provides the binary satisfaction result.

Declare4Py returns `state=1` for a satisfied constraint and `state=0`
otherwise. The script never interprets `state=1` as a violation.

## Conformance engine configuration

The script executes the same model twice with `MPDeclareAnalyzer`:

```python
MPDeclareAnalyzer(log, model, consider_vacuity=False)
MPDeclareAnalyzer(log, model, consider_vacuity=True)
```

The activation, fulfilment, violation, and pending counts must be identical in
both runs; the script checks this condition and stops if it is not met. Only
the `state` matrix is expected to change for inactive pairs.

RQ1 is computed from Declare4Py's activation matrix. RQ2 combines the
activation, violation, and non-vacuous state matrices. RQ3 compares the two
Declare4Py state matrices and then filters the non-vacuous matrix to active
pairs for the activation-aware score.

## Measures

### RQ1: activation coverage

For rule \(r\) and \(N\) traces:

```text
activation coverage(r) = activated traces(r) / N
```

### RQ2: conditional conformance

```text
conditional satisfaction(r) = satisfied activated traces(r) / activated traces(r)
conditional violation(r)    = violated activated traces(r) / activated traces(r)
```

### RQ3: global score sensitivity

Three treatments of inactive trace–constraint pairs are compared:

1. **Inactive considered satisfied**: mean Declare4Py state with
   `consider_vacuity=True`.
2. **Inactive considered non-satisfied**: mean Declare4Py state with
   `consider_vacuity=False`.
3. **Inactive excluded**: mean non-vacuous Declare4Py state over pairs with
   `num_activations > 0`.

## Outputs

The script creates:

| File | Contents |
|---|---|
| `case_rule_results.csv` | Declare4Py activations, fulfilments, violations, pendings, both states, and outcome |
| `rq1_activation_coverage.csv` | Activation coverage by rule |
| `rq2_conditional_conformance.csv` | Satisfaction and violation among activated cases |
| `rq3_global_scores.csv` | Global scores under the three inactivity treatments |
| `experiment_summary.json` | Machine-readable experiment metadata and headline results |
| `activation_outcomes.png` | Stacked outcome figure suitable for a paper draft |

## Expected results for the supplied CURIA snapshot

The supplied snapshot contains 4,744 events, 1,284 cases, and five constraints.

### RQ1

| Rule | Activated cases | Activation coverage | Inactive cases |
|---|---:|---:|---:|
| R1 | 703 | 54.75% | 581 |
| R2 | 703 | 54.75% | 581 |
| R3 | 624 | 48.60% | 660 |
| R4 | 703 | 54.75% | 581 |
| R5 | 703 | 54.75% | 581 |

### RQ2

| Rule | Satisfied | Conditional satisfaction | Violated | Conditional violation |
|---|---:|---:|---:|---:|
| R1 | 129 | 18.35% | 574 | 81.65% |
| R2 | 561 | 79.80% | 142 | 20.20% |
| R3 | 168 | 26.92% | 456 | 73.08% |
| R4 | 110 | 15.65% | 593 | 84.35% |
| R5 | 703 | 100.00% | 0 | 0.00% |

### RQ3

| Treatment of inactive constraints | Global score |
|---|---:|
| Inactive considered satisfied | 72.51% |
| Inactive considered non-satisfied | 26.03% |
| Inactive excluded | 48.63% |

The exact values are provided as a reproducibility check. They may change if the
event log or the DECLARE model is updated.


## Interpretation and limitations

The reported outcomes measure conformance between the formal constraints and
the events observable in the public log. They must not be interpreted as legal
findings of procedural non-compliance. An inactive or violated constraint may
reflect the scope of the rule, optional procedural steps, missing public
metadata, or the selected event abstraction.

The method uses exact activity-label matching. Changes to the activity
vocabulary must therefore be reflected in the DECLARE model.

## Reproducibility

For a reproducible release, archive together:

- the exact event-log snapshot;
- the supplied DECLARE model;
- the generated `experiment_summary.json`;
- the Python and dependency versions used for the run.

The experiment is deterministic and does not use random seeds.

## Contacts
Roberto Nai - [roberto.nai@unito.it](roberto.nai@unito.it)