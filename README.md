# NFO / BFO Spread Terminal — Bloomberg Dark

Options spread dashboard for NSE/BSE using Fyers API v3.

---

## Project structure

```
your-project/
├── dashboard_v3_bloomberg.py   ← main app
├── requirements.txt
├── .gitignore                  ← keeps secrets out of git
├── .streamlit/
│   ├── secrets.toml            ← YOUR SECRETS (never commit)
│   └── config.toml             ← optional theme config
└── access_token.txt            ← auto-generated, gitignored
```

---

## Quick start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure secrets
Copy the template and fill in your values:
```bash
mkdir -p .streamlit
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # if using example file
# OR edit .streamlit/secrets.toml directly
```

Minimum required keys in `secrets.toml`:
```toml
DASH_USER        = "admin"
DASH_PASSWORD    = "your_strong_password"
FYERS_CLIENT_ID  = "XXXX-100"
FYERS_SECRET_KEY = "..."
FYERS_USERNAME   = "..."
FYERS_PIN        = "1234"
FYERS_TOTP_KEY   = "BASE32SECRET"
```

### 3. Run
```bash
streamlit run dashboard_v3_bloomberg.py
```

---

## Deploying to Streamlit Cloud

1. Push your repo to GitHub (**confirm `.streamlit/secrets.toml` is NOT in the repo**)
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app
3. In **Advanced settings → Secrets**, paste the contents of your `secrets.toml`
4. Deploy

---

## Keeping API keys safe on GitHub

| What to do | Why |
|---|---|
| Add `.streamlit/secrets.toml` to `.gitignore` | Prevents accidental commits |
| Never hardcode keys in `.py` files | Keys in code = keys in git history forever |
| Use `get_secret("KEY")` everywhere | Reads from `secrets.toml` or env vars safely |
| Rotate keys immediately if exposed | Git history is permanent even after deletion |

### If you accidentally committed secrets:
```bash
# 1. Rotate your Fyers API key immediately in the Fyers dashboard
# 2. Remove the file from git tracking
git rm --cached .streamlit/secrets.toml
echo ".streamlit/secrets.toml" >> .gitignore
git add .gitignore
git commit -m "remove secrets from tracking"
git push
# 3. To scrub from git history entirely (optional, more involved):
#    https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
```

---

## Authentication

The dashboard requires a username + password login before anything is shown.
Credentials are set in `secrets.toml` under `DASH_USER` and `DASH_PASSWORD`.

To add multiple users, edit `_auth_gate()` in the main file and extend the `VALID_USERS` dict.

---

## Suppressing terminal output

The Fyers SDK prints to stdout by default. This is handled by:
- `logging.disable(logging.CRITICAL)` — kills all stdlib loggers
- `_silent()` context manager — wraps all SDK calls, redirecting stdout/stderr to `/dev/null`

You should see a clean terminal when running.
