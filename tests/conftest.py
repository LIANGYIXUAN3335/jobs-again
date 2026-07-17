import pytest

from app import config as cfgmod


@pytest.fixture
def cfg(tmp_path):
    """Real config (keyword maps, filters) with throwaway DB + output paths."""
    c = dict(cfgmod.load_config())
    c["db_path"] = str(tmp_path / "jobscan.db")
    c["user_db_path"] = str(tmp_path / "user_state.db")
    c["output_dirs"] = {"prompts": str(tmp_path / "prompts"),
                        "resumes": str(tmp_path / "resumes")}
    return c
