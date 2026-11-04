"""Run the normal ranking pipeline against fictional job-board responses."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, scan
from app.sources.base import Posting


def demo_board(_client, company, slug, *, retries):
    postings = (
        ("platform", "Backend Platform Engineer", "Build Go services, PostgreSQL APIs, and Kafka consumers."),
        ("infra", "Infrastructure Engineer", "Own Kubernetes, Terraform, observability, and CI/CD."),
        ("frontend", "Frontend Engineer", "Build browser interfaces with React and CSS."),
    )
    return [
        Posting("greenhouse", company, slug, key, title, "Remote, United States", description,
                f"https://jobs.example.com/demo/{key}", is_remote=True)
        for key, title, description in postings
    ]


if __name__ == "__main__":
    cfg = config.load_config("config.example.yaml")
    cfg["companies"] = [{"name": "Demo Systems", "ats": "greenhouse", "slug": "demo-systems"}]
    cfg["filters"] = {"role_include": ["backend", "infrastructure"]}
    with patch.dict(scan.FETCHERS, greenhouse=demo_board):
        scan.run_scan(cfg, mode="demo")
