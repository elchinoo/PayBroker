"""Train the Tier 2 fraud model and export it as JSON trees (the logic lives in src/paybroker/trainer.py).

    uv pip install -e ".[train]" && python scripts/train_model.py                       # 1,000,000 synthetic rows, bundled model
    python scripts/train_model.py --from-export training.csv --mix-synthetic 0.3 --out my_model.json
    python scripts/train_model.py --dataset tabformer --path card_transaction.v1.csv --out tabformer_model.json

Run with --help for every option. The bundled model is trained on SYNTHETIC data; it shows the mechanics and says nothing about
real fraud. `training_report.json` (written next to the model) holds the evaluation and the provenance of every source.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from paybroker.trainer import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
