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

## Logging

All activity is logged to `repo_automation.log` in the same directory as the script.

---

## License

MIT
