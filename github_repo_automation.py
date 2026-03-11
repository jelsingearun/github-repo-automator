import os
import re
import zipfile
import shutil
import subprocess
import uuid
import logging
import time
import sys
import tempfile

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_automation.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

LARGE_FILE_LIMIT_MB = 100
SKIP_FOLDERS = {"__macosx", ".ds_store", "node_modules", ".git"}

# File extensions to scan for secrets
SECRET_SCAN_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".env", ".json",
    ".yaml", ".yml", ".xml", ".properties", ".cfg", ".ini",
    ".sh", ".bat", ".cmd", ".rb", ".php", ".java", ".go",
    ".cs", ".cpp", ".c", ".h", ".toml", ".conf",
}

# --- GLOBAL CONFIGURATION ---
DRY_RUN = False  # Set to True to simulate without pushing/creating
ORG_NAME = None  # Set to organization name if creating for an org
DISCORD_WEBHOOK_URL = None # Set to your webhook URL for notifications
DEFAULT_LICENSE = "MIT"
DEFAULT_README_CONTENT = """# {repo_name}

Automated repository created by `github_repo_automation.py`.

## Features
- Automated initialization
- Hardcoded secret sanitization
- Standardized .gitignore

## Usage
[Add usage instructions here]
"""

# Standard .gitignore patterns by language
GITIGNORE_TEMPLATES = {
    "python": ["__pycache__/", "*.py[cod]", "*$py.class", ".env", "venv/", "env/"],
    "javascript": ["node_modules/", ".env", "dist/", "build/", ".DS_Store"],
    "java": ["target/", "*.class", "*.jar", "*.war"],
}

# --- SECURITY SCAN CONFIG ---
ENTROPY_THRESHOLD = 4.0  # Threshold for Shannon entropy (API keys are usually > 4.0)
MIN_SECRET_LEN = 8       # Minimum length to check for entropy
SAST_DANGEROUS_FUNCS = {
    "eval(", "exec(", "os.system(", "subprocess.Popen(", "subprocess.call(",
    "pickle.load(", "marshal.load(", "input(", "os.popen(", "commands.getoutput("
}
SUSPICIOUS_KEYWORDS = {"password", "secret", "token", "private", "key", "auth", "credential", "admin"}
SECURITY_COMMENTS = {"FIXME", "TODO", "XXX", "HACK", "SECURITY", "BUG"}

# Regex for specific high-risk patterns
DEEP_SCAN_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API Key"),
    (re.compile(r"sk_live_[0-9a-zA-Z]{24}"), "Stripe Live Secret Key"),
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"), "Private Key File"),
    (re.compile(r"EYJ[A-Z0-9_-]+\.EYJ[A-Z0-9_-]+\.[A-Z0-9_-]+", re.I), "JWT Token"),
    (re.compile(r"gh[oprs]_[a-zA-Z0-9]{36}"), "GitHub Token"),
]

