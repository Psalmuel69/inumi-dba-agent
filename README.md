# Inumi — Enterprise AI DBA Agent & Secure DBA Control Gateway

Status: under active build-out. See ARCHITECTURE.md and SECURITY.md (added
progressively as each phase lands).

## Local development

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
cp .env.example .env
pytest
```
