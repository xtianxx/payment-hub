#!/usr/bin/env python3
"""Read-only CPU placement checks for the isolated payment benchmark lane.

The plan is deliberately fixed for the 200 rps benchmark run:

    k6       0-1   (one physical core, both SMT siblings)
    postgres 2-5   (two physical cores)
    app      6-9   (two physical cores)

WSL2 exposes guest vCPUs.  Pinning a guest vCPU says nothing about which host
physical CPU runs it; this module records that limitation instead of claiming
host-level isolation.  Sibling pairs are never split: every CPU in a group
must have all of its thread siblings in the same group.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_GROUPS = {
    "k6": "0-1",
    "postgres": "2-5",
    "app": "6-9",
}
WSL_SCOPE_NOTE = (
    "Guest-visible WSL2 affinity only. The Windows host scheduler may still "
    "co-schedule these vCPUs on the same host physical CPUs; do not describe "
    "this as host-level physical isolation."
)


class CpuPlanError(RuntimeError):
    """The CPU plan cannot be proven on this machine."""


def parse_cpu_set(value: str) -> set[int]:
    """Parse the kernel's compact CPU-list syntax, e.g. '0-1,6,8-9'."""
    if not isinstance(value, str) or not value.strip():
        raise CpuPlanError("empty CPU set")
    result: set[int] = set()
    for part in value.strip().split(","):
        part = part.strip()
        if not part:
            raise CpuPlanError(f"empty item in CPU set: {value!r}")
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2 or not pieces[0].isdigit() or not pieces[1].isdigit():
                raise CpuPlanError(f"invalid CPU range: {part!r}")
            start, end = int(pieces[0]), int(pieces[1])
            if end < start:
                raise CpuPlanError(f"descending CPU range: {part!r}")
            cpu_range = set(range(start, end + 1))
        else:
            if not part.isdigit():
                raise CpuPlanError(f"invalid CPU number: {part!r}")
            cpu_range = {int(part)}
        overlap = result & cpu_range
        if overlap:
            raise CpuPlanError(f"duplicate CPU in set: {sorted(overlap)}")
        result |= cpu_range
    if not result:
        raise CpuPlanError(f"empty CPU set: {value!r}")
    return result