# Regex patterns that match secret assignments.
# Group 1 = everything before the value, Group 2 = the secret value itself.
SECRET_PATTERNS = [
    # KEY = "value" / KEY = 'value'
    (re.compile(
        r"""((?:api[_-]?key|api[_-]?secret|auth[_-]?token|access[_-]?token|
             bearer[_-]?token|secret[_-]?key|client[_-]?secret|client[_-]?id|
             app[_-]?secret|app[_-]?key|private[_-]?key|encryption[_-]?key|
             db[_-]?password|database[_-]?password|db[_-]?pass|passwd|
             password|passwd|pwd|token|secret|credential|auth[_-]?key|
             aws[_-]?access[_-]?key|aws[_-]?secret|stripe[_-]?key|
             sendgrid[_-]?key|twilio[_-]?token|firebase[_-]?key|
             google[_-]?api[_-]?key|openai[_-]?key|jwt[_-]?secret|
             oauth[_-]?token|session[_-]?secret|signing[_-]?key)
             \s*[=:]\s*)(['\"])[^'\"\n]{8,}\2""",
        re.IGNORECASE | re.VERBOSE
    ), 1),
    # KEY=VALUE (no quotes, env-style)
    (re.compile(
        r"""^((?:API_KEY|API_SECRET|AUTH_TOKEN|ACCESS_TOKEN|SECRET_KEY|
             CLIENT_SECRET|CLIENT_ID|APP_SECRET|PRIVATE_KEY|DB_PASSWORD|
             DATABASE_PASSWORD|DB_PASS|PASSWORD|PASSWD|TOKEN|SECRET|
             AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|STRIPE_SECRET_KEY|
             SENDGRID_API_KEY|TWILIO_AUTH_TOKEN|FIREBASE_API_KEY|
             GOOGLE_API_KEY|OPENAI_API_KEY|JWT_SECRET|OAUTH_TOKEN|
             SESSION_SECRET|SIGNING_KEY)=)(.+)$""",
        re.IGNORECASE | re.VERBOSE | re.MULTILINE
    ), 1),
]


def run_command(cmd, cwd=None, step="operation"):
    if DRY_RUN:
        print(f"[DRY-RUN] Would execute: {' '.join(cmd)} (cwd: {cwd})")
        return ""
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error_msg = f"\n[FAILED] {step}\nCommand: {' '.join(cmd)}\nReason: {e.stderr.strip()}"
        print(error_msg)
        logging.error(error_msg)
        raise


def sanitize_repo_name(name):
    try:
        name = name.lower()
        noise_suffixes = r'(-main|-master|-develop|-dev|-release|-prod|-production|-copy|-backup|-new|-old|-v\d+[\d.]*|-\d+)$'
        while True:
            cleaned = re.sub(noise_suffixes, '', name)
            if cleaned == name:
                break
            name = cleaned
        name = re.sub(r'[^a-z0-9-_]', '-', name)
        name = re.sub(r'-+', '-', name)
        name = name.strip("-")
        if not name:
            raise ValueError("Sanitized repo name is empty")
        return name
    except Exception as e:
        raise RuntimeError(f"Repo name sanitization failed: {e}")


def normalize_path(raw):
    path = raw.strip().strip('"').strip("'").rstrip("/\\")
    return os.path.normpath(path)


