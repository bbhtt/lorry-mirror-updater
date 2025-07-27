import argparse
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from typing import TYPE_CHECKING

from . import __version__

try:
    import gitlab

    GITLAB_IMPORTED = True
except ImportError:
    GITLAB_IMPORTED = False

if TYPE_CHECKING and not GITLAB_IMPORTED:
    import gitlab

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def run_command(
    command: list[str],
    check: bool = True,
    capture_output: bool = False,
    cwd: str | None = None,
    message: str | None = None,
    warn: bool = False,
) -> CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=check,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            text=True,
            cwd=cwd,
        )
    except subprocess.CalledProcessError as e:
        if message and warn:
            logging.warning("%s: %s", message, e.stderr.strip() if e.stderr else "")
        elif message and not warn:
            logging.error("%s: %s", message, e.stderr.strip() if e.stderr else "")
        else:
            logging.error(
                "Command failed: %s\nError: %s",
                " ".join(command),
                e.stderr.strip() if e.stderr else "",
            )
        raise


def element_exists(element: str, path: str) -> bool:
    command = [
        "bst",
        "--no-interactive",
        "show",
        "--deps",
        "none",
        "-f",
        "%{name}",
        element,
    ]
    try:
        run_command(
            command,
            cwd=path,
            message=f"Did not find element: {element}",
            warn=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def run_git(
    args: list[str],
    repo_path: str | None = None,
    capture_output: bool = False,
    message: str | None = None,
    warn: bool = False,
) -> CompletedProcess[str]:
    if repo_path is None:
        repo_path = str(Path.cwd())
    command = ["git", "-c", "credential.interactive=false", "-C", repo_path, *args]
    logging.info("Running command: %s", " ".join(command))
    return run_command(
        command, capture_output=capture_output, message=message, warn=warn
    )


def is_cmd_present(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def is_git_dir(path: str | None = None) -> bool:
    try:
        run_git(["rev-parse"], path, message="Not a git repository")
        return True
    except subprocess.CalledProcessError:
        return False


def is_dirty(repo_path: str | None = None, subdir: str | None = None) -> bool:
    try:
        args = ["status", "--porcelain"]
        if subdir:
            args += ["--", subdir]
        result = run_git(
            args, repo_path, capture_output=True, message="Failed to check git status"
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return True


def git_add(repo_path: str | None = None, *paths: str) -> bool:
    try:
        run_git(
            ["add", *paths],
            repo_path=repo_path,
            message="Failed to add files to git",
        )
        return True
    except subprocess.CalledProcessError:
        return False


def git_commit(
    repo_path: str | None = None, message: str = "(Automated) Update mirrors"
) -> bool:
    try:
        run_git(
            ["commit", "-m", message],
            repo_path=repo_path,
            message="Failed to commit changes",
        )
        return True
    except subprocess.CalledProcessError:
        return False


def get_toplevel(path: str | None = None) -> str | None:
    try:
        result = run_git(
            ["rev-parse", "--show-toplevel"],
            path,
            capture_output=True,
            message="Failed to determine git toplevel",
        )
        toplevel: str = result.stdout.strip()
        logging.info("Found git toplevel: %s", toplevel)
        return toplevel
    except subprocess.CalledProcessError:
        return None


def validate_environment() -> bool:
    validations = [
        (is_cmd_present("git"), "Unable to find git in PATH"),
        (is_cmd_present("bst"), "Unable to find bst-to-lorry in PATH"),
        (is_cmd_present("bst-to-lorry"), "Unable to find auto_updater in PATH"),
        (is_git_dir(), "Current directory is not a git repository"),
        (not is_dirty(), "Current repository checkout is dirty"),
    ]
    for valid, msg in validations:
        if not valid:
            logging.error(msg)
            return False
    return True


def load_mirror_config(file: str) -> dict[str, dict[str, list[str]]]:
    try:
        with open(file, encoding="utf-8") as f:
            config: dict[str, dict[str, list[str]]] = json.load(f)
        logging.info("Loaded mirror configuration from %s", file)
        return config
    except FileNotFoundError:
        logging.error("Mirror configuration file not found: %s", file)
        raise
    except json.JSONDecodeError as e:
        logging.error("Invalid JSON in mirror configuration file: %s", e)
        raise


@contextmanager
def clone_repo(url: str) -> Generator[tuple[bool, str | None], None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_name = url.split("/")[-1].replace(".git", "")
        dest_path = Path(tmpdir) / repo_name

        try:
            run_git(
                ["clone", url, str(dest_path)],
                message=f"Failed to clone repository: {url}",
            )
            logging.info("Cloned %s to %s", url, dest_path)
            yield True, str(dest_path)
        except subprocess.CalledProcessError:
            yield False, None
            return


def checkout_branch(branch: str, repo_path: str | None = None) -> bool:
    try:
        run_git(
            ["checkout", branch],
            repo_path,
            message=f"Failed to checkout branch: {branch}",
        )
        logging.info("Checked out %s in repo %s", branch, repo_path)
        return True
    except subprocess.CalledProcessError:
        return False


def create_branch(base_branch: str) -> str | None:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    branch_name = f"update-mirrors/{base_branch}/{timestamp}"
    try:
        run_git(
            ["checkout", "-b", branch_name, base_branch],
            message=f"Failed to create branch {branch_name} from {base_branch}",
        )
        logging.info("Created branch %s from %s", branch_name, base_branch)
        return branch_name
    except subprocess.CalledProcessError:
        return None


def run_bst_to_lorry(
    elements: list[str],
    git_dir: str,
    raw_files_dir: str,
    exclude_aliases: list[str],
    cwd: str,
    lorry2: bool = False,
) -> bool:
    command = [
        "bst-to-lorry",
        *elements,
        "--git-directory",
        git_dir,
        "--raw-files-directory",
        raw_files_dir,
    ]

    if not lorry2:
        command.append("--refspecs")

    for alias in exclude_aliases:
        command.extend(["--exclude-alias", alias])

    logging.info("Running bst-to-lorry: %s", " ".join(command))
    try:
        run_command(command, cwd=cwd, message="bst-to-lorry failed")
        return True
    except subprocess.CalledProcessError:
        return False


def process_branch(
    repo_url: str,
    branch: str,
    elements: list[str],
    clone_dest: str,
    git_dir: str,
    raw_files_dir: str,
    exclude_aliases: list[str],
    lorry2: bool,
) -> bool:
    logging.info(
        "Processing branch: %s of repo %s with elements: %s",
        branch,
        repo_url,
        elements,
    )

    if not checkout_branch(branch, str(clone_dest)):
        return False

    missing_elements = [
        elem for elem in elements if not element_exists(elem, str(clone_dest))
    ]

    if missing_elements:
        logging.error(
            "Required elements not found in branch %s of repo %s: %s",
            branch,
            repo_url,
            missing_elements,
        )
        return False

    logging.info("All required elements found in branch %s: %s", branch, elements)

    if not run_bst_to_lorry(
        elements, git_dir, raw_files_dir, exclude_aliases, clone_dest, lorry2
    ):
        logging.error("bst-to-lorry failed for branch %s in repo %s", branch, repo_url)
        return False

    return True


def process_repo(
    repo_url: str,
    repo_config: dict[str, list[str]],
    git_dir: str,
    raw_files_dir: str,
    exclude_aliases: list[str],
    base_branch: str,
    lorry2: bool,
) -> bool:
    with clone_repo(repo_url) as (clone_status, clone_dest):
        if not clone_status or clone_dest is None:
            return False

        if not checkout_branch(base_branch):
            return False

        for branch, config_elements in repo_config.items():
            if not process_branch(
                repo_url,
                branch,
                config_elements,
                clone_dest,
                git_dir,
                raw_files_dir,
                exclude_aliases,
                lorry2,
            ):
                return False

    return True


def commit_changes(
    git_dir: str, raw_files_dir: str, base_branch: str
) -> tuple[bool, str | None]:
    local_br = create_branch(base_branch)
    if not local_br:
        return False, None

    if git_add(None, git_dir, raw_files_dir) and git_commit():
        return True, local_br
    return False, None


def process_mirroring(
    mirror_config: dict[str, dict[str, list[str]]],
    git_dir: str,
    raw_files_dir: str,
    exclude_aliases: list[str],
    base_branch: str,
    lorry2: bool,
) -> tuple[bool, str | None]:
    for repo_url, repo_config in mirror_config.items():
        if not process_repo(
            repo_url,
            repo_config,
            git_dir,
            raw_files_dir,
            exclude_aliases,
            base_branch,
            lorry2,
        ):
            return False, None

    if is_dirty(subdir=raw_files_dir) or is_dirty(subdir=git_dir):
        return commit_changes(git_dir, raw_files_dir, base_branch)

    logging.warning("Nothing to commit")
    return True, None


def cleanup_branches(
    project: "gitlab.v4.objects.Project",
    branch_regex: str = r"^update-mirrors/([^/]+)/(\d+)$",
) -> None:
    branches = project.branches.list(iterator=True, regex=branch_regex)
    open_mrs = project.mergerequests.list(state="opened", iterator=True)
    branch_names = {branch.name for branch in branches}
    open_mr_branches = {
        mr.source_branch for mr in open_mrs if re.match(branch_regex, mr.source_branch)
    }
    branches_without_open_mrs = branch_names - open_mr_branches
    project.delete_merged_branches()
    for branch in branches_without_open_mrs:
        logging.info("Deleting branch: %s", branch)
        project.branches.delete(branch)


def create_merge_request(
    source_branch: str,
    base_branch: str,
    mr_title: str = "(Automated) Update mirrors",
    clear_br: bool = True,
) -> bool:
    token = os.environ.get("GITLAB_API_KEY") or os.environ.get("FREEDESKTOP_API_TOKEN")
    if not token:
        logging.error("GITLAB_API_KEY is not defined")
        return False

    if not (
        (project_id := os.environ.get("CI_PROJECT_ID"))
        and (gitlab_url := os.environ.get("CI_SERVER_URL"))
    ):
        logging.error(
            "CI_PROJECT_ID or CI_SERVER_URL is not defined. "
            "Likely running outside of GitLab pipeline"
        )
        return False

    try:
        gl = gitlab.Gitlab(gitlab_url, private_token=token)
        project = gl.projects.get(project_id, lazy=True)
        mr = project.mergerequests.create(
            {
                "source_branch": source_branch,
                "target_branch": base_branch,
                "title": mr_title,
            }
        )
        logging.info("Merge request created: %s", mr.web_url)
        if clear_br:
            cleanup_branches(project)
        return True
    except gitlab.exceptions.GitlabError as e:
        logging.error("Failed to create merge request: %s", e)
        return False


def push_branch_to_remote(branch: str, remote: str = "origin") -> bool:
    if not checkout_branch(branch):
        return False
    try:
        run_git(
            ["push", "--set-upstream", "-f", remote, branch],
            message=f"Failed to push branch {branch} to {remote}",
        )
        logging.info("Pushed %s to %s", branch, remote)
        return True
    except subprocess.CalledProcessError:
        return False


def main() -> int:
    default_git_dir = "gits"
    default_raw_files_dir = "files"
    description = textwrap.dedent("""\
        Lorry mirror updater

        A wrapper for bst-to-lorry to automate updating lorry files and sending Gitlab
        merge requests to the mirroring-config repository. This is expected to be run
        from a clean checkout of the mirroring-config repository.
    """)
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawTextHelpFormatter,
        usage=argparse.SUPPRESS,
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", help="Show this help message and exit"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the version and exit",
    )
    parser.add_argument(
        "--mirror-config",
        default="mirrors.json",
        help="Mirror config file",
        type=str,
        metavar="",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        metavar="",
        help="Base branch of mirroring-config repository",
    )
    parser.add_argument(
        "--git-directory",
        default=default_git_dir,
        metavar="",
        help="Path to the Git directory for bst-to-lorry",
    )
    parser.add_argument(
        "--raw-files-directory",
        default=default_raw_files_dir,
        metavar="",
        help="Path to the raw files directory for bst-to-lorry",
    )
    parser.add_argument(
        "--exclude-alias",
        nargs="*",
        metavar="",
        default=["fdsdk_git", "fdsdk_mirror"],
        help="List of aliases to exclude in bst-to-lorry",
    )
    parser.add_argument(
        "--push",
        default=False,
        action="store_true",
        help="Push the branch to remote repository",
    )
    parser.add_argument(
        "--create-mr",
        default=False,
        action="store_true",
        help="Push the branch to remote repository",
    )
    parser.add_argument(
        "--lorry2",
        default=False,
        action="store_true",
        help="Use lorry2 format in bst-to-lorry",
    )
    args = parser.parse_args()

    if args.create_mr and not args.push:
        logging.error("--create-mr requires --push")
        return 1

    if args.create_mr and not GITLAB_IMPORTED:
        logging.error("--create-mr used but python-gitlab not imported")
        return 1

    if not validate_environment():
        return 1

    try:
        mirror_config = load_mirror_config(args.mirror_config)
    except (FileNotFoundError, json.JSONDecodeError):
        return 1

    git_toplevel = get_toplevel()
    if not git_toplevel:
        logging.error("Failed to determine the top-level git directory")
        return 1

    default_git_dir = str(Path(git_toplevel) / "gits")
    default_raw_files_dir = str(Path(git_toplevel) / "files")
    args.git_directory = default_git_dir
    args.raw_files_directory = default_raw_files_dir

    status, branch = process_mirroring(
        mirror_config,
        args.git_directory,
        args.raw_files_directory,
        args.exclude_alias,
        args.base_branch,
        args.lorry2,
    )

    if status is False and branch is None:
        return 1

    if status and branch and args.push:
        push_ret = push_branch_to_remote(branch)
        if not push_ret:
            return 1
        if (
            GITLAB_IMPORTED
            and args.create_mr
            and push_ret
            and not create_merge_request(branch, args.base_branch)
        ):
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
