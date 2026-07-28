#!/usr/bin/env python3
"""Deterministic contracts for visible-part fidelity reviews.

Global image scores are deliberately insufficient here.  A report must review each
important visible part locally, prove the expected pose/attachment when requested,
close known defects with before/after evidence, and carry fingerprints for the code
that produced the reviewed geometry.  Fingerprints make a previously passing review
stale as soon as the part or one of its declared dependencies changes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VISIBLE_STATES = {"full", "partial", "fragment"}
IMPORTANCE_LEVELS = {"high", "medium", "low"}
CHECK_KINDS = {
    "presence",
    "silhouette",
    "contour",
    "proportion",
    "position",
    "pose",
    "attachment",
    "overlap",
    "material",
}
REVIEW_VERDICTS = {"pass", "fail", "accepted-approximation"}
DEFECT_KINDS = {
    "presence",
    "primitive-family",
    "contour",
    "proportion",
    "position",
    "pose",
    "attachment",
    "overlap",
    "material",
}
DEFAULT_CHANGE_GROUPS = {
    "presence": [{"presence", "component", "geometry"}],
    "primitive-family": [{"primitive-family", "topology", "geometry"}],
    "contour": [{"contour", "profile", "control-points", "silhouette"}],
    "proportion": [{"proportion", "dimensions", "scale"}],
    "position": [{"position", "transform", "landmarks"}],
    "pose": [{"pose", "orientation", "transform", "endpoints"}],
    "attachment": [{"attachment", "parent", "socket", "root-contact"}],
    "overlap": [{"overlap", "occlusion", "depth-order"}],
    "material": [{"material", "roughness", "color", "normal", "texture"}],
}


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def visible_part_policy(spec: dict[str, Any]) -> dict[str, Any]:
    loop = spec.get("selfCorrectLoop")
    acceptance = loop.get("visualAcceptance") if isinstance(loop, dict) else None
    policy = acceptance.get("visiblePartContractPolicy") if isinstance(acceptance, dict) else None
    return policy if isinstance(policy, dict) else {}


def visible_part_gate_enabled(spec: dict[str, Any]) -> bool:
    return visible_part_policy(spec).get("enabled") is True


def visible_part_contracts_for_pass(
    spec: dict[str, Any], pass_id: str
) -> list[dict[str, Any]]:
    contracts = spec.get("visiblePartContracts", [])
    if not isinstance(contracts, list):
        return []
    result: list[dict[str, Any]] = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        pass_ids = contract.get("passIds", [])
        if isinstance(pass_ids, list) and pass_id in pass_ids:
            result.append(contract)
    return result


def contract_requires_review(contract: dict[str, Any]) -> bool:
    if contract.get("mustReview") is not None:
        return contract.get("mustReview") is True
    return (
        contract.get("visibility") in VISIBLE_STATES
        and contract.get("identityImportance") in {"high", "medium"}
    )


def required_visible_part_contracts(
    spec: dict[str, Any], pass_id: str
) -> list[dict[str, Any]]:
    if not visible_part_gate_enabled(spec):
        return []
    return [
        contract
        for contract in visible_part_contracts_for_pass(spec, pass_id)
        if contract_requires_review(contract)
    ]


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def contracts_digest(contracts: list[dict[str, Any]]) -> str:
    return _canonical_digest(contracts)


def _artifact_path(base_dir: Path, reference: str) -> Path:
    # ``file.ts#symbol`` is a useful authoring reference.  Fingerprinting remains
    # conservative and hashes the whole file, so any edit invalidates the review.
    file_part = reference.split("#", 1)[0].strip()
    if not file_part or "://" in file_part:
        raise ValueError(f"artifact reference must be a local file path: {reference!r}")
    path = Path(file_part).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def artifact_hashes(references: Any, base_dir: Path) -> dict[str, str]:
    if not isinstance(references, list):
        return {}
    hashes: dict[str, str] = {}
    for raw in references:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = _artifact_path(base_dir, raw)
        if not path.is_file():
            raise FileNotFoundError(f"visible-part artifact does not exist: {path}")
        hashes[raw] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _threshold_for(contract: dict[str, Any], check_id: str, policy: dict[str, Any]) -> float:
    per_check = contract.get("minimumScores")
    if isinstance(per_check, dict) and is_number(per_check.get(check_id)):
        return float(per_check[check_id])
    defaults = policy.get("localCheckDefaultThresholds")
    importance = str(contract.get("identityImportance") or "medium")
    if isinstance(defaults, dict) and is_number(defaults.get(importance)):
        return float(defaults[importance])
    return 0.8 if importance == "high" else 0.7 if importance == "medium" else 0.5


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _review_evidence_failures(
    part_id: str, review: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    if policy.get("requireLocalEvidence", True) is not True:
        return []
    evidence = review.get("evidence")
    if not isinstance(evidence, dict):
        return [f"part:{part_id}:missing-local-evidence"]
    failures = []
    for field in ("reference", "render", "comparison", "cameraView"):
        if not _non_empty_string(evidence.get(field)):
            failures.append(f"part:{part_id}:evidence-missing-{field}")
    if policy.get("requireSameCamera", True) is True and evidence.get("sameCamera") is not True:
        failures.append(f"part:{part_id}:evidence-not-same-camera")
    return failures


def _check_failures(
    contract: dict[str, Any], review: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    part_id = str(contract.get("id") or "(unnamed)")
    required = contract.get("requiredChecks", [])
    checks = review.get("checks")
    failures: list[str] = []
    if not isinstance(checks, dict):
        return [f"part:{part_id}:checks-missing"]
    for check_id in required if isinstance(required, list) else []:
        if check_id not in CHECK_KINDS:
            continue
        result = checks.get(check_id)
        if not isinstance(result, dict):
            failures.append(f"part:{part_id}:check-missing:{check_id}")
            continue
        verdict = result.get("verdict")
        accepted = verdict == "accepted-approximation" and contract.get("allowApproximation") is True
        if verdict != "pass" and not accepted:
            failures.append(f"part:{part_id}:check-failed:{check_id}")
            continue
        score = result.get("score")
        threshold = _threshold_for(contract, check_id, policy)
        if not is_number(score):
            failures.append(f"part:{part_id}:check-score-missing:{check_id}")
        elif float(score) < threshold and not accepted:
            failures.append(
                f"part:{part_id}:check-below-threshold:{check_id}:{float(score):.3f}<{threshold:.3f}"
            )
    return failures


def _attachment_failures(contract: dict[str, Any], review: dict[str, Any]) -> list[str]:
    required_checks = contract.get("requiredChecks", [])
    expected = contract.get("expectedAttachment")
    if "attachment" not in required_checks and not isinstance(expected, dict):
        return []
    part_id = str(contract.get("id") or "(unnamed)")
    actual = review.get("attachment")
    if not isinstance(actual, dict):
        return [f"part:{part_id}:attachment-evidence-missing"]
    failures: list[str] = []
    expected = expected if isinstance(expected, dict) else {}
    parent = expected.get("parentRef")
    socket = expected.get("socketRef")
    if _non_empty_string(parent) and actual.get("parentRef") != parent:
        failures.append(
            f"part:{part_id}:wrong-parent:{actual.get('parentRef')!r}!={parent!r}"
        )
    if _non_empty_string(socket) and actual.get("socketRef") != socket:
        failures.append(
            f"part:{part_id}:wrong-socket:{actual.get('socketRef')!r}!={socket!r}"
        )
    if expected.get("rootContactRequired", True) is True and actual.get("rootContact") is not True:
        failures.append(f"part:{part_id}:root-contact-failed")
    return failures


def _pose_failures(contract: dict[str, Any], review: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    required_checks = contract.get("requiredChecks", [])
    if "pose" not in required_checks:
        return []
    part_id = str(contract.get("id") or "(unnamed)")
    pose = review.get("pose")
    if not isinstance(pose, dict) or not is_number(pose.get("orientationDeltaDegrees")):
        return [f"part:{part_id}:pose-orientation-missing"]
    expected = contract.get("expectedAttachment")
    maximum = expected.get("maxOrientationDeltaDegrees") if isinstance(expected, dict) else None
    if not is_number(maximum):
        maximum = policy.get("maxOrientationDeltaDegrees", 12.0)
    if not is_number(maximum):
        maximum = 12.0
    delta = float(pose["orientationDeltaDegrees"])
    if delta > float(maximum):
        return [f"part:{part_id}:pose-delta:{delta:.3f}>{float(maximum):.3f}"]
    return []


def _change_groups(defect: dict[str, Any]) -> list[set[str]]:
    raw_groups = defect.get("requiredChangeGroups")
    if isinstance(raw_groups, list):
        groups = [
            {str(item) for item in group if isinstance(item, str) and item.strip()}
            for group in raw_groups
            if isinstance(group, list)
        ]
        if groups:
            return groups
    raw_properties = defect.get("requiredChangeProperties")
    if isinstance(raw_properties, list):
        values = {str(item) for item in raw_properties if isinstance(item, str) and item.strip()}
        if values:
            return [values]
    return DEFAULT_CHANGE_GROUPS.get(str(defect.get("kind")), [])


def _metric_improved(metric: Any) -> bool:
    if not isinstance(metric, dict):
        return False
    before = metric.get("before")
    after = metric.get("after")
    if not is_number(before) or not is_number(after):
        return False
    minimum = metric.get("minimumImprovement", 0.0)
    minimum = float(minimum) if is_number(minimum) else 0.0
    direction = metric.get("direction")
    if direction == "lower-is-better":
        return float(before) - float(after) > minimum
    if direction == "higher-is-better":
        return float(after) - float(before) > minimum
    return False


def _defect_failures(
    spec: dict[str, Any], contract: dict[str, Any], review: dict[str, Any], pass_id: str,
    policy: dict[str, Any],
) -> list[str]:
    if policy.get("requireDefectClosure", True) is not True:
        return []
    part_id = str(contract.get("id") or "(unnamed)")
    defects = spec.get("visualDefects", [])
    relevant: list[dict[str, Any]] = []
    if isinstance(defects, list):
        for defect in defects:
            if not isinstance(defect, dict) or defect.get("partId") != part_id:
                continue
            if defect.get("status", "open") != "open":
                continue
            pass_ids = defect.get("passIds")
            if isinstance(pass_ids, list) and pass_ids and pass_id not in pass_ids:
                continue
            relevant.append(defect)
    if not relevant:
        return []
    resolutions = review.get("defectResolutions")
    by_id = {
        item.get("id"): item
        for item in resolutions
        if isinstance(item, dict) and _non_empty_string(item.get("id"))
    } if isinstance(resolutions, list) else {}
    failures: list[str] = []
    for defect in relevant:
        defect_id = str(defect.get("id") or "(unnamed)")
        resolution = by_id.get(defect_id)
        if not isinstance(resolution, dict):
            failures.append(f"part:{part_id}:defect-unreviewed:{defect_id}")
            continue
        accepted = (
            resolution.get("status") == "accepted-approximation"
            and contract.get("allowApproximation") is True
            and _non_empty_string(resolution.get("approximationNote"))
        )
        if resolution.get("status") != "resolved" and not accepted:
            failures.append(f"part:{part_id}:defect-open:{defect_id}")
            continue
        changed = {
            str(item)
            for item in resolution.get("changedProperties", [])
            if isinstance(item, str) and item.strip()
        } if isinstance(resolution.get("changedProperties"), list) else set()
        for group in _change_groups(defect):
            if not (changed & group):
                failures.append(
                    f"part:{part_id}:defect-wrong-change:{defect_id}:needs-one-of-{','.join(sorted(group))}"
                )
        if not _non_empty_string(resolution.get("beforeEvidence")):
            failures.append(f"part:{part_id}:defect-before-evidence-missing:{defect_id}")
        if not _non_empty_string(resolution.get("afterEvidence")):
            failures.append(f"part:{part_id}:defect-after-evidence-missing:{defect_id}")
        if resolution.get("sameCamera") is not True:
            failures.append(f"part:{part_id}:defect-camera-changed:{defect_id}")
        if policy.get("requireMetricImprovement", True) is True and not _metric_improved(resolution.get("metric")):
            failures.append(f"part:{part_id}:defect-not-improved:{defect_id}")
    return failures


def _fingerprint_failures(
    contract: dict[str, Any], review: dict[str, Any], policy: dict[str, Any], base_dir: Path | None,
) -> list[str]:
    part_id = str(contract.get("id") or "(unnamed)")
    artifact_refs = contract.get("artifactRefs", [])
    dependency_refs = contract.get("dependencyArtifactRefs", [])
    failures: list[str] = []
    if policy.get("requireArtifactFingerprints", True) is True and not artifact_refs:
        failures.append(f"part:{part_id}:artifact-refs-missing")
        return failures
    if base_dir is None:
        return failures
    for refs, field, label in (
        (artifact_refs, "artifactHashes", "artifact"),
        (dependency_refs, "dependencyHashes", "dependency"),
    ):
        try:
            current = artifact_hashes(refs, base_dir)
        except (FileNotFoundError, ValueError) as exc:
            failures.append(f"part:{part_id}:{label}-fingerprint-error:{exc}")
            continue
        reviewed = review.get(field)
        if not isinstance(reviewed, dict):
            failures.append(f"part:{part_id}:{label}-hashes-missing")
            continue
        for reference, digest in current.items():
            if reviewed.get(reference) != digest:
                failures.append(f"part:{part_id}:stale-{label}:{reference}")
    return failures


def evaluate_visible_part_report(
    spec: dict[str, Any],
    report: Any,
    pass_id: str,
    spec_dir: Path | None = None,
) -> dict[str, Any]:
    """Evaluate a captured report against the current contracts and source files."""
    if not visible_part_gate_enabled(spec):
        return {"passed": True, "action": "continue", "failedGates": [], "parts": []}
    contracts = required_visible_part_contracts(spec, pass_id)
    if not contracts:
        return {"passed": True, "action": "continue", "failedGates": [], "parts": []}
    failures: list[str] = []
    if not isinstance(report, dict):
        return {
            "passed": False,
            "action": "refine-code",
            "failedGates": ["visible-part-report-missing"],
            "parts": [],
        }
    if report.get("schemaVersion") != 1:
        failures.append("visible-part-report-schema")
    if report.get("passId") != pass_id:
        failures.append(f"visible-part-report-pass:{report.get('passId')!r}!={pass_id!r}")
    expected_digest = contracts_digest(visible_part_contracts_for_pass(spec, pass_id))
    if report.get("contractsDigest") != expected_digest:
        failures.append("visible-part-contracts-stale")
    parts = report.get("parts")
    part_list = parts if isinstance(parts, list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for part in part_list:
        if not isinstance(part, dict) or not _non_empty_string(part.get("id")):
            failures.append("visible-part-report-invalid-part")
            continue
        part_id = str(part["id"])
        if part_id in by_id:
            failures.append(f"visible-part-report-duplicate:{part_id}")
        by_id[part_id] = part

    policy = visible_part_policy(spec)
    summaries: list[dict[str, Any]] = []
    for contract in contracts:
        part_id = str(contract.get("id") or "(unnamed)")
        part_failures: list[str] = []
        review = by_id.get(part_id)
        if not isinstance(review, dict):
            part_failures.append(f"part:{part_id}:review-missing")
        else:
            verdict = review.get("status")
            accepted = (
                verdict == "accepted-approximation"
                and contract.get("allowApproximation") is True
                and _non_empty_string(review.get("approximationNote"))
            )
            if verdict != "pass" and not accepted:
                part_failures.append(f"part:{part_id}:status:{verdict!r}")
            part_failures.extend(_review_evidence_failures(part_id, review, policy))
            part_failures.extend(_check_failures(contract, review, policy))
            part_failures.extend(_attachment_failures(contract, review))
            part_failures.extend(_pose_failures(contract, review, policy))
            part_failures.extend(_defect_failures(spec, contract, review, pass_id, policy))
            part_failures.extend(_fingerprint_failures(contract, review, policy, spec_dir))
        failures.extend(part_failures)
        summaries.append({"id": part_id, "passed": not part_failures, "failures": part_failures})
    return {
        "passed": not failures,
        "action": "continue" if not failures else "refine-code",
        "failedGates": failures,
        "parts": summaries,
        "contractsDigest": expected_digest,
    }


def stamp_visible_part_report(
    spec: dict[str, Any],
    pass_id: str,
    parts: Any,
    spec_dir: Path,
) -> dict[str, Any]:
    """Capture review data together with the current contract and artifact hashes."""
    if isinstance(parts, dict):
        parts = parts.get("parts")
    if not isinstance(parts, list):
        raise ValueError("visible-part review input must be an array or an object with a parts array")
    contracts = visible_part_contracts_for_pass(spec, pass_id)
    by_id = {
        item.get("id"): item
        for item in contracts
        if isinstance(item, dict) and _non_empty_string(item.get("id"))
    }
    stamped: list[dict[str, Any]] = []
    for item in parts:
        if not isinstance(item, dict):
            raise ValueError("visible-part review entries must be objects")
        part_id = item.get("id")
        contract = by_id.get(part_id)
        if not isinstance(contract, dict):
            raise ValueError(f"visible-part review id has no contract for {pass_id!r}: {part_id!r}")
        copy = dict(item)
        copy["artifactHashes"] = artifact_hashes(contract.get("artifactRefs", []), spec_dir)
        copy["dependencyHashes"] = artifact_hashes(
            contract.get("dependencyArtifactRefs", []), spec_dir
        )
        stamped.append(copy)
    report = {
        "schemaVersion": 1,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "passId": pass_id,
        "contractsDigest": contracts_digest(contracts),
        "parts": stamped,
    }
    result = evaluate_visible_part_report(spec, report, pass_id, spec_dir)
    report["verdict"] = "pass" if result["passed"] else "fail"
    report["action"] = result["action"]
    report["failedGates"] = result["failedGates"]
    return report


def visible_part_gate_failures(
    spec: dict[str, Any],
    entry: dict[str, Any],
    pass_id: str,
    spec_dir: Path | None = None,
) -> list[str]:
    report = entry.get("visiblePartReport")
    return evaluate_visible_part_report(spec, report, pass_id, spec_dir)["failedGates"]


def visible_part_spec_gaps(spec: dict[str, Any], pass_id: str | None = None) -> list[str]:
    """Return authoring gaps that must be fixed before the visual gate can be trusted."""
    if not visible_part_gate_enabled(spec):
        return []
    policy = visible_part_policy(spec)
    contracts = spec.get("visiblePartContracts")
    if not isinstance(contracts, list):
        return ["visiblePartContracts must be an array"]
    gaps: list[str] = []
    ids: set[str] = set()
    component_refs: set[str] = set()
    detail_refs: set[str] = set()
    for index, contract in enumerate(contracts):
        label = f"visiblePartContracts[{index}]"
        if not isinstance(contract, dict):
            gaps.append(f"{label} must be an object")
            continue
        contract_id = contract.get("id")
        if not _non_empty_string(contract_id):
            gaps.append(f"{label}.id is required")
            continue
        contract_id = str(contract_id)
        if contract_id in ids:
            gaps.append(f"duplicate visible-part contract {contract_id!r}")
        ids.add(contract_id)
        visibility = contract.get("visibility")
        if visibility not in VISIBLE_STATES | {"occluded", "inferred"}:
            gaps.append(f"contract {contract_id!r} has invalid visibility")
        if contract.get("identityImportance") not in IMPORTANCE_LEVELS:
            gaps.append(f"contract {contract_id!r} has invalid identityImportance")
        pass_ids = contract.get("passIds")
        if not isinstance(pass_ids, list) or not all(_non_empty_string(item) for item in pass_ids):
            gaps.append(f"contract {contract_id!r} needs passIds")
        checks = contract.get("requiredChecks")
        if not isinstance(checks, list) or not checks:
            gaps.append(f"contract {contract_id!r} needs requiredChecks")
        elif any(item not in CHECK_KINDS for item in checks):
            gaps.append(f"contract {contract_id!r} has unknown requiredChecks")
        refs = contract.get("componentRefs", [])
        if isinstance(refs, list):
            component_refs.update(str(item) for item in refs if _non_empty_string(item))
        refs = contract.get("detailRefs", [])
        if isinstance(refs, list):
            detail_refs.update(str(item) for item in refs if _non_empty_string(item))
        applicable = pass_id is None or (isinstance(pass_ids, list) and pass_id in pass_ids)
        if (
            applicable
            and contract_requires_review(contract)
            and policy.get("requireArtifactFingerprints", True) is True
            and not contract.get("artifactRefs")
        ):
            gaps.append(f"contract {contract_id!r} needs artifactRefs for stale-review detection")
        expected = contract.get("expectedAttachment")
        if isinstance(checks, list) and "attachment" in checks:
            if not isinstance(expected, dict) or not _non_empty_string(expected.get("parentRef")):
                gaps.append(f"contract {contract_id!r} attachment check needs expectedAttachment.parentRef")

    if pass_id in {None, "structural-pass", "form-refinement"}:
        threshold = policy.get("componentImportanceThreshold", 0.65)
        threshold = float(threshold) if is_number(threshold) else 0.65
        for component in spec.get("componentTree", []):
            if not isinstance(component, dict) or component.get("visibleReviewExempt") is True:
                continue
            component_id = component.get("id")
            importance = component.get("importance")
            visibility = component.get("referenceVisibility", "full")
            if (
                _non_empty_string(component_id)
                and is_number(importance)
                and float(importance) >= threshold
                and visibility in VISIBLE_STATES
                and component_id not in component_refs
            ):
                gaps.append(f"important visible component {component_id!r} has no visible-part contract")
        assessment = spec.get("preSpecAssessment")
        inventory = assessment.get("detailInventory") if isinstance(assessment, dict) else None
        details = inventory.get("details", []) if isinstance(inventory, dict) else []
        for detail in details if isinstance(details, list) else []:
            if not isinstance(detail, dict):
                continue
            detail_id = detail.get("id")
            if (
                _non_empty_string(detail_id)
                and detail.get("identityImportance") in {"high", "medium"}
                and detail_id not in detail_refs
            ):
                gaps.append(f"identity-relevant detail {detail_id!r} has no visible-part contract")
    defects = spec.get("visualDefects", [])
    if isinstance(defects, list):
        for defect in defects:
            if not isinstance(defect, dict):
                gaps.append("visualDefects entries must be objects")
                continue
            defect_id = defect.get("id")
            part_id = defect.get("partId")
            if not _non_empty_string(defect_id) or not _non_empty_string(part_id):
                gaps.append("visual defects need id and partId")
            elif part_id not in ids:
                gaps.append(f"visual defect {defect_id!r} references unknown part {part_id!r}")
            if defect.get("kind") not in DEFECT_KINDS:
                gaps.append(f"visual defect {defect_id!r} has invalid kind")
    return gaps