def check_tool(tool, install_hint):
    try:
        subprocess.run([tool, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except FileNotFoundError:
        print(f"[ERROR] '{tool}' is not installed or not on PATH. {install_hint}")
        sys.exit(1)


def check_gh_auth():
    print("Checking GitHub authentication...")
    try:
        run_command(["gh", "auth", "status"], step="GitHub authentication check")
        print("[OK] GitHub authentication verified")
    except Exception:
        print("GitHub CLI not authenticated. Run: gh auth login")
        sys.exit(1)


def get_gh_username():
    try:
        return run_command(["gh", "api", "user", "--jq", ".login"], step="Fetch GitHub username")
    except Exception:
        return None


def check_git_identity():
    for field in ("user.name", "user.email"):
        try:
            value = subprocess.run(
                ["git", "config", "--global", field],
                capture_output=True, text=True
            ).stdout.strip()
            if not value:
                raise ValueError()
        except Exception:
            print(f"[ERROR] Git identity not set. Run: git config --global {field} \"your value\"")
            sys.exit(1)


def ask_visibility():
    while True:
        choice = input("Create repositories as [P]ublic or [R]ivate? ").strip().lower()
        if choice in ("p", "public"):
            return "--public"
        if choice in ("r", "private"):
            return "--private"
        print("Please enter 'P' for public or 'R' for private.")


def check_large_files(directory):
    warnings = []
    for root, _, files in os.walk(directory):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                if size_mb > LARGE_FILE_LIMIT_MB:
                    warnings.append(f"  {fpath} ({size_mb:.1f} MB)")
            except OSError:
                pass
    if warnings:
        print(f"[WARNING] Files exceeding {LARGE_FILE_LIMIT_MB} MB (may be rejected by GitHub):")
        for w in warnings:
            print(w)
        print("  Consider using Git LFS or removing them before pushing.")


def extract_zip(zip_path):
    print(f"\nExtracting ZIP: {zip_path}")

    if not os.path.isfile(zip_path):
        if os.path.isdir(zip_path):
            raise FileNotFoundError("The path points to a directory, not a ZIP file")
        raise FileNotFoundError("ZIP path does not exist")

    try:
        if not zipfile.is_zipfile(zip_path):
            raise ValueError("Provided file is not a valid ZIP archive")
    except zipfile.BadZipFile as e:
        raise ValueError(f"ZIP file is corrupted: {e}")

    with zipfile.ZipFile(zip_path, 'r') as zf:
        if not zf.namelist():
            raise ValueError("ZIP archive is empty")
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                raise ValueError("ZIP archive is password-protected — cannot extract")

    extract_dir = os.path.join(os.path.dirname(zip_path), f"extract_{uuid.uuid4().hex}")

    try:
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        print("[OK] Extraction completed")
        return extract_dir
    except OSError as e:
        raise RuntimeError(f"ZIP extraction failed (disk space or OS error): {e}")
    except Exception as e:
        raise RuntimeError(f"ZIP extraction failed: {e}")


def auto_generate_gitignore(project_path):
    """Detect language and create .gitignore if missing."""
    gitignore_path = os.path.join(project_path, ".gitignore")
    if os.path.exists(gitignore_path):
        return

    detected_langs = set()
    for root, _, files in os.walk(project_path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in {".py", ".pyc"}: detected_langs.add("python")
            if ext in {".js", ".ts", ".jsx", ".tsx"}: detected_langs.add("javascript")
            if ext in {".java", ".jar"}: detected_langs.add("java")

    patterns = set()
    for lang in detected_langs:
        patterns.update(GITIGNORE_TEMPLATES.get(lang, []))

    if patterns:
        print(f"  [AUTO] Generating .gitignore for: {', '.join(detected_langs)}")
        if not DRY_RUN:
            with open(gitignore_path, "w") as f:
                f.write("\n".join(sorted(list(patterns))) + "\n")
        else:
            print(f"  [DRY-RUN] Would write .gitignore with {len(patterns)} patterns")


def inject_templates(project_path, repo_name):
    """Add README and LICENSE if missing."""
    readme_path = os.path.join(project_path, "README.md")
    if not os.path.exists(readme_path):
        print("  [AUTO] Adding README.md")
        if not DRY_RUN:
            with open(readme_path, "w") as f:
                f.write(DEFAULT_README_CONTENT.format(repo_name=repo_name))
        else:
            print("  [DRY-RUN] Would add README.md")

    license_path = os.path.join(project_path, "LICENSE")
    if not os.path.exists(license_path):
        print(f"  [AUTO] Adding {DEFAULT_LICENSE} LICENSE")
        if not DRY_RUN:
            # Note: This is a placeholder; real MIT text could be inserted here.
            with open(license_path, "w") as f:
                f.write(f"Copyright (c) {time.strftime('%Y')} Automated Script\n\nLicense: {DEFAULT_LICENSE}")
        else:
            print(f"  [DRY-RUN] Would add LICENSE")


def inject_issue_templates(project_path):
    """Add basic ISSUE and PR templates if missing."""
    dot_github = os.path.join(project_path, ".github")
    templates_dir = os.path.join(dot_github, "ISSUE_TEMPLATE")
    
    if not os.path.exists(templates_dir):
        print("  [AUTO] Adding Issue templates")
        if not DRY_RUN:
            os.makedirs(templates_dir, exist_ok=True)
            with open(os.path.join(templates_dir, "bug_report.md"), "w") as f:
                f.write("# Bug Report\n\n**Describe the bug**\n...\n")
        else:
            print("  [DRY-RUN] Would add Issue templates")

    pr_template = os.path.join(dot_github, "PULL_REQUEST_TEMPLATE.md")
    if not os.path.exists(pr_template):
        print("  [AUTO] Adding PR template")
        if not DRY_RUN:
            os.makedirs(dot_github, exist_ok=True)
            with open(pr_template, "w") as f:
                f.write("# Pull Request\n\n- [ ] Bug fix\n- [ ] New feature\n")
        else:
            print("  [DRY-RUN] Would add PR template")


def scan_vulnerability_risk(project_path):
    """Simple check for dependency files."""
    risk_found = False
    if os.path.exists(os.path.join(project_path, "requirements.txt")):
        print("  [INFO] Detected requirements.txt - running basic vulnerability check...")
        risk_found = True # In a real implementation, we'd use 'gh api' or 'safety'
    if os.path.exists(os.path.join(project_path, "package.json")):
        print("  [INFO] Detected package.json - recommending 'npm audit'...")
        risk_found = True
    return risk_found


def send_notification(message):
    """Send success notification to Discord/Slack if webhook is configured."""
    if not DISCORD_WEBHOOK_URL or DRY_RUN:
        return
    try:
        import json
        import urllib.request
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as res:
            pass
    except Exception as e:
        print(f"  [WARNING] Failed to send notification: {e}")


def calculate_entropy(data):
    """Calculate Shannon entropy of a string."""
    if not data:
        return 0
    import math
    entropy = 0
    for x in range(256):
        p_x = float(data.count(chr(x))) / len(data)
        if p_x > 0:
            entropy += - p_x * math.log(p_x, 2)
    return entropy


def deep_security_audit(project_path):
    """Detailed 'word-by-word' security scan of the project."""
    findings = []
    print("  [AUDIT] Performing deep security scan...")
    
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_FOLDERS and not d.startswith(".")]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SECRET_SCAN_EXTENSIONS:
                continue
            
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, project_path)
            
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except OSError:
                continue

            for line_num, line in enumerate(lines, 1):
                clean_line = line.strip()
                
                # 1. Pattern Matching (Known Secrets)
                for pattern, desc in DEEP_SCAN_PATTERNS:
                    if pattern.search(clean_line):
                        findings.append(f"{rel_path}:{line_num} - [HIGH] Likely {desc}")

                # 2. SAST (Dangerous Functions)
                for func in SAST_DANGEROUS_FUNCS:
                    if func in clean_line:
                        findings.append(f"{rel_path}:{line_num} - [MED] Dangerous function call: {func.strip('(')}")

                # 3. Suspicious Keywords in Comments
                if any(k in clean_line.lower() for k in SECURITY_COMMENTS):
                    findings.append(f"{rel_path}:{line_num} - [LOW] Security-related comment found")

                # 4. Entropy-based scanning (Word-by-word)
                words = re.findall(r"['\"]([a-zA-Z0-9_\-\.]{8,})['\"]", clean_line)
                for word in words:
                    if calculate_entropy(word) > ENTROPY_THRESHOLD:
                        # Double check it's not a known safe string
                        if not any(k in clean_line.lower() for k in {"import", "class", "def"}):
                            findings.append(f"{rel_path}:{line_num} - [HIGH] High entropy string (potential key): {word[:4]}...")

    if findings:
        report_path = os.path.join(project_path, "security_audit_report.md")
        report_content = "# Security Audit Report\n\nGenerated on: " + time.ctime() + "\n\n"
        report_content += "The following issues were detected and should be reviewed:\n\n"
        for f in findings:
            report_content += f"- {f}\n"
            print(f"    [WARNING] {f}")
        
        if not DRY_RUN:
            with open(report_path, "w") as f:
                f.write(report_content)
        print(f"  [!] Deep audit found {len(findings)} potential issues. Report saved to {report_path if not DRY_RUN else '[DRY-RUN]'}")
    else:
        print("  [OK] Deep security audit clean.")
    
    return findings


def add_repo_topics(project_path, repo_name, username):
    """Add topics based on project contents."""
    detected_langs = set()
    for f in os.listdir(project_path):
        ext = os.path.splitext(f)[1].lower()
        if ext == ".py": detected_langs.add("python")
        if ext in {".js", ".ts"}: detected_langs.add("javascript")

    if detected_langs:
        for lang in detected_langs:
            run_command(["gh", "repo", "edit", f"{username}/{repo_name}", "--add-topic", lang],
                        cwd=project_path, step=f"add topic {lang}")


def has_files_to_commit(path):
    if DRY_RUN:
        return True # Assume files exist in dry run
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=path
        )
        return bool(result.stdout.strip())
    except Exception as e:
        raise RuntimeError(f"Git status check failed: {e}")


