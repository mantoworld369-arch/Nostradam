import yaml
import os
from dotenv import load_dotenv

load_dotenv()

def load_config(path="config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["api"] = {
        "key": os.getenv("POLY_API_KEY", ""),
        "secret": os.getenv("POLY_API_SECRET", ""),
        "passphrase": os.getenv("POLY_PASSPHRASE", ""),
        "private_key": os.getenv("PRIVATE_KEY", ""),
    }
    return cfg
