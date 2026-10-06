"""What AWS caps a resource at, for the checks that measure how close it is.

- A burstable (T) instance's CPU credits: the most it can bank, 24 hours of
  what it earns, from AWS's table. The same for an RDS `db.t*` class.
- A volume's or a database's IOPS, only where they are a hard cap: io1, io2
  and gp3. A gp2, st1 or sc1 volume bursts above its baseline, so its
  `BurstBalance` is what says it is running out, not its IOPS.
- A database's `max_connections`: its parameter group's value, or the
  engine's formula worked out for its instance class. A formula is an
  estimate: RDS keeps some of the class's memory for itself, so the real
  limit is a little lower.

Nothing here raises for a limit it cannot find: that is None, and the
threshold it would have fed is left out.
"""

import logging
import math
import re
import time

from app.monitoring.infra.aws import client
from app.monitoring.infra.aws.client import AwsError

logger = logging.getLogger(__name__)

#: CPU credits a burstable instance earns an hour, by family and size.
_T3_CREDITS = {"nano": 6, "micro": 12, "small": 24, "medium": 24, "large": 36, "xlarge": 96, "2xlarge": 192}
_CREDITS_PER_HOUR: dict[str, dict[str, float]] = {
    "t2": {"nano": 3, "micro": 6, "small": 12, "medium": 24, "large": 36, "xlarge": 54, "2xlarge": 81.6},
    "t3": _T3_CREDITS,
    "t3a": _T3_CREDITS,
    "t4g": _T3_CREDITS,
}
#: Volume (or RDS storage) types whose IOPS are a hard cap.
PROVISIONED_IOPS = frozenset({"io1", "io2", "gp3"})
#: Volume types that burst, and report `BurstBalance`.
BURSTING = frozenset({"gp2", "st1", "sc1"})
#: How long a parameter group's `max_connections` and a class's memory are
#: reused: both change only when someone edits or resizes.
CACHE_SECONDS = 3600


def _family_size(instance_type: str | None) -> tuple[str, str]:
    """`t3.micro` -> (t3, micro); an RDS class's `db.` is dropped."""
    name = (instance_type or "").removeprefix("db.")
    family, _, size = name.partition(".")
    return family, size


def is_burstable(instance_type: str | None) -> bool:
    return _family_size(instance_type)[0] in _CREDITS_PER_HOUR


def cpu_credit_max(instance_type: str | None) -> float | None:
    """The most CPU credits the instance (or RDS class) can bank; None for
    one that does not burst, or a size AWS's table does not list."""
    family, size = _family_size(instance_type)
    per_hour = _CREDITS_PER_HOUR.get(family, {}).get(size)
    return per_hour * 24 if per_hour else None


def provisioned_iops(storage_type: str | None, iops: int | float | None) -> float | None:
    """The IOPS a volume or a database's storage cannot go above."""
    if storage_type in PROVISIONED_IOPS and iops:
        return float(iops)
    return None


# --- max_connections ------------------------------------------------------------

_cache: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple) -> tuple[bool, object]:
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
        return True, hit[1]
    return False, None


def _remember(key: tuple, value: object) -> object:
    if len(_cache) > 2048:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)
    return value


class FormulaError(ValueError):
    """A parameter value this evaluator does not handle."""


_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_]\w*)|(\S))")
_FUNCTIONS = {"LEAST": min, "GREATEST": max, "SUM": lambda *args: sum(args)}


