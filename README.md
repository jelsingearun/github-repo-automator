# GitHub Repo Automator

A Python CLI tool that automates creating and pushing GitHub repositories from **ZIP archives** or **local project folders** — in bulk, in seconds.

---

## Features

- 📦 Accepts both **ZIP files** and **unzipped folders** as input
- 🗂️ Handles **multiple projects** inside a single ZIP (one repo per subfolder)
- 🔍 Validates GitHub authentication, Git identity, and tool availability before starting
- 🚫 Filters out junk folders (`__MACOSX`, `.DS_Store`, hidden dirs) automatically
- ⚠️ Warns about files exceeding GitHub's 100 MB limit (Git LFS hint)
- 🔒 Choose **public** or **private** visibility at startup
- 🧹 Auto-cleans up temporary extraction directories
- 📋 Detailed logging to `repo_automation.log`
- 🛡️ **Secret scanning** — automatically detects and removes hardcoded secrets before every commit

---

## Requirements

| Requirement | Install |
|---|---|
| Python 3.8+ | [python.org](https://python.org) |
| Git | [git-scm.com](https://git-scm.com) |
| GitHub CLI (`gh`) | [cli.github.com](https://cli.github.com) |

### Authenticate GitHub CLI

```bash
gh auth login
```

### Set Git identity (if not already done)

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

---

## Usage

```bash
python github_repo_automation.py
```

You will be prompted to:

1. Choose **Public** or **Private** for all repositories in the session
2. Enter a **ZIP file path** or a **folder path** (repeat as needed, type `exit` to quit)

### Examples

```
Enter ZIP file or folder path (or type 'exit'): F:\projects\MyApp.zip
Enter ZIP file or folder path (or type 'exit'): F:\projects\OpenSourceStuff
Enter ZIP file or folder path (or type 'exit'): "F:\My Projects\Academix-main.zip"
```

> Paths with or without surrounding quotes are both handled correctly.

---

## How It Works

```
Input Path
   │
   ├── Folder  ──► Scan for subfolders ──► Git init + commit + gh repo create + push
   │
   └── ZIP ───► Extract to temp dir ──► Scan for subfolders ──► Git init + commit + gh repo create + push ──► Cleanup
```

- If the ZIP/folder contains **multiple subfolders**, each becomes its own GitHub repository.
- If the ZIP/folder is **flat** (no subfolders), the root itself is treated as one project.

---

## 🛡️ Secret Scanning

Before every `git add`, the tool automatically scans all project source files for **hardcoded secrets** and replaces their values with a safe comment placeholder — so sensitive credentials are never accidentally pushed to GitHub.

### What it detects

Common secret variable names across any format:

| Category | Examples |
|---|---|
| API Keys | `api_key`, `api_secret`, `google_api_key`, `openai_api_key`, `stripe_key` |
| Auth Tokens | `auth_token`, `access_token`, `oauth_token`, `bearer_token`, `jwt_secret` |
| Passwords | `password`, `passwd`, `pwd`, `db_password`, `database_password` |
| Cloud Credentials | `aws_access_key_id`, `aws_secret_access_key`, `firebase_key` |
| App Secrets | `secret_key`, `client_secret`, `client_id`, `session_secret`, `signing_key` |

### How it works

The value of a matched secret is replaced with a language-appropriate comment:

```python
# Before
api_key = "sk-abc123xyz789supersecret"

# After
api_key = # SECRET REMOVED - replace with your actual value
```

```javascript
// Before
const clientSecret = "abc123xyz789"

// After
const clientSecret = // SECRET REMOVED - replace with your actual value
```

```xml
<!-- Before -->
<apiKey>abc123xyz789</apiKey>

<!-- After -->
<apiKey><!-- SECRET REMOVED - replace with your actual value --></apiKey>
```

### Supported file types

`.py` `.js` `.ts` `.jsx` `.tsx` `.env` `.json` `.yaml` `.yml` `.xml` `.sh` `.java` `.go` `.cs` `.php` `.rb` `.toml` `.cfg` `.ini` `.conf` `.properties` and more.

> **Note:** Modified files are reported in the terminal and logged to `repo_automation.log`. Always review the sanitized files before sharing them publicly.

---

## Logging

All activity is logged to `repo_automation.log` in the same directory as the script.

---

## License

MIT
