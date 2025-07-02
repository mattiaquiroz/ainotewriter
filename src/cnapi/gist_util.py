import os
import json
import requests
from typing import List, Set


def get_processed_post_ids() -> Set[str]:
    """
    Retrieve the list of already processed post IDs from the GitHub Gist.
    Returns an empty set if the Gist is not accessible or doesn't contain valid data.
    """
    gist_token = os.getenv("GIST_TOKEN")
    gist_id = os.getenv("GIST_ID")
    
    if not gist_token or not gist_id:
        print("Warning: GIST_TOKEN or GIST_ID not found in environment variables")
        return set()
    
    try:
        headers = {
            "Authorization": f"token {gist_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers)
        response.raise_for_status()
        
        gist_data = response.json()
        
        # Get the post_ids.json file content
        files = gist_data.get("files", {})
        post_ids_file = files.get("post_ids.json")
        
        if not post_ids_file:
            print("Warning: post_ids.json file not found in Gist")
            return set()
        
        content = post_ids_file.get("content", "{}")
        data = json.loads(content)
        post_ids = data.get("post_ids", [])
        
        print(f"Retrieved {len(post_ids)} processed post IDs from Gist")
        return set(post_ids)
        
    except requests.RequestException as e:
        print(f"Error fetching Gist: {e}")
        return set()
    except json.JSONDecodeError as e:
        print(f"Error parsing Gist JSON: {e}")
        return set()
    except Exception as e:
        print(f"Unexpected error retrieving processed post IDs: {e}")
        return set()


def add_processed_post_id(post_id: str) -> bool:
    """
    Add a post ID to the list of processed posts in the GitHub Gist.
    Returns True if successful, False otherwise.
    """
    gist_token = os.getenv("GIST_TOKEN")
    gist_id = os.getenv("GIST_ID")
    
    if not gist_token or not gist_id:
        print("Warning: GIST_TOKEN or GIST_ID not found in environment variables")
        return False
    
    try:
        # First, get the current processed post IDs
        current_post_ids = list(get_processed_post_ids())
        
        # Add the new post ID if it's not already there
        if post_id not in current_post_ids:
            current_post_ids.append(post_id)
            
            # Prepare the updated content
            updated_content = {
                "post_ids": current_post_ids
            }
            
            headers = {
                "Authorization": f"token {gist_token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json"
            }
            
            # Update the Gist
            update_data = {
                "files": {
                    "post_ids.json": {
                        "content": json.dumps(updated_content, indent=2)
                    }
                }
            }
            
            response = requests.patch(f"https://api.github.com/gists/{gist_id}", 
                                    headers=headers, 
                                    json=update_data)
            response.raise_for_status()
            
            #print(f"Successfully added post ID {post_id} to Gist")
            return True
        else:
            print(f"Post ID {post_id} already exists in Gist")
            return True
            
    except requests.RequestException as e:
        print(f"Error updating Gist: {e}")
        return False
    except json.JSONDecodeError as e:
        print(f"Error parsing Gist JSON: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error adding post ID to Gist: {e}")
        return False 