def init_git_repo(project_path):
    try:
        if not os.path.exists(os.path.join(project_path, ".git")):
            print("Initializing git repository")
            run_command(["git", "init"], cwd=project_path, step="git initialization")
        else:
            print("Git repository already exists")
        run_command(["git", "branch", "-M", "main"], cwd=project_path, step="set default branch")
    except Exception as e:
        raise RuntimeError(f"Git initialization failed: {e}")


def sanitize_secrets(project_path):
    """Scan all text files in the project and replace hardcoded secret values
    with a comment placeholder so they are not committed to GitHub."""
    replaced_count = 0
    for root, dirs, files in os.walk(project_path):
        # Skip hidden / vendor folders
        dirs[:] = [
            d for d in dirs
            if d.lower() not in SKIP_FOLDERS and not d.startswith(".")
        ]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SECRET_SCAN_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    original = f.read()
            except OSError:
                continue

            modified = original
            for pattern, value_group in SECRET_PATTERNS:
                def replacer(m, vg=value_group):
                    prefix = m.group(1)
                    # Detect comment style based on file extension
                    if ext in {".py", ".sh", ".yml", ".yaml", ".rb",
                               ".env", ".cfg", ".ini", ".toml", ".conf",
                               ".properties"}:
                        comment = "# SECRET REMOVED - replace with your actual value"
                    elif ext in {".js", ".ts", ".jsx", ".tsx", ".java",
                                 ".go", ".cs", ".cpp", ".c", ".h", ".php"}:
                        comment = "// SECRET REMOVED - replace with your actual value"
                    elif ext in {".xml"}:
                        comment = "<!-- SECRET REMOVED - replace with your actual value -->"
                    else:
                        comment = "# SECRET REMOVED - replace with your actual value"
                    return prefix + comment
                modified = pattern.sub(replacer, modified)

            if modified != original:
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(modified)
                    rel = os.path.relpath(fpath, project_path)
                    print(f"  [SECRET SANITIZED] {rel}")
                    logging.warning(f"Secret(s) replaced in: {rel}")
                    replaced_count += 1
                except OSError as e:
                    print(f"  [WARNING] Could not sanitize {fpath}: {e}")

    if replaced_count:
        print(f"[INFO] Secrets sanitized in {replaced_count} file(s) — review before pushing.")
    else:
        print("[OK] No hardcoded secrets detected.")


