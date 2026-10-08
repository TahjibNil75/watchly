"""How well an Application Load Balancer is guarded at its edge: the AWS WAF
Web ACL in front of it, and the ALB's own hardening settings.

Discovery keeps what AWS says in the resource's `aws_detail` (`waf`,
`attributes`, and each listener's TLS policy and redirect). Grading happens
when the detail is read (`grade`), so the rules can change without touching
stored data, as with a site's security headers.

An item is `ok`, `weak` (there, but protects little), `missing`, `unknown`
(AWS would not say: a permission is missing) or `info` (does not apply, e.g.
a WAF on an internal ALB). The score is how many are ok out of those that are
ok, weak or missing; the letter follows from the share.
"""

from dataclasses import dataclass, field

#: Managed rule groups that are graded: (key, label, AWS name, what it stops).
MANAGED = (
    (
        "core",
        "Core rule set",
        "AWSManagedRulesCommonRuleSet",
        "The OWASP Top 10 basics: XSS, path traversal, oversized requests.",
    ),
    (
        "bad_inputs",
        "Known bad inputs",
        "AWSManagedRulesKnownBadInputsRuleSet",
        "Exploit payloads such as Log4Shell and host-header attacks.",
    ),
    (
        "ip_reputation",
        "IP reputation list",
        "AWSManagedRulesAmazonIpReputationList",
        "Addresses Amazon threat intelligence knows for bots and attacks.",
    ),
    (
        "sqli",
        "SQL injection",
        "AWSManagedRulesSQLiRuleSet",
        "SQL injection in the query string, body and cookies.",
    ),
)

#: Other AWS managed groups worth naming when they are there; not graded.
NOTED = {
    "AWSManagedRulesBotControlRuleSet": "Bot Control",
    "AWSManagedRulesAnonymousIpList": "Anonymous IP list",
    "AWSManagedRulesATPRuleSet": "Account takeover prevention",
    "AWSManagedRulesACFPRuleSet": "Account creation fraud prevention",
    "AWSManagedRulesLinuxRuleSet": "Linux",
    "AWSManagedRulesUnixRuleSet": "POSIX",
    "AWSManagedRulesWindowsRuleSet": "Windows",
    "AWSManagedRulesPHPRuleSet": "PHP",
    "AWSManagedRulesWordPressRuleSet": "WordPress",
    "AWSManagedRulesAdminProtectionRuleSet": "Admin protection",
    "AWSManagedRulesAntiDDoSRuleSet": "Anti-DDoS",
}

#: ELB security policies allowing TLS 1.2 and newer only, by name.
MODERN_TLS = ("TLS13-1-2", "TLS13-1-3", "-TLS-1-2-", "FS-1-2")

_GRADES = ((0.9, "A"), (0.75, "B"), (0.6, "C"), (0.4, "D"))

#: The IAM action whose refusal leaves an item `unknown`.
WAF_READ = "wafv2:GetWebACLForResource"
LOGGING_READ = "wafv2:GetLoggingConfiguration"
ATTRIBUTES_READ = "elasticloadbalancing:DescribeLoadBalancerAttributes"


@dataclass(frozen=True, slots=True)
class EdgeItem:
    key: str
    #: `waf` or `alb`: which list the page shows it in.
    group: str
    label: str
    #: `ok`, `weak`, `missing`, `unknown` or `info`.
    status: str
    value: str | None = None
    #: Why it is weak or missing, or what it does.
    note: str | None = None


@dataclass(frozen=True, slots=True)
class EdgeExtra:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class EdgeReport:
    items: list[EdgeItem]
    extras: list[EdgeExtra] = field(default_factory=list)
    acl_name: str | None = None
    acl_arn: str | None = None

    @property
    def score(self) -> int:
        return sum(item.status == "ok" for item in self.items)

    @property
    def total(self) -> int:
        return sum(item.status in ("ok", "weak", "missing") for item in self.items)

    @property
    def grade(self) -> str:
        if not self.total:
            return "?"
        share = self.score / self.total
        return next((letter for floor, letter in _GRADES if share >= floor), "F")


def _group_text(rule: dict) -> str:
    return f"{rule.get('vendor')}/{rule.get('group')}" if rule.get("vendor") else str(rule.get("group"))


def _managed_items(rules: list[dict]) -> list[EdgeItem]:
    by_name = {
        rule.get("group"): rule
        for rule in rules
        if rule.get("kind") == "managed" and rule.get("vendor") == "AWS"
    }
    vendors = [rule for rule in rules if rule.get("kind") == "managed" and rule.get("vendor") != "AWS"]
    items = []
    for key, label, name, what in MANAGED:
        rule = by_name.get(name)
        if rule is not None:
            note = what
            if rule.get("count_override") == "all":
                note = f"{what} Runs in Count mode: matches are logged, not blocked."
            elif rule.get("count_override") == "partial":
                counted = ", ".join(r for r in rule.get("counted_rules", []) if r)
                note = f"{what} Set to Count, not block: {counted}."
            items.append(EdgeItem(key, "waf", label, "ok", name, note))
        elif key == "core" and vendors:
            items.append(
                EdgeItem(
                    key, "waf", label, "ok", _group_text(vendors[0]),
                    f"No {name}; {_group_text(vendors[0])} likely covers the same ground.",
                )
            )
        else:
            items.append(EdgeItem(key, "waf", label, "missing", None, f"Add {name}. {what}"))
    return items


