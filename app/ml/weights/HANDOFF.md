# Plant Presence Classifier — Handoff

Binary image classifier: does a photo contain a plant, or not? Used as a moderation
gate before a submitted photo reaches admin review.

Model: EfficientNet-B0 (transfer learning), single logit output. `sigmoid(logit)` is
the probability the image is a plant. `class_to_idx = {not_plant: 0, plant: 1}`.

## Files included

- `checkpoints/model.torchscript.pt` — the trained model, exported to TorchScript
  (self-contained, doesn't need the training code to load).
- `checkpoints/export_meta.yaml` — `img_size` (224), `class_to_idx`, and
  `chosen_threshold` (the probability cutoff above which a photo counts as "plant").
- `serve.py` / `requirements-serve.txt` / `Dockerfile` — a ready-to-run REST API
  wrapping the model, if you'd rather call it over HTTP than embed it.

Need an ONNX version instead (e.g. for a non-Python backend)? It's already exported
and sitting in Google Drive (`MyDrive/plant-presence-classifier/checkpoints/model.onnx`)
from the training run — just ask and it'll get pulled down, no retraining needed.

## Option A — run it as its own service (recommended if you don't want to touch Python)

```bash
docker build -t plant-classifier .
docker run -d -p 8000:8080 plant-classifier
```

Then:

```bash
curl -X POST -F "file=@photo.jpg" http://localhost:8000/predict
# {"is_plant": true, "confidence": 0.97}
```

That's the whole API — one endpoint, multipart image in, JSON out. Works regardless
of what language your backend is written in; you just make an HTTP call to it. No
auth is built in yet — if this needs to be reachable from outside your own network,
say so before deploying and we'll lock it down first (shared-secret header or
network-level restriction).

## Option B — load the model directly inside your own Python backend

```python
import torch
import yaml
from PIL import Image
from torchvision import transforms

IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

with open("checkpoints/export_meta.yaml") as f:
    meta = yaml.safe_load(f)

model = torch.jit.load("checkpoints/model.torchscript.pt", map_location="cpu")
model.eval()

transform = transforms.Compose([
    transforms.Resize(int(meta["img_size"] * 1.15)),
    transforms.CenterCrop(meta["img_size"]),
    transforms.ToTensor(),
    transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
])

def predict(image_path: str) -> dict:
    img = Image.open(image_path).convert("RGB")
    tensor = transform(img).unsqueeze(0)
    with torch.no_grad():
        confidence = torch.sigmoid(model(tensor)).item()
    return {"is_plant": confidence >= meta["chosen_threshold"], "confidence": round(confidence, 4)}

print(predict("photo.jpg"))
# {'is_plant': True, 'confidence': 0.97}
```

Only needs `torch`, `torchvision`, `pillow`, `pyyaml` installed — no FastAPI/uvicorn
required for this path.

## Known limitation, worth knowing before wiring this into production

The model was trained on a fairly narrow negative set (food, dogs, faces as "not
plant" examples). It's reliable on clear plants and on those three categories, but
gives borderline/uncertain scores (0.5–0.7ish) on other random non-plant photos
(objects, screenshots, scenery) rather than confidently rejecting them. The
`chosen_threshold` in `export_meta.yaml` was picked with that in mind, but it's not
a fully solved problem — a broader retrain is a possible follow-up if this proves
too permissive in practice.
