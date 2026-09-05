# Configuration

Configuration-related source and Agent-editable declarative configuration live
under this package. Runtime secrets remain in the repository-root `.env`, which
the maintenance Agent cannot read or write.

- `__init__.py`: legacy music configuration compatibility.
- `agent/`: Agent-writable non-sensitive declarative configuration.
- `credentials/`: protected service credentials; the maintenance Agent can
  replace them only through a write-only host tool and cannot read them back.

Service cookies are centralized under `credentials/`. `.env` stores their
configured paths. Credential files are Git-ignored and blocked from general
Agent project tools even though the dedicated owner-only updater may replace
them.
