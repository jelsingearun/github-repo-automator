import os
import re
import zipfile
import shutil
import subprocess
import uuid
import logging
import time
import sys

# ---------------------------------------------------------------------------
# Logging — stored beside the script, not wherever the user runs it from (7.1)
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_automation.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

LARGE_FILE_LIMIT_MB = 100  # GitHub rejects files larger than this (5.6)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_command(cmd, cwd=None, step="operation"):
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.strip()
        error_msg = f"\n[FAILED] {step}\nCommand: {' '.join(cmd)}\nReason: {stderr}"
        print(error_msg)
        logging.error(error_msg)
        raise


def sanitize_repo_name(name):
    try:
        name = name.lower()
        name = re.sub(r'[^a-z0-9-_]', '-', name)
        name = re.sub(r'-+', '-', name)
        name = name.strip("-")
        if not name:
            raise ValueError("Sanitized repo name is empty")  # (4.3)
        return name
    except Exception as e:
        raise RuntimeError(f"Repo name sanitization failed: {e}")


def normalize_path(raw):
    """Strip quotes, whitespace, trailing slashes and normalize separators."""  # (1.7, 1.8)
    path = raw.strip().strip('"').strip("'").rstrip("/\\")
    return os.path.normpath(path)


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def check_tool(tool, install_hint):
    """Verify an external tool is available on PATH."""  # (5.1, 6.2)
    try:
        subprocess.run(
            [tool, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
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
    """Return the currently authenticated GitHub username."""
    try:
        return run_command(["gh", "api", "user", "--jq", ".login"], step="Fetch GitHub username")
    except Exception:
        return None


def check_git_identity():
    """Ensure git user.name and user.email are configured."""  # (5.5)
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


# ---------------------------------------------------------------------------
# Repository visibility
# ---------------------------------------------------------------------------

def ask_visibility():
    """Ask the user whether to create public or private repos."""  # (6.6)
    while True:
        choice = input("Create repositories as [P]ublic or [R]ivate? ").strip().lower()
        if choice in ("p", "public"):
            return "--public"
        if choice in ("r", "private"):
            return "--private"
        print("Please enter 'P' for public or 'R' for private.")


# ---------------------------------------------------------------------------
# ZIP handling
# ---------------------------------------------------------------------------

def check_large_files(directory):
    """Warn if any file exceeds the GitHub 100 MB limit."""  # (5.6)
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
        print(f"[WARNING] The following files exceed {LARGE_FILE_LIMIT_MB} MB and may be rejected by GitHub:")
        for w in warnings:
            print(w)
        print("  Consider using Git LFS or removing them before pushing.")


def extract_zip(zip_path):
    print(f"\nExtracting ZIP: {zip_path}")

    # (2.10) must be a file, not a directory
    if not os.path.isfile(zip_path):
        if os.path.isdir(zip_path):
            raise FileNotFoundError("The path points to a directory, not a ZIP file")
        raise FileNotFoundError("ZIP path does not exist")

    # (2.4) catch corrupt ZIPs early
    try:
        if not zipfile.is_zipfile(zip_path):
            raise ValueError("Provided file is not a valid ZIP archive")
    except zipfile.BadZipFile as e:
        raise ValueError(f"ZIP file is corrupted: {e}")

    # (2.5) check ZIP has contents
    with zipfile.ZipFile(zip_path, 'r') as zf:
        if not zf.namelist():
            raise ValueError("ZIP archive is empty")

        # (2.3) detect password protection
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                raise ValueError("ZIP archive is password-protected — cannot extract")

    extract_dir = os.path.join(
        os.path.dirname(zip_path),
        f"extract_{uuid.uuid4().hex}"
    )

    try:
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        print("[OK] Extraction completed")
        return extract_dir

    # (2.9) disk space / OS errors
    except OSError as e:
        raise RuntimeError(f"ZIP extraction failed (disk space or OS error): {e}")
    except Exception as e:
        raise RuntimeError(f"ZIP extraction failed: {e}")


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def has_files_to_commit(path):
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=path
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

        # (6.8) ensure default branch is 'main' to match GitHub's default
        run_command(["git", "branch", "-M", "main"], cwd=project_path, step="set default branch")

    except Exception as e:
        raise RuntimeError(f"Git initialization failed: {e}")


def commit_project(project_path):
    try:
        check_large_files(project_path)  # (5.6)

        print("Adding files to git")
        run_command(["git", "add", "."], cwd=project_path, step="git add")

        if has_files_to_commit(project_path):
            print("Creating initial commit")
            run_command(
                ["git", "commit", "-m", "Initial commit"],
                cwd=project_path,
                step="git commit"
            )
        else:
            raise RuntimeError("Project folder contains no files to commit")

    except Exception as e:
        raise RuntimeError(f"Git commit stage failed: {e}")


# ---------------------------------------------------------------------------
# GitHub repository creation
# ---------------------------------------------------------------------------

def repo_exists(username, repo_name):
    """Check using owner/repo format so the correct account is queried."""  # (6.7)
    try:
        qualified = f"{username}/{repo_name}" if username else repo_name
        result = subprocess.run(
            ["gh", "repo", "view", qualified],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return result.returncode == 0
    except Exception as e:
        raise RuntimeError(f"Repository existence check failed: {e}")


def create_repo(project_path, repo_name, visibility_flag, username):
    print(f"Creating GitHub repository: {repo_name}")

    try:
        if repo_exists(username, repo_name):
            raise RuntimeError(f"Repository '{repo_name}' already exists on GitHub")

        run_command(
            [
                "gh", "repo", "create",
                repo_name,
                visibility_flag,
                "--source", ".",
                "--remote", "origin",
                "--push"
            ],
            cwd=project_path,
            step="GitHub repository creation"
        )

        print("[OK] Repository created and pushed")
        logging.info(f"[SUCCESS] Repo '{repo_name}' created and pushed")  # (7.2)

    except RuntimeError as e:
        # (6.5) surface rate-limit messages clearly
        msg = str(e)
        if "rate limit" in msg.lower():
            raise RuntimeError(f"GitHub API rate limit hit. Wait a few minutes and retry.\nDetail: {e}")
        raise


# ---------------------------------------------------------------------------
# Project processing
# ---------------------------------------------------------------------------

SKIP_FOLDERS = {"__macosx", ".ds_store", "node_modules", ".git"}  # (4.2, 4.4)


def process_projects(base_dir, visibility_flag, username):
    print("\nScanning extracted directory for projects...")

    try:
        all_entries = os.listdir(base_dir)
    except Exception as e:
        raise RuntimeError(f"Failed to read extracted directory: {e}")

    # (2.6) Detect flat ZIP — no subdirectories at the top level
    subdirs = [
        e for e in all_entries
        if os.path.isdir(os.path.join(base_dir, e))
        and e.lower() not in SKIP_FOLDERS
        and not e.startswith(".")  # (4.4) skip hidden
    ]

    if not subdirs:
        # Flat ZIP: the extracted root itself is the single project
        print("[INFO] No subdirectories found — treating extracted root as a single project")
        folder_name = os.path.basename(base_dir)
        subdirs_to_process = [(base_dir, folder_name)]
    else:
        subdirs_to_process = [(os.path.join(base_dir, d), d) for d in subdirs]

    if not subdirs_to_process:
        print("No project folders detected")
        return

    for project_path, folder in subdirs_to_process:
        print("\n------------------------------")
        print(f"Processing project: {folder}")

        try:
            repo_name = sanitize_repo_name(folder)
            print(f"Sanitized repo name: {repo_name}")

            init_git_repo(project_path)
            commit_project(project_path)
            create_repo(project_path, repo_name, visibility_flag, username)

            print(f"[SUCCESS] Project '{repo_name}' uploaded successfully")

        except Exception as e:
            print(f"[ERROR] Failed processing project '{folder}'")
            print(f"Reason: {e}")
            logging.error(f"Project failure '{folder}': {e}")

        time.sleep(2)


# ---------------------------------------------------------------------------
# ZIP pipeline
# ---------------------------------------------------------------------------

def process_zip(zip_path, visibility_flag, username):
    extract_dir = None  # (3.2) must be initialised before try so finally is safe

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


# ---------------------------------------------------------------------------
# Folder pipeline
# ---------------------------------------------------------------------------

def process_folder(folder_path, visibility_flag, username):
    """Process an already-extracted/unzipped project folder directly."""
    try:
        process_projects(folder_path, visibility_flag, username)
    except Exception as e:
        print("\n[FOLDER PROCESS FAILURE]")
        print(f"Reason: {e}")
        logging.error(f"Folder failure '{folder_path}': {e}")


# ---------------------------------------------------------------------------
# Input router — ZIP or folder
# ---------------------------------------------------------------------------

def process_input(path, visibility_flag, username):
    """Route to ZIP or folder handler depending on what the path points to."""
    if os.path.isdir(path):
        print(f"[INFO] Path is a folder — processing directly: {path}")
        process_folder(path, visibility_flag, username)
    elif os.path.isfile(path):
        process_zip(path, visibility_flag, username)
    else:
        print(f"[ERROR] Path does not exist or is not accessible: {path}")
        logging.error(f"Invalid path supplied: {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # (5.1, 6.2) pre-flight tool checks
    check_tool("git", "Install Git from https://git-scm.com/")
    check_tool("gh", "Install GitHub CLI from https://cli.github.com/")

    check_gh_auth()
    check_git_identity()  # (5.5)

    username = get_gh_username()
    if username:
        print(f"[INFO] Logged in as: {username}")

    visibility_flag = ask_visibility()  # (6.6)

    try:  # (1.5) handle Ctrl+C cleanly
        while True:
            raw = input("\nEnter ZIP file or folder path (or type 'exit'): ")
            input_path = normalize_path(raw)  # (1.7, 1.8)

            if input_path.lower() == "exit":
                print("Exiting automation")
                break

            if not input_path:  # (1.6) empty input
                print("[WARNING] No path entered. Please provide a ZIP file or folder path.")
                continue

            process_input(input_path, visibility_flag, username)

    except KeyboardInterrupt:
        print("\n\n[INFO] Interrupted by user. Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()