def format_cpu_set(cpus: set[int] | frozenset[int]) -> str:
    """Render a set of CPU numbers in compact kernel syntax."""
    if not cpus:
        raise CpuPlanError("cannot format an empty CPU set")
    ordered = sorted(cpus)
    ranges: list[str] = []
    start = previous = ordered[0]
    for cpu in ordered[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def parse_lscpu_topology(text: str) -> dict[int, tuple[int, int]]:
    """Parse `lscpu -p=CPU,CORE,SOCKET` into cpu -> (socket, core)."""
    topology: dict[int, tuple[int, int]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(",")
        if len(fields) < 3:
            raise CpuPlanError(f"unexpected lscpu row: {raw!r}")
        try:
            cpu, core, socket = (int(fields[0]), int(fields[1]), int(fields[2]))
        except ValueError as exc:
            raise CpuPlanError(f"non-numeric lscpu row: {raw!r}") from exc
        if cpu in topology:
            raise CpuPlanError(f"duplicate CPU in lscpu output: {cpu}")
        topology[cpu] = (socket, core)
    if not topology:
        raise CpuPlanError("lscpu returned no CPU topology")
    return topology


def sibling_groups(topology: dict[int, tuple[int, int]]) -> dict[tuple[int, int], set[int]]:
    groups: dict[tuple[int, int], set[int]] = {}
    for cpu, key in topology.items():
        groups.setdefault(key, set()).add(cpu)
    return groups


def read_topology() -> tuple[dict[int, tuple[int, int]], str]:
    try:
        proc = subprocess.run(
            ["lscpu", "-p=CPU,CORE,SOCKET"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CpuPlanError(f"cannot run lscpu: {exc}") from exc
    if proc.returncode != 0:
        raise CpuPlanError(
            f"lscpu failed with exit {proc.returncode}: {proc.stderr.strip()}"
        )
    return parse_lscpu_topology(proc.stdout), proc.stdout


def read_guest_allowed_cpus() -> set[int]:
    try:
        allowed = os.sched_getaffinity(0)
    except (AttributeError, OSError) as exc:
        raise CpuPlanError(f"cannot read guest CPU affinity: {exc}") from exc
    if not allowed:
        raise CpuPlanError("guest CPU affinity is empty")
    return set(allowed)


def read_process_affinity(pid: int) -> set[int]:
    path = Path(f"/proc/{pid}/status")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CpuPlanError(f"cannot read {path}: {exc}") from exc
    for line in text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return parse_cpu_set(line.split(":", 1)[1].strip())
    raise CpuPlanError(f"{path} has no Cpus_allowed_list")


def read_process_cpu_ticks(pid: int) -> tuple[int, int]:
    """Return (utime_ticks, stime_ticks) from /proc/<pid>/stat."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as exc:
        raise CpuPlanError(f"cannot read /proc/{pid}/stat: {exc}") from exc
    close = text.rfind(")")
    if close < 0:
        raise CpuPlanError(f"unexpected /proc/{pid}/stat content")
    fields = text[close + 2:].split()
    # fields[0] is state (field 3); utime/stime are fields 14/15.
    if len(fields) < 13:
        raise CpuPlanError(f"short /proc/{pid}/stat content")
    try:
        return int(fields[11]), int(fields[12])
    except ValueError as exc:
        raise CpuPlanError(f"non-numeric /proc/{pid}/stat times: {exc}") from exc


def clock_ticks_per_second() -> int:
    try:
        value = int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError) as exc:
        raise CpuPlanError(f"cannot read SC_CLK_TCK: {exc}") from exc
    if value <= 0:
        raise CpuPlanError("invalid SC_CLK_TCK")
    return value


def validate_groups(
    groups: dict[str, str],
    topology: dict[int, tuple[int, int]],
    guest_allowed: set[int],
) -> dict:
    parsed: dict[str, set[int]] = {}
    reasons: list[str] = []
    for name, mask in groups.items():
        try:
            parsed[name] = parse_cpu_set(mask)
        except CpuPlanError as exc:
            reasons.append(f"group {name}: {exc}")
    if reasons:
        raise CpuPlanError("; ".join(reasons))

    names = list(parsed)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            overlap = parsed[left] & parsed[right]
            if overlap:
                reasons.append(
                    f"groups {left} and {right} overlap on {sorted(overlap)}"
                )

    siblings = sibling_groups(topology)
    for name, cpus in parsed.items():
        missing = cpus - set(topology)
        if missing:
            reasons.append(f"group {name} has CPUs missing from lscpu: {sorted(missing)}")
        outside_guest = cpus - guest_allowed
        if outside_guest:
            reasons.append(
                f"group {name} is outside the guest-allowed set: {sorted(outside_guest)}"
            )
        for cpu in sorted(cpus):
            key = topology.get(cpu)
            if key is None:
                continue
            full_pair = siblings[key]
            if len(full_pair) != 2:
                reasons.append(
                    f"group {name} uses CPU {cpu} whose sibling set "
                    f"is not a two-thread core: {sorted(full_pair)}"
                )
                continue
            if not full_pair.issubset(cpus):
                reasons.append(
                    f"group {name} splits sibling pair {sorted(full_pair)} at CPU {cpu}"
                )

    if reasons:
        raise CpuPlanError("; ".join(reasons))

    return {
        "groups": {
            name: {
                "mask": format_cpu_set(cpus),
                "cpus": sorted(cpus),
                "physical_cores": sorted(
                    {topology[cpu] for cpu in cpus if cpu in topology}
                ),
            }
            for name, cpus in parsed.items()
        },
        "sibling_pairs": [
            sorted(pair) for pair in sorted(siblings.values(), key=lambda p: min(p))
        ],
        "guest_allowed": format_cpu_set(guest_allowed),
        "guest_allowed_cpus": sorted(guest_allowed),
        "wsl_scope_note": WSL_SCOPE_NOTE,
        "validation": "each group is a union of complete sibling pairs and groups are disjoint",
    }


def verify_taskset_child(cpus: set[int]) -> set[int]:
    mask = format_cpu_set(cpus)
    try:
        proc = subprocess.run(
            [
                "taskset", "-c", mask, "sh", "-c",
                "grep '^Cpus_allowed_list' /proc/self/status",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CpuPlanError(f"cannot run taskset check: {exc}") from exc
    if proc.returncode != 0:
        raise CpuPlanError(
            f"taskset child check failed with exit {proc.returncode}: {proc.stderr.strip()}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            observed = parse_cpu_set(line.split(":", 1)[1].strip())
            if observed != cpus:
                raise CpuPlanError(
                    f"taskset child effective mask {format_cpu_set(observed)} "
                    f"differs from requested {mask}"
                )
            return observed
    raise CpuPlanError("taskset child did not report Cpus_allowed_list")


def build_plan() -> dict:
    topology, raw = read_topology()
    guest_allowed = read_guest_allowed_cpus()
    validated = validate_groups(DEFAULT_GROUPS, topology, guest_allowed)
    k6_cpus = set(validated["groups"]["k6"]["cpus"])
    observed = verify_taskset_child(k6_cpus)
    validated["checks"] = {
        "taskset_available": True,
        "k6_taskset_child_effective": format_cpu_set(observed),
        "topology_source": "lscpu -p=CPU,CORE,SOCKET",
        "lscpu_rows": len(topology),
    }
    validated["schema_version"] = SCHEMA_VERSION
    validated["valid"] = True
    return validated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the fixed CPU placement plan.")
    parser.add_argument("--json", action="store_true", help="print JSON (default)")
    args = parser.parse_args(argv)
    try:
        plan = build_plan()
    except CpuPlanError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
