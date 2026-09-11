"""robots.txt parsing, following RFC 9309.

`urllib.robotparser` exists but does not expose Crawl-delay, does not surface
Sitemap lines, and is vague about longest-match precedence — all three of which
this crawler needs, so the ~120 lines are worth owning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

WILDCARD = "*"


@dataclass(slots=True)
class Rule:
    allow: bool
    path: str
    pattern: re.Pattern[str]

    @property
    def specificity(self) -> int:
        """Longest rule wins, per the spec. Length of the literal path is the measure."""
        return len(self.path)


@dataclass(slots=True)
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _compile_path(path: str) -> re.Pattern[str]:
    """robots.txt path -> regex. `*` is any sequence, a trailing `$` anchors the end."""
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    escaped = "".join(".*" if char == WILDCARD else re.escape(char) for char in path)
    return re.compile(f"^{escaped}{'$' if anchored else ''}")


def product_token(user_agent: str) -> str:
    """Pull the crawler's own name out of a full UA string, for group matching."""
    match = re.search(r"compatible;\s*([A-Za-z0-9_.\-]+)", user_agent)
    if match:
        return match.group(1).split("/")[0].lower()
    return (user_agent.split("/")[0] or user_agent).strip().lower()


@dataclass(slots=True)
class RobotsTxt:
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    status: int | None = None
    fetched: bool = False
    error: str = ""
    source_url: str = ""

    # --- group selection ---------------------------------------------------

    def _group_for(self, agent: str) -> Group | None:
        """Most specific matching group: longest matching token beats `*`."""
        agent = agent.lower()
        best: tuple[int, Group] | None = None
        fallback: Group | None = None
        for group in self.groups:
            for declared in group.agents:
                if declared == WILDCARD:
                    fallback = fallback or group
                elif agent.startswith(declared) or declared in agent:
                    if best is None or len(declared) > best[0]:
                        best = (len(declared), group)
        return best[1] if best else fallback

    # --- the questions the crawler actually asks ---------------------------

    def is_allowed(self, url: str, agent: str) -> bool:
        group = self._group_for(agent)
        if group is None or not group.rules:
            return True

        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query

        matches = [rule for rule in group.rules if rule.pattern.match(target)]
        if not matches:
            return True
        # Longest match wins; Allow wins a tie. Both are the documented rules.
        best = max(matches, key=lambda rule: (rule.specificity, rule.allow))
        return best.allow

    def crawl_delay(self, agent: str) -> float | None:
        group = self._group_for(agent)
        return group.crawl_delay if group else None


def parse(text: str, *, source_url: str = "") -> RobotsTxt:
    robots = RobotsTxt(source_url=source_url)
    current: Group | None = None
    # A blank line or a rule ends a run of user-agent lines; the next user-agent
    # after a rule starts a new group rather than joining the previous one.
    accepting_agents = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        field_name, separator, value = line.partition(":")
        if not separator:
            continue
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "sitemap":
            if value:
                robots.sitemaps.append(urljoin(source_url, value))
            continue

        if field_name == "user-agent":
            if current is None or not accepting_agents:
                current = Group()
                robots.groups.append(current)
                accepting_agents = True
            current.agents.append(value.lower())
            continue

        if current is None:
            continue  # a rule before any user-agent line has no group to join
        accepting_agents = False

        if field_name in ("allow", "disallow"):
            if field_name == "disallow" and not value:
                continue  # "Disallow:" with no path means nothing is disallowed
            if not value:
                continue
            current.rules.append(
                Rule(allow=field_name == "allow", path=value, pattern=_compile_path(value))
            )
        elif field_name == "crawl-delay":
            try:
                current.crawl_delay = max(0.0, float(value))
            except ValueError:
                pass

    return robots