def evaluate(formula: str, variables: dict[str, float]) -> float:
    """An RDS parameter formula, e.g. `LEAST({DBInstanceClassMemory/9531392},5000)`.
    Numbers, + - * /, braces and parentheses, LEAST, GREATEST and SUM, and the
    names in `variables`. Raises FormulaError for anything else (`log`, an
    unknown name)."""
    tokens: list[str] = []
    for number, name, symbol in _TOKEN.findall(formula):
        tokens.append(number or name or symbol)
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take(expected: str | None = None) -> str:
        nonlocal position
        token = peek()
        if token is None or (expected is not None and token != expected):
            raise FormulaError(f"expected {expected or 'more'} in {formula!r}")
        position += 1
        return token

    def expression() -> float:
        value = term()
        while peek() in {"+", "-"}:
            value = value + term() if take() == "+" else value - term()
        return value

    def term() -> float:
        value = factor()
        while peek() in {"*", "/"}:
            if take() == "*":
                value *= factor()
            else:
                divisor = factor()
                if divisor == 0:
                    raise FormulaError(f"division by zero in {formula!r}")
                value /= divisor
        return value

    def factor() -> float:
        token = take()
        if token in {"(", "{"}:
            value = expression()
            take(")" if token == "(" else "}")
            return value
        if token == "-":
            return -factor()
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            return float(token)
        if token.upper() in _FUNCTIONS and peek() == "(":
            take("(")
            args = [expression()]
            while peek() == ",":
                take(",")
                args.append(expression())
            take(")")
            return float(_FUNCTIONS[token.upper()](*args))
        if token in variables:
            return float(variables[token])
        raise FormulaError(f"{token!r} is not handled in {formula!r}")

    value = expression()
    if peek() is not None:
        raise FormulaError(f"unexpected {peek()!r} in {formula!r}")
    return value


async def class_memory(region: client.Region, instance_class: str) -> tuple[int, int] | None:
    """(memory in bytes, vCPUs) of an RDS instance class, read from the EC2
    instance type it runs on (`db.r6g.large` -> `r6g.large`); None when EC2
    does not know it (`db.serverless`) or cannot be asked."""
    instance_type = instance_class.removeprefix("db.")
    found, value = _cached(("class", instance_type))
    if found:
        return value  # type: ignore[return-value]
    try:
        response = await client.call("ec2", "describe_instance_types", region, InstanceTypes=[instance_type])
    except AwsError as exc:
        logger.info("Memory of %s not known: %s", instance_class, exc)
        return _remember(("class", instance_type), None)  # type: ignore[return-value]
    types = response.get("InstanceTypes") or []
    if not types:
        return _remember(("class", instance_type), None)  # type: ignore[return-value]
    memory = int((types[0].get("MemoryInfo") or {}).get("SizeInMiB") or 0) * 1024 * 1024
    vcpus = int((types[0].get("VCpuInfo") or {}).get("DefaultVCpus") or 0)
    return _remember(("class", instance_type), (memory, vcpus) if memory else None)  # type: ignore[return-value]


async def _parameter(region: client.Region, group: str, name: str) -> str | None:
    """One parameter's value in a DB parameter group, as RDS writes it.
    Raises AwsError."""
    key = ("param", region.account, region.name, group, name)
    found, value = _cached(key)
    if found:
        return value  # type: ignore[return-value]
    parameters = await client.paginate(
        "rds", "describe_db_parameters", region, "Parameters", DBParameterGroupName=group
    )
    value = next((p.get("ParameterValue") for p in parameters if p.get("ParameterName") == name), None)
    return _remember(key, value)  # type: ignore[return-value]


async def max_connections(region: client.Region, db: dict) -> tuple[int | None, str | None]:
    """The database's `max_connections`, and where it came from (or why it
    is not known), from its parameter group. A formula is worked out for its
    class, and said to be an estimate."""
    groups = db.get("DBParameterGroups") or []
    group = groups[0].get("DBParameterGroupName") if groups else None
    if not group:
        return None, "it has no DB parameter group"
    try:
        raw = await _parameter(region, group, "max_connections")
    except AwsError as exc:
        return None, f"{group} could not be read ({exc.code})"
    if not raw:
        return None, f"{group} does not set max_connections"
    raw = raw.strip()
    if raw.isdigit():
        return int(raw), f"{group}: {raw}"
    instance_class = db.get("DBInstanceClass") or ""
    sizing = await class_memory(region, instance_class)
    if sizing is None:
        return None, f"{group} sets it as {raw}, and the memory of {instance_class or 'its class'} is not known"
    memory, vcpus = sizing
    variables = {
        "DBInstanceClassMemory": memory,
        "DBInstanceVCPU": vcpus,
        "AllocatedStorage": (db.get("AllocatedStorage") or 0) * 1024**3,
    }
    try:
        value = evaluate(raw, variables)
    except FormulaError:
        return None, f"{group} sets it as {raw}, which Watchly cannot work out"
    if not math.isfinite(value) or value < 1:
        return None, f"{group} sets it as {raw}, which comes to {value:g}"
    return int(value), f"about {int(value)} by {group}'s {raw} for {instance_class}"
