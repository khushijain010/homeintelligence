.PHONY: dev emulator app demo scenario test keys set-password tunnel

dev:        ## emulator on :8787 + Guardian on :8000
	uv run python scripts/dev.py

emulator:
	uv run ring-sandbox serve --port 8787

app:
	uv run uvicorn guardian.main:app --host 127.0.0.1 --port 8000

demo:       ## seed Sharma Residence into Guardian + the emulator
	uv run python scripts/demo.py

scenario:   ## make scenario name=leak_empty_home
	uv run python scripts/demo.py $(name)

test:
	uv run pytest -q

keys:       ## RS256 key that signs Alexa access tokens
	uv run python scripts/keys.py

set-password:  ## bcrypt hash for OWNER_PASSWORD_HASH
	uv run python scripts/set_password.py

tunnel:     ## public HTTPS URL for Alexa+ (copy it into PUBLIC_BASE_URL)
	cloudflared tunnel --url http://127.0.0.1:8000
