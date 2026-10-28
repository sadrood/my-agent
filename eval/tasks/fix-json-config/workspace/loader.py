import json


def load_config():
    with open("config.json", encoding="utf-8") as fh:
        return json.load(fh)