def _rate_item(rules: list[dict]) -> EdgeItem:
    rates = [rule for rule in rules if rule.get("kind") == "rate"]
    label = "Rate limiting"
    if not rates:
        return EdgeItem(
            "rate", "waf", label, "missing", None,
            "Add a rate-based rule, so one address cannot flood the app or brute-force logins.",
        )
    enforcing = [rule for rule in rates if rule.get("action") not in ("count", None)]
    shown = ", ".join(f"{rule.get('name')} ({rule.get('limit')})" for rule in rates)
    if not enforcing:
        return EdgeItem("rate", "waf", label, "weak", shown, "The rate-based rules only count, they block nobody.")
    return EdgeItem("rate", "waf", label, "ok", shown, "Requests per address per evaluation window.")


def _enforcing_item(rules: list[dict]) -> EdgeItem:
    counting = [rule for rule in rules if rule.get("kind") in ("managed", "group") and rule.get("count_override") == "all"]
    label = "Blocking, not counting"
    if counting:
        names = ", ".join(_group_text(rule) for rule in counting)
        return EdgeItem(
            "enforcing", "waf", label, "weak", names,
            "These rule groups run in Count mode: matches are logged, not blocked.",
        )
    return EdgeItem("enforcing", "waf", label, "ok", None, "Every rule group blocks what it matches.")


def _logging_item(acl: dict) -> EdgeItem:
    label = "WAF logging"
    logging_on = acl.get("logging")
    if logging_on is None:
        return EdgeItem("logging", "waf", label, "unknown", None, f"Grant {LOGGING_READ} to read it.")
    if logging_on:
        return EdgeItem("logging", "waf", label, "ok", "on", "Blocked and allowed requests are recorded.")
    return EdgeItem(
        "logging", "waf", label, "missing", "off",
        "Turn on logging (CloudWatch Logs, S3 or Firehose) to see what was blocked and why.",
    )


def _fail_open_item(attributes: dict | None, has_acl: bool) -> EdgeItem:
    label = "Fails closed"
    if not has_acl:
        return EdgeItem("fail_open", "waf", label, "info", None, "Applies once a Web ACL is attached.")
    if attributes is None:
        return EdgeItem("fail_open", "waf", label, "unknown", None, f"Grant {ATTRIBUTES_READ} to read it.")
    if attributes.get("waf_fail_open"):
        return EdgeItem(
            "fail_open", "waf", label, "weak", "fail open",
            "When the ALB cannot reach WAF it lets requests through unchecked.",
        )
    return EdgeItem("fail_open", "waf", label, "ok", "fail closed", "No request skips WAF.")


def _waf_items(waf: dict, public: bool) -> list[EdgeItem]:
    keys = [(key, label) for key, label, _, _ in MANAGED] + [
        ("rate", "Rate limiting"),
        ("enforcing", "Blocking, not counting"),
        ("logging", "WAF logging"),
    ]
    if "error" in waf:
        # One row, not one per rule: nothing about the ACL is known.
        note = f"Grant {WAF_READ} to read it and its rules."
        return [EdgeItem("acl", "waf", "Web ACL attached", "unknown", None, note)]
    if public:
        return [
            EdgeItem(
                "acl", "waf", "Web ACL attached", "missing", None,
                "This internet-facing ALB has no AWS WAF Web ACL: every request reaches the app.",
            )
        ] + [EdgeItem(key, "waf", label, "missing", None, "No Web ACL.") for key, label in keys]
    return [
        EdgeItem(
            "acl", "waf", "Web ACL attached", "info", None,
            "Internal load balancer: a WAF is optional.",
        )
    ]


