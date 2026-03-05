import os
import re
import zipfile
import shutil
import subprocess
import uuid
import logging
import time
import sys

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
             \s*[=:]\s*)(['\"])[^'\"\n]{8,}\3""",
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


def has_files_to_commit(path):
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


def commit_project(project_path):
    try:
        check_large_files(project_path)
        print("Scanning for hardcoded secrets...")
        sanitize_secrets(project_path)
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
        if repo_exists(username, repo_name):
            print(f"[INFO] Repository '{repo_name}' already exists — pushing to it")
            push_to_existing_repo(project_path, repo_name, username)
            return

        run_command(
            ["gh", "repo", "create", repo_name, visibility_flag, "--source", ".", "--remote", "origin", "--push"],
            cwd=project_path,
            step="GitHub repository creation"
        )
        print("[OK] Repository created and pushed")
        logging.info(f"[SUCCESS] Repo '{repo_name}' created and pushed")

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

    if not subdirs:
        print("[INFO] No subdirectories found — treating root as a single project")
        subdirs_to_process = [(base_dir, os.path.basename(base_dir))]
    else:
        subdirs_to_process = [(os.path.join(base_dir, d), d) for d in subdirs]

    for project_path, folder in subdirs_to_process:
        print(f"\n--- Processing: {folder} ---")
        try:
            repo_name = sanitize_repo_name(folder)
            print(f"Repo name: {repo_name}")
            init_git_repo(project_path)
            commit_project(project_path)
            create_repo(project_path, repo_name, visibility_flag, username)
            print(f"[SUCCESS] '{repo_name}' done")
        except Exception as e:
            print(f"[ERROR] Failed: '{folder}'")
            print(f"Reason: {e}")
            logging.error(f"Project failure '{folder}': {e}")
        time.sleep(2)


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
        process_zip(path, visibility_flag, username)
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