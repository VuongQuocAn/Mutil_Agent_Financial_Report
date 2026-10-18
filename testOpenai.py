import requests

data = requests.get(
    "https://openrouter.ai/api/v1/models"
).json()["data"]

free_models = [
    (m["id"], m["context_length"])
    for m in data
    if m["id"].endswith(":free")
]

for model_id, context in free_models:
    print(model_id, context)