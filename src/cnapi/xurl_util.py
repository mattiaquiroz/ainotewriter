import json
import subprocess
from typing import Any, Dict, List


def run_xurl(cmd: List[str], verbose_if_failed: bool = False) -> Dict[str, Any]:
    """
    Run `xurl` and return its JSON stdout as a Python dict.
    Currently extremely simple without any retry logic.
    """
    try:
        completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        error_msg = f"xurl command failed with exit code {exc.returncode}"
        
        if verbose_if_failed:
            print(f"\n[ {error_msg} ]", flush=True)
            print(f"Command: {' '.join(cmd)}")
            if exc.stdout:
                print("── stdout ──")
                print(exc.stdout, end="", flush=True)
            if exc.stderr:
                print("── stderr ──")
                print(exc.stderr, end="", flush=True)
        
        # Create a more detailed error message
        if exc.stderr:
            error_msg += f"\nStderr: {exc.stderr.strip()}"
        if exc.stdout:
            error_msg += f"\nStdout: {exc.stdout.strip()}"
        
        raise subprocess.CalledProcessError(exc.returncode, cmd, exc.stdout, exc.stderr) from exc
    
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        error_msg = f"Failed to parse xurl response as JSON: {str(e)}"
        if verbose_if_failed:
            print(f"\n[ {error_msg} ]", flush=True)
            print(f"Raw stdout: {completed.stdout}")
        raise Exception(error_msg)
