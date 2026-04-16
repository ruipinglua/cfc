import json
from pathlib import Path

CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"


def load_credentials() -> dict:
    if not CREDENTIALS_FILE.exists():
        example = CREDENTIALS_FILE.parent / "credentials.json.example"
        raise FileNotFoundError(
            f"Credentials file not found: {CREDENTIALS_FILE}\n"
            f"Copy {example} to {CREDENTIALS_FILE} and fill in your Alpaca API keys."
        )
    creds = json.loads(CREDENTIALS_FILE.read_text())
    missing = {"api_key", "api_secret"} - creds.keys()
    if missing:
        raise ValueError(f"Missing required fields in credentials.json: {missing}")
    return creds