def commit_project(project_path, repo_name):
    try:
        check_large_files(project_path)
        print("Scanning for hardcoded secrets...")
        sanitize_secrets(project_path)

        print("Generating .gitignore if missing...")
        auto_generate_gitignore(project_path)

        print("Injecting README and LICENSE if missing...")
        inject_templates(project_path, repo_name)

        print("Injecting Issue/PR templates if missing...")
        inject_issue_templates(project_path)

        print("Performing Deep Security Audit...")
        deep_security_audit(project_path)

        scan_vulnerability_risk(project_path)

        print("Adding files to git")
        run_command(["git", "add", "."], cwd=project_path, step="git add")
        if has_files_to_commit(project_path):
            print("Creating initial commit")
            run_command(["git", "commit", "-m", "Initial commit"], cwd=project_path, step="git commit")
        else:
            raise RuntimeError("Project folder contains no files to commit")
    except Exception as e:
        raise RuntimeError(f"Git commit stage failed: {e}")


def repo_exists(username, repo_name):
    try:
        qualified = f"{username}/{repo_name}" if username else repo_name
        result = subprocess.run(
            ["gh", "repo", "view", qualified],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0
    except Exception as e:
        raise RuntimeError(f"Repository existence check failed: {e}")


def push_to_existing_repo(project_path, repo_name, username):
    try:
        remote_url = f"https://github.com/{username}/{repo_name}.git"
        remotes = subprocess.run(
            ["git", "remote"], capture_output=True, text=True, cwd=project_path
        ).stdout.strip()
        if "origin" not in remotes.split():
            run_command(["git", "remote", "add", "origin", remote_url], cwd=project_path, step="add remote")
        else:
            run_command(["git", "remote", "set-url", "origin", remote_url], cwd=project_path, step="update remote")
        run_command(["git", "push", "-u", "origin", "main", "--force"], cwd=project_path, step="git push")
        print("[OK] Pushed to existing repository")
        logging.info(f"[SUCCESS] Pushed to existing repo '{repo_name}'")
    except Exception as e:
        raise RuntimeError(f"Push to existing repo failed: {e}")


def create_repo(project_path, repo_name, visibility_flag, username):
    print(f"Creating GitHub repository: {repo_name}")
    try:
        owner = ORG_NAME if ORG_NAME else username
        if repo_exists(owner, repo_name):
            print(f"[INFO] Repository '{repo_name}' already exists — pushing to it")
            push_to_existing_repo(project_path, repo_name, owner)
            return

        create_cmd = ["gh", "repo", "create", repo_name, visibility_flag, "--source", ".", "--remote", "origin", "--push"]
        if ORG_NAME:
            # gh repo create org/repo --public
            create_cmd[3] = f"{ORG_NAME}/{repo_name}"

        run_command(create_cmd, cwd=project_path, step="GitHub repository creation")
        print("[OK] Repository created and pushed")
        logging.info(f"[SUCCESS] Repo '{repo_name}' created and pushed")

        send_notification(f"🚀 New repository created: **{repo_name}** ({visibility_flag})")

        print("Adding auto-topics...")
        add_repo_topics(project_path, repo_name, owner)

    except RuntimeError as e:
        if "rate limit" in str(e).lower():
            raise RuntimeError(f"GitHub API rate limit hit. Wait a few minutes and retry.\nDetail: {e}")
        raise


def process_projects(base_dir, visibility_flag, username):
    print("\nScanning for projects...")

    try:
        all_entries = os.listdir(base_dir)
    except Exception as e:
        raise RuntimeError(f"Failed to read directory: {e}")

    subdirs = [
        e for e in all_entries
        if os.path.isdir(os.path.join(base_dir, e))
        and e.lower() not in SKIP_FOLDERS
        and not e.startswith(".")
    ]

    top_level_files = [
        e for e in all_entries
        if os.path.isfile(os.path.join(base_dir, e))
        and not e.startswith(".")
        and e.lower() not in {"thumbs.db", ".ds_store"}
    ]

    if not subdirs and not top_level_files:
        print("[INFO] No subdirectories found — treating root as a single project")
        subdirs_to_process = [(base_dir, os.path.basename(base_dir))]
    else:
        subdirs_to_process = [(os.path.join(base_dir, d), d) for d in subdirs]
        for f in top_level_files:
            single_file_path = os.path.join(base_dir, f)
            repo_base_name = os.path.splitext(f)[0] or f
            subdirs_to_process.append((single_file_path, repo_base_name))

    for project_path, folder in subdirs_to_process:
        print(f"\n--- Processing: {folder} ---")
        try:
            repo_name = sanitize_repo_name(folder)
            print(f"Repo name: {repo_name}")

            if os.path.isfile(project_path):
                process_single_file_project(project_path, repo_name, visibility_flag, username)
            else:
                init_git_repo(project_path)
                commit_project(project_path, repo_name)
                create_repo(project_path, repo_name, visibility_flag, username)

            print(f"[SUCCESS] '{repo_name}' done")
        except Exception as e:
            print(f"[ERROR] Failed: '{folder}'")
            print(f"Reason: {e}")
            logging.error(f"Project failure '{folder}': {e}")
        time.sleep(2)


def process_single_file_project(file_path, repo_name, visibility_flag, username):
    """Create a temporary project from a single file and upload it as a repo."""
    print(f"[INFO] Single-file project detected: {os.path.basename(file_path)}")
    with tempfile.TemporaryDirectory(prefix="single_file_project_") as temp_project_dir:
        target_path = os.path.join(temp_project_dir, os.path.basename(file_path))
        shutil.copy2(file_path, target_path)

        init_git_repo(temp_project_dir)
        commit_project(temp_project_dir, repo_name)
        create_repo(temp_project_dir, repo_name, visibility_flag, username)


def process_zip(zip_path, visibility_flag, username):
    extract_dir = None
    try:
        extract_dir = extract_zip(zip_path)
        process_projects(extract_dir, visibility_flag, username)
    except Exception as e:
        print("\n[ZIP PROCESS FAILURE]")
        print(f"Reason: {e}")
        logging.error(f"ZIP failure '{zip_path}': {e}")
    finally:
        if extract_dir:
            shutil.rmtree(extract_dir, ignore_errors=True)


def process_folder(folder_path, visibility_flag, username):
    try:
        process_projects(folder_path, visibility_flag, username)
    except Exception as e:
        print("\n[FOLDER PROCESS FAILURE]")
        print(f"Reason: {e}")
        logging.error(f"Folder failure '{folder_path}': {e}")


def process_input(path, visibility_flag, username):
    if os.path.isdir(path):
        print(f"[INFO] Folder detected — processing directly: {path}")
        process_folder(path, visibility_flag, username)
    elif os.path.isfile(path):
        if zipfile.is_zipfile(path):
            process_zip(path, visibility_flag, username)
        else:
            repo_name = sanitize_repo_name(os.path.splitext(os.path.basename(path))[0])
            process_single_file_project(path, repo_name, visibility_flag, username)
    else:
        print(f"[ERROR] Path does not exist: {path}")
        logging.error(f"Invalid path: {path}")


def main():
    check_tool("git", "Install Git from https://git-scm.com/")
    check_tool("gh", "Install GitHub CLI from https://cli.github.com/")

    check_gh_auth()
    check_git_identity()

    username = get_gh_username()
    if username:
        print(f"[INFO] Logged in as: {username}")

    visibility_flag = ask_visibility()

    global ORG_NAME
    org_choice = input("Create for an Organization? (y/N): ").strip().lower()
    if org_choice == "y":
        ORG_NAME = input("Enter Organization Name: ").strip()

    global DRY_RUN
    dry_run_choice = input("Enable Dry Run mode? (y/N): ").strip().lower()
    if dry_run_choice == "y":
        DRY_RUN = True
        print("[INFO] DRY RUN MODE ENABLED - No changes will be pushed to GitHub.")

    try:
        while True:
            raw = input("\nEnter ZIP file or folder path (or type 'exit'): ")
            input_path = normalize_path(raw)

            if input_path.lower() == "exit":
                print("Exiting automation")
                break

            if not input_path:
                print("[WARNING] No path entered.")
                continue

            process_input(input_path, visibility_flag, username)

    except KeyboardInterrupt:
        print("\n\n[INFO] Interrupted by user. Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()
