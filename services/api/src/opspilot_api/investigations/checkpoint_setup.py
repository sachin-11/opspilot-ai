"""Create/upgrade LangGraph-owned checkpoint tables explicitly."""

from opspilot_api.config import get_settings
from opspilot_api.investigations.workflow import setup_checkpoints

if __name__ == "__main__":
    setup_checkpoints(get_settings())
