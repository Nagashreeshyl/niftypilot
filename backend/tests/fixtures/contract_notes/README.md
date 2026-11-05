# Real contract notes for fee reconciliation

Put one JSON file per REAL Groww contract note here (format: `niftypilot/risk/reconcile.py`).
Transcribe only values printed on the note. Remove name, client code, PAN and address.
JSON files in this folder are git-ignored by default because they are personal financial
records. `tests/test_contract_notes.py` reconciles every file and skips loudly when none exist.
