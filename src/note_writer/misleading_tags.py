import json
import re
from typing import List

from data_models import MisleadingTag, Post
from note_writer.llm_util import get_gemini_response


def get_misleading_tags(
    post: Post, images_summary: str, note_text: str, retries: int = 3
) -> List[MisleadingTag]:
    misleading_why_tags_prompt = _get_prompt_for_misleading_why_tags(
        post, images_summary, note_text
    )
    while retries > 0:
        try:
            misleading_why_tags_str = get_gemini_response(misleading_why_tags_prompt)
            
            # Try to extract JSON from the response more robustly
            json_data = _extract_json_from_response(misleading_why_tags_str)
            
            if json_data and "misleading_tags" in json_data:
                misleading_why_tags = json_data["misleading_tags"]
                return [MisleadingTag(tag) for tag in misleading_why_tags]
            else:
                raise ValueError(f"No valid JSON found in response: {misleading_why_tags_str}")
                
        except Exception as e:
            print(f"Error parsing misleading tags (attempt {4-retries}): {e}")
            retries -= 1
            if retries == 0:
                # Fallback: return a default tag
                print("Using fallback: missing_important_context")
                return [MisleadingTag("missing_important_context")]
    
    return [MisleadingTag("missing_important_context")]


def _extract_json_from_response(response: str) -> dict:
    """Extract JSON from model response, handling various formats"""
    if not response or not response.strip():
        return None
        
    response = response.strip()
    
    # Try direct JSON parsing first
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    
    # Try to find JSON within the response using regex
    json_pattern = r'\{[^{}]*"misleading_tags"[^{}]*\[[^\]]*\][^{}]*\}'
    matches = re.findall(json_pattern, response, re.DOTALL)
    
    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue
    
    # Try to find just the array part
    array_pattern = r'"misleading_tags":\s*(\[[^\]]*\])'
    match = re.search(array_pattern, response)
    if match:
        try:
            tags_array = json.loads(match.group(1))
            return {"misleading_tags": tags_array}
        except json.JSONDecodeError:
            pass
    
    return None


def _get_prompt_for_misleading_why_tags(
    post: Post, images_summary: str = "", note: str = ""
):
    images_summary_prompt = f"""```
    Summary of images in the post:
    ```
    {images_summary}
    ```"""

    return f"""Below will be a post on X, and a proposed community note that \
        adds additional context to the potentially misleading post. \
        Your task will be to identify which of the following tags apply to the post and note. \
        You may choose as many tags as apply, but you must choose at least one. \
        You must respond in valid JSON format, with a list of which of the following options apply:
        - "factual_error":  # the post contains a factual error
        - "manipulated_media":  # the post contains manipulated/fake/out-of-context media
        - "outdated_information":  # the post contains outdated information
        - "missing_important_context":  # the post is missing important context
        - "disputed_claim_as_fact":  # including unverified claims
        - "misinterpreted_satire":  # the post is satire that may likely be misinterpreted as fact
        - "other":  # the post contains other misleading reasons

        Example valid JSON response:
        {{
            "misleading_tags": ["factual_error", "outdated_information", "missing_important_context"]
        }}

        OTHER = 0
        FACTUAL_ERROR = 1
        MANIPULATED_MEDIA = 2
        OUTDATED_INFORMATION = 3
        MISSING_IMPORTANT_CONTEXT = 4
        DISPUTED_CLAIM_AS_FACT = 5
        MISINTERPRETED_SATIRE = 6

        The post and note are as follows:

        ```
        Post text:
        ```
        {post.text}
        ```

        {images_summary_prompt}

        ```
        Proposed community note:
        ```
        {note}
        ```
        """
