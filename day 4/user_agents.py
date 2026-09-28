import itertools
import random
from collections.abc import Sequence


class UserAgentRotator:
    """Hands out User-Agent strings: round-robin over `agents` or random choice.

    A single agent means no rotation. robots.txt rules are checked for every agent
    of the rotation (see RobotsParser), so rotating never widens what we may fetch.
    """

    def __init__(self, agents: str | Sequence[str], *, randomize: bool = False, rng: random.Random | None = None):
        self.agents = [agents] if isinstance(agents, str) else list(agents)
        if not self.agents or not all(a.strip() for a in self.agents):
            raise ValueError("at least one non-empty User-Agent is required")
        self.randomize = randomize
        self.rng = rng or random.Random()
        self._cycle = itertools.cycle(self.agents)

    @property
    def primary(self) -> str:
        return self.agents[0]

    def next(self) -> str:
        if len(self.agents) == 1:
            return self.agents[0]
        return self.rng.choice(self.agents) if self.randomize else next(self._cycle)

    __next__ = next

    def __iter__(self):
        return self