def _https_items(listeners: list[dict], public: bool) -> list[EdgeItem]:
    http = [row for row in listeners if row.get("protocol") == "HTTP"]
    https = [row for row in listeners if row.get("protocol") == "HTTPS"]
    items = []
    plain = [row for row in http if not row.get("redirects_to_https")]
    if not http:
        items.append(EdgeItem("redirect", "alb", "HTTP goes to HTTPS", "ok", None, "No plain-HTTP listener."))
    elif plain:
        ports = ", ".join(f"HTTP:{row.get('port')}" for row in plain)
        status = "missing" if public else "weak"
        note = "Serves the app over plain HTTP; make its default action a redirect to HTTPS."
        items.append(EdgeItem("redirect", "alb", "HTTP goes to HTTPS", status, ports, note))
    else:
        items.append(EdgeItem("redirect", "alb", "HTTP goes to HTTPS", "ok", None, "Plain HTTP is redirected."))

    if not https:
        status = "missing" if public else "info"
        items.append(EdgeItem("tls", "alb", "TLS policy", status, None, "No HTTPS listener."))
        return items
    old = [row for row in https if not any(part in (row.get("ssl_policy") or "") for part in MODERN_TLS)]
    if old:
        policies = ", ".join(sorted({row.get("ssl_policy") or "?" for row in old}))
        items.append(
            EdgeItem(
                "tls", "alb", "TLS policy", "weak", policies,
                "Allows TLS 1.0/1.1. Use ELBSecurityPolicy-TLS13-1-2-2021-06 or newer.",
            )
        )
    else:
        policies = ", ".join(sorted({row.get("ssl_policy") for row in https}))
        items.append(EdgeItem("tls", "alb", "TLS policy", "ok", policies, "TLS 1.2 and newer only."))
    return items


def _attribute_items(attributes: dict | None) -> list[EdgeItem]:
    if attributes is None:
        note = f"Grant {ATTRIBUTES_READ} to read it."
        return [
            EdgeItem("drop_headers", "alb", "Drops invalid headers", "unknown", None, note),
            EdgeItem("desync", "alb", "Desync mitigation", "unknown", None, note),
        ]
    items = []
    if attributes.get("drop_invalid_headers"):
        items.append(EdgeItem("drop_headers", "alb", "Drops invalid headers", "ok", "on", None))
    else:
        items.append(
            EdgeItem(
                "drop_headers", "alb", "Drops invalid headers", "missing", "off",
                "Turn on routing.http.drop_invalid_header_fields so malformed headers never reach the app.",
            )
        )
    mode = attributes.get("desync_mode")
    if mode in ("defensive", "strictest"):
        items.append(EdgeItem("desync", "alb", "Desync mitigation", "ok", mode, "Blocks HTTP request smuggling."))
    else:
        items.append(
            EdgeItem(
                "desync", "alb", "Desync mitigation", "weak", mode,
                "Monitor mode lets ambiguous requests through: set it to defensive or strictest.",
            )
        )
    return items


def _extras(acl: dict | None, attributes: dict | None) -> list[EdgeExtra]:
    extras = []
    if acl is not None:
        if acl.get("default_action"):
            extras.append(EdgeExtra("Default action", acl["default_action"].capitalize()))
        if acl.get("capacity") is not None:
            extras.append(EdgeExtra("Capacity", f"{acl['capacity']} WCU"))
        if acl.get("firewall_manager"):
            extras.append(EdgeExtra("Managed by", "AWS Firewall Manager"))
        graded = {name for _, _, name, _ in MANAGED}
        for rule in acl.get("rules", []):
            kind, group = rule.get("kind"), rule.get("group")
            if kind == "managed" and group in graded:
                continue
            if kind == "managed":
                label = NOTED.get(group, _group_text(rule)) if rule.get("vendor") == "AWS" else _group_text(rule)
                extras.append(EdgeExtra("Managed rule group", label))
            elif kind == "group":
                extras.append(EdgeExtra("Own rule group", str(group)))
            elif kind == "geo":
                extras.append(EdgeExtra("Geo rule", f"{rule.get('name')} ({rule.get('action')})"))
            elif kind == "ip_set":
                extras.append(EdgeExtra("IP set rule", f"{rule.get('name')} ({rule.get('action')})"))
    if attributes is not None:
        for key, label in (("deletion_protection", "Deletion protection"), ("access_logs", "Access logs")):
            if key in attributes:
                extras.append(EdgeExtra(label, "on" if attributes[key] else "off"))
    return extras


def grade(aws_detail: dict | None) -> EdgeReport | None:
    """Grade an ALB's stored detail; None for anything else, and for an ALB
    synced before discovery read its WAF."""
    if not aws_detail or aws_detail.get("type") != "application" or "waf" not in aws_detail:
        return None
    public = aws_detail.get("scheme") == "internet-facing"
    waf = aws_detail.get("waf") or {}
    acl = waf.get("acl")
    attributes = aws_detail.get("attributes")

    if acl is None:
        items = _waf_items(waf, public)
    else:
        rules = acl.get("rules") or []
        items = [
            EdgeItem("acl", "waf", "Web ACL attached", "ok", acl.get("name"), None),
            *_managed_items(rules),
            _rate_item(rules),
            _enforcing_item(rules),
            _logging_item(acl),
        ]
    items.append(_fail_open_item(attributes, acl is not None))
    items += _https_items(aws_detail.get("listeners") or [], public)
    items += _attribute_items(attributes)
    return EdgeReport(
        items=items,
        extras=_extras(acl, attributes),
        acl_name=acl.get("name") if acl else None,
        acl_arn=acl.get("arn") if acl else None,
    )
