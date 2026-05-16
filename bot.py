#!/usr/bin/env python3
"""
Upload a whole folder to a GitHub repository (main branch) in one commit.
Uses concurrent blob creation and a single commit for maximum speed.
No cache/temp files are created.
"""

import os
import sys
import base64
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ------------------------------------------------------------
#  Configuration
# ------------------------------------------------------------
MAX_WORKERS = 8          # Number of concurrent blob uploads (adjust for your network)
CHUNK_SIZE = 8192        # For reading files in chunks (not strictly needed for base64)

# ------------------------------------------------------------
#  Helper: Get token from environment or user input
# ------------------------------------------------------------
def get_token():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        token = input("🔐 Enter your GitHub personal access token: ").strip()
        if not token:
            print("❌ Token is required.", file=sys.stderr)
            sys.exit(1)
    return token

# ------------------------------------------------------------
#  Progress display (fallback if tqdm not available)
# ------------------------------------------------------------
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

class ProgressTracker:
    def __init__(self, total_files, total_bytes):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.processed_files = 0
        self.processed_bytes = 0
        self.start_time = time.time()
        self.lock = threading.Lock()
        self.last_update = 0

    def update(self, file_size):
        with self.lock:
            self.processed_files += 1
            self.processed_bytes += file_size
            now = time.time()
            if now - self.last_update < 0.2:   # update at most 5 times per second
                return
            self.last_update = now
            self._display()

    def _format_time(self, seconds):
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.0f}s"

    def _display(self):
        elapsed = time.time() - self.start_time
        files_pct = (self.processed_files / self.total_files) * 100
        bytes_pct = (self.processed_bytes / self.total_bytes) * 100 if self.total_bytes else 0
        speed = self.processed_bytes / elapsed / 1_000_000 if elapsed > 0 else 0   # MB/s

        # ETA based on bytes (more accurate)
        if speed > 0 and self.processed_bytes < self.total_bytes:
            eta = (self.total_bytes - self.processed_bytes) / (speed * 1_000_000)
        else:
            eta = 0

        # Build progress bar (simple ASCII)
        bar_len = 30
        filled = int(bar_len * bytes_pct / 100)
        bar = '█' * filled + '░' * (bar_len - filled)

        line = (f"\r[{bar}] {bytes_pct:.1f}% | "
                f"files: {self.processed_files}/{self.total_files} ({files_pct:.1f}%) | "
                f"{self.processed_bytes/1_000_000:.1f}/{self.total_bytes/1_000_000:.1f} MB | "
                f"speed: {speed:.2f} MB/s | "
                f"ETA: {self._format_time(eta)} | "
                f"elapsed: {self._format_time(elapsed)}")

        sys.stdout.write(line)
        sys.stdout.flush()

    def finish(self):
        elapsed = time.time() - self.start_time
        print(f"\n✅ Upload complete! {self.processed_files} files, {self.processed_bytes/1_000_000:.2f} MB in {self._format_time(elapsed)}")

# ------------------------------------------------------------
#  GitHub API interactions
# ------------------------------------------------------------
class GitHubUploader:
    def __init__(self, token, repo_full_name, branch="main"):
        self.token = token
        self.repo_full_name = repo_full_name
        self.branch = branch
        self.api_base = "https://api.github.com"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        self.repo_url = f"{self.api_base}/repos/{repo_full_name}"

    def get_current_commit_sha(self):
        """Get the SHA of the latest commit on the target branch."""
        url = f"{self.repo_url}/git/refs/heads/{self.branch}"
        resp = requests.get(url, headers=self.headers)
        if resp.status_code == 404:
            # Branch might not exist; we'll create it from the default branch later
            # For simplicity, assume 'main' exists or we need to create.
            # Better: get default branch SHA
            repo_info = requests.get(self.repo_url, headers=self.headers).json()
            default_branch = repo_info.get("default_branch", "main")
            url = f"{self.repo_url}/git/refs/heads/{default_branch}"
            resp = requests.get(url, headers=self.headers)
            if resp.status_code != 200:
                raise Exception(f"Cannot get commit SHA: {resp.status_code} {resp.text}")
        if resp.status_code != 200:
            raise Exception(f"Failed to get commit SHA: {resp.status_code} {resp.text}")
        return resp.json()["object"]["sha"]

    def create_blob(self, file_path, content_bytes):
        """Create a blob and return its SHA."""
        url = f"{self.repo_url}/git/blobs"
        data = {
            "content": base64.b64encode(content_bytes).decode("utf-8"),
            "encoding": "base64"
        }
        resp = requests.post(url, json=data, headers=self.headers)
        if resp.status_code != 201:
            raise Exception(f"Blob creation failed for {file_path}: {resp.status_code} {resp.text}")
        return resp.json()["sha"]

    def create_tree(self, base_tree_sha, tree_items):
        """Create a tree with the given items."""
        url = f"{self.repo_url}/git/trees"
        data = {
            "base_tree": base_tree_sha,
            "tree": tree_items
        }
        resp = requests.post(url, json=data, headers=self.headers)
        if resp.status_code != 201:
            raise Exception(f"Tree creation failed: {resp.status_code} {resp.text}")
        return resp.json()["sha"]

    def create_commit(self, parent_sha, tree_sha, message):
        """Create a commit object."""
        url = f"{self.repo_url}/git/commits"
        data = {
            "message": message,
            "tree": tree_sha,
            "parents": [parent_sha]
        }
        resp = requests.post(url, json=data, headers=self.headers)
        if resp.status_code != 201:
            raise Exception(f"Commit creation failed: {resp.status_code} {resp.text}")
        return resp.json()["sha"]

    def update_branch(self, commit_sha):
        """Update the branch reference to point to the new commit."""
        url = f"{self.repo_url}/git/refs/heads/{self.branch}"
        data = {"sha": commit_sha, "force": True}
        resp = requests.patch(url, json=data, headers=self.headers)
        if resp.status_code != 200:
            raise Exception(f"Branch update failed: {resp.status_code} {resp.text}")
        return True

