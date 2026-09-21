from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    max_concurrency: int = 10
    connector_limit: int = 100
    connector_limit_per_host: int = 10
    total_timeout: float = 10.0
    user_agent: str = "AsyncCrawler/0.1"
