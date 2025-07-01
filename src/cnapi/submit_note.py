import json
from typing import Any, Dict, Set

from .xurl_util import run_xurl

from data_models import ProposedMisleadingNote


def get_notes_written_by_user(test_mode: bool = True, max_results: int = 100) -> Set[str]:
    """
    Get all post IDs that the user has already written notes for.
    Uses the official API endpoint: GET /2/notes/search/notes_written
    Returns a set of post_id strings for fast lookup.
    """
    try:
        path = (
            "/2/notes/search/notes_written"
            f"?test_mode={'true' if test_mode else 'false'}"
            f"&max_results={max_results}"
        )
        
        cmd = [
            "xurl",
            path,
        ]
        
        result = run_xurl(cmd, verbose_if_failed=True)
        
        print(f"DEBUG: Raw API response: {result}")
        print(f"DEBUG: Response type: {type(result)}")
        
        # Extract post_ids from the response
        post_ids = set()
        if isinstance(result, dict):
            print(f"DEBUG: Response keys: {result.keys()}")
            
            if "data" in result:
                print(f"DEBUG: Found 'data' key with {len(result['data'])} items")
                for i, note in enumerate(result["data"]):
                    print(f"DEBUG: Note {i}: {note}")
                    if "post_id" in note:
                        post_id = str(note["post_id"])
                        post_ids.add(post_id)
                        print(f"DEBUG: Added post_id: {post_id}")
                    else:
                        print(f"DEBUG: Note {i} missing 'post_id' field")
            else:
                print("DEBUG: No 'data' key found in response")
        else:
            print(f"DEBUG: Response is not a dict: {result}")
        
        print(f"DEBUG: Final post_ids set: {post_ids}")
        print(f"DEBUG: Returning {len(post_ids)} post IDs")
        return post_ids
        
    except Exception as e:
        print(f"ERROR: Could not fetch existing notes: {e}")
        print(f"ERROR: Exception type: {type(e)}")
        import traceback
        print(f"ERROR: Traceback: {traceback.format_exc()}")
        return set()  # Return empty set on error, so we don't skip posts unnecessarily


def submit_note(
    note: ProposedMisleadingNote,
    test_mode: bool = True,
    verbose_if_failed: bool = False,
) -> Dict[str, Any]:
    """
    Submit a note to the Community Notes API. For more details, see:
    https://docs.x.com/x-api/community-notes/introduction
    """
    payload = {
        "test_mode": test_mode,
        "post_id": note.post_id,
        "info": {
            "text": note.note_text,
            "classification": "misinformed_or_potentially_misleading",
            "misleading_tags": [tag.value for tag in note.misleading_tags],
            "trustworthy_sources": True,
        },
    }

    cmd = [
        "xurl",
        "-X",
        "POST",
        "/2/notes",
        "-d",
        json.dumps(payload),
    ]

    return run_xurl(cmd, verbose_if_failed=verbose_if_failed)