# ------------------------------------------------------------
#  Collect local files
# ------------------------------------------------------------
def collect_files(local_path):
    """Walk directory, return list of (relative_path, full_path, size)."""
    root = Path(local_path).resolve()
    files = []
    for file_path in root.rglob("*"):
        if file_path.is_file():
            # Ignore .git folder (if any)
            if ".git" in file_path.parts:
                continue
            rel_path = str(file_path.relative_to(root)).replace("\\", "/")
            size = file_path.stat().st_size
            files.append((rel_path, file_path, size))
    return files

# ------------------------------------------------------------
#  Main upload procedure
# ------------------------------------------------------------
def main():
    print("=" * 60)
    print("🚀 GitHub Bulk Folder Uploader (Single Commit)")
    print("=" * 60)

    token = get_token()

    repo_input = input("📦 GitHub repo full name (e.g., 'username/repo'): ").strip()
    if "/" not in repo_input:
        print("❌ Please provide the full repo name including username/organization, e.g., 'octocat/Hello-World'")
        sys.exit(1)

    folder_input = input("📁 Local folder path to upload: ").strip()
    folder_path = Path(folder_input)
    if not folder_path.exists() or not folder_path.is_dir():
        print(f"❌ Folder '{folder_input}' does not exist or is not a directory.")
        sys.exit(1)

    print("📂 Scanning files...")
    files = collect_files(folder_path)
    if not files:
        print("⚠️ No files found to upload.")
        return
    total_bytes = sum(f[2] for f in files)
    total_files = len(files)
    print(f"✅ Found {total_files} files, total size: {total_bytes/1_000_000:.2f} MB")

    uploader = GitHubUploader(token, repo_input, branch="main")

    print("🔍 Fetching current commit SHA...")
    try:
        parent_sha = uploader.get_current_commit_sha()
    except Exception as e:
        print(f"❌ Failed to get commit SHA: {e}")
        sys.exit(1)

    # Step 1: Create blobs concurrently with progress tracking
    print("☁️  Uploading file blobs (concurrent)...")
    progress = ProgressTracker(total_files, total_bytes)
    # We'll store (rel_path, blob_sha, file_mode)
    tree_items = []
    results = [None] * total_files   # placeholder for results
    failed = []

    def upload_one(idx, rel_path, full_path, size):
        try:
            with open(full_path, "rb") as f:
                content = f.read()
            blob_sha = uploader.create_blob(rel_path, content)
            return idx, rel_path, blob_sha, size, None
        except Exception as e:
            return idx, rel_path, None, size, str(e)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(upload_one, i, rel_path, full_path, size): i
            for i, (rel_path, full_path, size) in enumerate(files)
        }
        for future in as_completed(futures):
            idx, rel_path, blob_sha, size, err = future.result()
            if err:
                failed.append((rel_path, err))
            else:
                tree_items.append({
                    "path": rel_path,
                    "mode": "100644",   # regular file
                    "type": "blob",
                    "sha": blob_sha
                })
            progress.update(size)

    if failed:
        print(f"\n⚠️ {len(failed)} files failed to upload:")
        for path, err in failed[:10]:   # show first 10
            print(f"   - {path}: {err}")
        print("Aborting commit creation.")
        sys.exit(1)

    # Sort tree_items to ensure deterministic order (optional)
    tree_items.sort(key=lambda x: x["path"])

    # Step 2: Create the tree
    print("\n🌳 Creating git tree...")
    try:
        tree_sha = uploader.create_tree(parent_sha, tree_items)
    except Exception as e:
        print(f"❌ Tree creation failed: {e}")
        sys.exit(1)

    # Step 3: Create commit
    commit_message = f"THIS IS ALL PAID DATABASES FROM @NR-CODEX"
    print("📝 Creating commit...")
    try:
        commit_sha = uploader.create_commit(parent_sha, tree_sha, commit_message)
    except Exception as e:
        print(f"❌ Commit creation failed: {e}")
        sys.exit(1)

    # Step 4: Update branch
    print("🔄 Updating branch...")
    try:
        uploader.update_branch(commit_sha)
    except Exception as e:
        print(f"❌ Branch update failed: {e}")
        sys.exit(1)

    progress.finish()
    print(f"🎉 Success! All files pushed to: https://github.com/{repo_input}/tree/main")

if __name__ == "__main__":
    main()